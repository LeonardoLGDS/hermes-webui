"""Lifecycle for a WebUI-owned synthetic external-completion turn.

This module is a small integration building block.  It owns the claim / renew /
receipt / release lifecycle around an ``external_completion_v1`` delegation
event that the WebUI consumer has been asked to turn into a synthetic user
message. The streaming caller owns the try/finally and actual persistence.

Design contract (fail-closed):

* ``acquire()`` validates the queued event against the live bindings *before*
  it claims the durable delivery.  It never derives owner/route from the
  event; every authoritative binding must be supplied in ``bindings``.
* No legacy ACK wrapper and no pre-run ACK.  The only ACK is the atomic
  receipt recorded by ``finish(completed=True)`` once the caller has persisted
  the synthetic continuation.
* A bounded background worker renews the claim while the turn runs.  A renewal
  failure poisons the turn so ``finish`` can never ACK.
* Any validation/claim/renewal failure or non-true completion
  releases the delivery claim so the event can be retried by another consumer.
* Explicit user cancellation retires the claimed automatic delivery as dropped,
  preserving the completed job record without requeuing another model turn.
"""

from __future__ import annotations

import threading
import copy
import contextvars
import logging
import math
from typing import Any, Dict, Optional

_MIN_RENEW_INTERVAL = 0.01
_DEFAULT_RENEW_INTERVAL = 30.0
_SHUTDOWN_JOIN_TIMEOUT = 5.0


class ExternalCompletionTurn:
    """Own the claim/renew/receipt lifecycle for one external completion event.

    Parameters
    ----------
    event:
        The queued ``async_delegation`` event dict.
    bindings:
        The live consumer's authoritative bindings.  Must include
        ``owner_session_id`` plus the other consumer bindings the validator
        understands.  ``owner_session_id`` is required here because it must
        never be taken from the event.
    completion_queue:
        The queue that owns the event, used to requeue on failure.
    native:
        Injected native module (defaults to a lazy ``tools.async_delegation``
        import).  Tests inject a fake; production wiring imports on first use.
    renew_interval:
        Seconds between renewal attempts.  Values are floored to a small
        positive minimum so a caller cannot busy-loop the worker.
    """

    def __init__(
        self,
        event: Dict[str, Any],
        *,
        bindings: Dict[str, Any],
        completion_queue: Any,
        native: Any = None,
        renew_interval: float = _DEFAULT_RENEW_INTERVAL,
    ) -> None:
        self.event = copy.deepcopy(event)
        self.bindings = copy.deepcopy(bindings)
        self.completion_queue = completion_queue
        self._native = native
        interval = float(renew_interval)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("renew_interval must be finite and positive")
        self._renew_interval = min(max(interval, _MIN_RENEW_INTERVAL), 60.0)

        self._delegation_id = event.get("delegation_id") if isinstance(event, dict) else None
        self._claim_id: Optional[str] = None
        self._marker: Optional[str] = None
        self._acquired = False
        self._finished = False
        self._renewal_failed = False
        self._cancel = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._requeued = False

    # ------------------------------------------------------------------ util

    def _native_module(self) -> Any:
        if self._native is None:
            from tools import async_delegation  # local import: avoid heavy import at module load

            self._native = async_delegation
        return self._native

    def _requeue(self) -> None:
        """Best-effort requeue; never raise out of cleanup paths."""
        if self._requeued:
            return
        try:
            put = getattr(self.completion_queue, "put", None)
            if callable(put):
                put(self.event)
            else:  # pragma: no cover - defensive
                self.completion_queue.append(self.event)
            self._requeued = True
        except Exception:  # pragma: no cover - cleanup must not raise
            pass

    def _release_claim(self) -> None:
        if not self._claim_id or not self._delegation_id:
            return
        try:
            self._native_module().release_event_delivery(self.event, self._claim_id)
        except Exception:  # pragma: no cover - cleanup must not raise
            pass

    # -------------------------------------------------------------- lifecycle

    def acquire(self) -> bool:
        """Validate, claim, and start renewal.  Returns True only when armed."""
        if self._finished or self._requeued:
            return False
        if self._acquired:
            return True
        if not isinstance(self.bindings, dict) or not self.bindings.get("owner_session_id"):
            # Owner must be supplied; never derived from the event.
            self._requeue()
            return False

        try:
            native = self._native_module()
        except Exception:
            self._requeue()
            return False

        try:
            valid = native.validate_external_completion_event(
                self.event, bindings=self.bindings
            )
        except Exception:
            valid = False
        if not valid:
            self._requeue()
            return False

        try:
            marker = native.external_completion_continuation_marker(self._delegation_id)
        except Exception:
            self._requeue()
            return False

        try:
            claim_id = native.claim_event_delivery(self.event, "webui-external-turn")
        except Exception:
            claim_id = None
        if not claim_id:
            self._requeue()
            return False

        self._claim_id = claim_id
        self._marker = marker
        self._acquired = True
        try:
            self._start_renewal()
        except Exception:
            self._renewal_failed = True
            self._release_claim()
            self._requeue()
            self._acquired = False
            self._worker = None
            return False
        return True

    def _start_renewal(self) -> None:
        self._cancel.clear()
        # Python worker threads do not inherit the streaming turn's profile
        # ContextVar. Renew in its copied context, never the default profile.
        context = contextvars.copy_context()
        worker = threading.Thread(
            target=context.run, args=(self._renew_loop,),
            name="external-completion-renew", daemon=True,
        )
        self._worker = worker
        worker.start()

    def _renew_loop(self) -> None:
        native = self._native_module()
        while not self._cancel.wait(self._renew_interval):
            if self._renewal_failed:
                return
            try:
                ok = native.renew_external_completion_delivery_claim(
                    self._delegation_id, self._claim_id
                )
            except Exception:
                ok = False
            if not ok:
                # Poison the turn: finish() must never ACK after this.
                self._renewal_failed = True
                return

    def _stop_renewal(self) -> None:
        self._cancel.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=_SHUTDOWN_JOIN_TIMEOUT)
            if worker.is_alive():
                self._renewal_failed = True
            else:
                self._worker = None

    def augment_message(self, original: str) -> str:
        """Return marker-prefixed text used as both the live and persisted turn."""
        if not self._acquired or self._marker is None:
            raise RuntimeError("augment_message requires an acquired external completion turn")
        if not isinstance(original, str):
            raise TypeError("original message must be a string")
        return self._marker + "\n" + original

    def finish(self, *, completed: bool, cancelled: bool = False) -> bool:
        """Stop renewal and attempt the single atomic receipt.  Idempotent."""
        if self._finished:
            return self._last_finish_result
        self._stop_renewal()

        if cancelled is True:
            # Explicit user Stop is not a transient delivery failure. Preserve
            # the completed job/result but retire this automatic delivery;
            # do not enqueue another model turn after the user cancelled it.
            if self._acquired:
                try:
                    dropped = self._native_module().drop_completion_delivery(
                        self._delegation_id, self._claim_id,
                    )
                except Exception:
                    dropped = False
                if dropped is not True:
                    logging.getLogger(__name__).error(
                        "External cancellation could not retire delivery %s; "
                        "claim was not overwritten; reconciliation required",
                        self._delegation_id,
                    )
            self._finished = True
            self._last_finish_result = False
            return False

        if not self._acquired or completed is not True or self._renewal_failed:
            self._release_claim()
            self._requeue()
            self._finished = True
            self._last_finish_result = False
            return False

        try:
            ok = self._native_module().record_external_continuation_receipt_if_persisted(
                self._delegation_id,
                claim_id=self._claim_id,
                owner_session_id=self.bindings["owner_session_id"],
                marker=self._marker,
                turn_completed=True,
            )
        except Exception:
            ok = False

        if ok is not True:
            self._release_claim()
            self._requeue()
            self._finished = True
            self._last_finish_result = False
            return False

        self._finished = True
        self._last_finish_result = True
        return True

    _last_finish_result = False

    # ------------------------------------------------------------- ctx mgr

    def __enter__(self) -> "ExternalCompletionTurn":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Caller still owns the persistence try/finally; we only guarantee the
        # renewal worker is stopped and a claim is not leaked on exception.
        if not self._finished:
            self.finish(completed=False)
        return False
