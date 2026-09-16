"""Thread-safe bounded in-memory handoff for external completion events.

Durable native storage remains the authority. This module is a small handoff
only: it never claims, ACKs, starts models, imports the server, opens a
database, or creates threads/timers.

Authority is ALWAYS the explicit caller-supplied binding (``owner_session_id``,
``owner_profile``, ``route``, ``policy_hash``) that the caller's native
validator checked against immutable SQLite enrollment inside the correctly
scoped profile. The native ``async_delegation`` envelope (with
``external_completion_v1``) is stored as an opaque deep copy, keeps canonical
equality, and is never mutated -- in particular this module never injects an
invented ``bindings`` field into it. ``pending_owners`` therefore returns the
stored explicit bindings, never identity derived from the event body.

The class exists so a background drain can hand a validated event to the real
owning turn without requeueing it into the queue that drain continuously
drains, while a short bounded wakeup lease suppresses racing duplicate wake
launches. It is not a substitute for the durable native receipt/ACK: the
owning turn still performs claim/renew/receipt only after durable writeback.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional


_REQUIRED_BINDING_KEYS = frozenset(
    ("owner_session_id", "owner_profile", "route", "policy_hash")
)
_MAX_PENDING_OWNERS_LIMIT = 64


class _OwnerEntry:
    """One pending owner: trusted binding copy plus FIFO event copies."""

    __slots__ = ("bindings", "events", "identity")

    def __init__(self, bindings: dict, identity: tuple) -> None:
        self.bindings = bindings
        self.events: List[dict] = []
        self.identity = identity


class ExternalCompletionMailbox:
    """Bounded in-memory mailbox keyed by trusted owner bindings.

    ``capacity`` bounds total pending events across owners; ``per_owner``
    bounds pending events for one owner. An unconsumed event is never evicted
    to make room -- capacity pressure fails closed so the caller leaves the
    durable native pending record alone.
    """

    def __init__(
        self,
        capacity: int = 256,
        per_owner: int = 8,
        wakeup_lease_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        capacity = self._require_positive_int("capacity", capacity)
        per_owner = self._require_positive_int("per_owner", per_owner)
        if per_owner > capacity:
            raise ValueError("per_owner must not exceed capacity")
        if isinstance(wakeup_lease_seconds, bool) or not isinstance(
            wakeup_lease_seconds, (int, float)
        ):
            raise ValueError("wakeup_lease_seconds must be a finite positive number")
        lease = float(wakeup_lease_seconds)
        if not math.isfinite(lease) or lease <= 0.0:
            raise ValueError("wakeup_lease_seconds must be a finite positive number")
        if not callable(clock):
            raise ValueError("clock must be callable")

        self._capacity = capacity
        self._per_owner = per_owner
        self._lease_seconds = lease
        self._clock = clock
        self._lock = threading.Lock()
        # owner key -> pending entry (insertion ordered for pending_owners).
        self._pending: Dict[tuple, _OwnerEntry] = {}
        # (owner_session_id, owner_profile) -> owner key, while that owner has
        # pending events. Rejects a second concurrent entry for the same
        # owner identity with a different route/policy.
        self._identities: Dict[tuple, tuple] = {}
        # delegation_id -> owner key. Bounded tombstones: kept after take so
        # the same delegation id can never be accepted as a different job for
        # another owner; evicted oldest-first once over capacity.
        self._delegations: Dict[str, tuple] = {}
        # owner key -> wakeup lease deadline (monotonic-ish). Bounded by
        # capacity; lazily expired, never with timers/threads.
        self._leases: Dict[tuple, float] = {}

    # -- public API ---------------------------------------------------------

    def offer(
        self,
        event: Mapping[str, Any],
        *,
        bindings: Mapping[str, Any],
        validate: Callable[..., bool],
    ) -> bool:
        """Offer an external completion event for an exact trusted owner.

        ``validate(event, bindings=bindings)`` is invoked *before* acquiring
        the internal lock, on copies, so it is the caller's native validator
        running inside the correct profile context. A non-callable validator,
        False, or an exception rejects the offer with no mutation.

        Returns True when a new slot was stored or an identical duplicate is
        already pending (no new slot, no lease reset). Returns False on
        validation failure, malformed event/bindings, a delegation id already
        bound to another owner or to a conflicting event, a conflicting
        route/policy for the same owner identity, or capacity rejection.
        """
        if not callable(validate):
            return False
        key = self._owner_key(bindings)
        if key is None:
            return False
        try:
            bindings_copy = copy.deepcopy(dict(bindings))
        except Exception:
            return False
        event_copy = self._event_copy(event)
        if event_copy is None:
            return False
        delegation_id = event_copy["delegation_id"]
        identity = (key[0], key[1])

        # Caller's native validator first, outside our lock, on copies only.
        try:
            accepted = validate(
                copy.deepcopy(event_copy), bindings=copy.deepcopy(bindings_copy)
            )
        except Exception:
            return False
        if not accepted:
            return False

        with self._lock:
            self._expire_leases_locked()
            # Global same-id conflict: never accepted as another owner/event.
            if self._delegations.get(delegation_id, key) != key:
                return False
            # Same owner identity must not gain a second concurrent entry with
            # a different route/policy.
            if self._identities.get(identity, key) != key:
                return False
            entry = self._pending.get(key)
            if entry is None:
                if self._total_events_locked() >= self._capacity:
                    return False
                if not self._remember_delegation_locked(delegation_id, key):
                    return False
                new_entry = _OwnerEntry(bindings_copy, identity)
                new_entry.events.append(event_copy)
                self._pending[key] = new_entry
                self._identities[identity] = key
                return True
            for existing in entry.events:
                if existing.get("delegation_id") != delegation_id:
                    continue
                if self._canonical(existing) == self._canonical(event_copy):
                    return True
                return False
            if len(entry.events) >= self._per_owner:
                return False
            if self._total_events_locked() >= self._capacity:
                return False
            if not self._remember_delegation_locked(delegation_id, key):
                return False
            entry.events.append(event_copy)
            return True

    def peek(self, *, bindings: Mapping[str, Any]) -> Optional[dict]:
        """Return an isolated FIFO-head copy without consuming or leasing it."""
        key = self._owner_key(bindings)
        if key is None:
            return None
        with self._lock:
            entry = self._pending.get(key)
            if entry is None or not entry.events:
                return None
            return copy.deepcopy(entry.events[0])

    def pending_events(self, *, bindings: Mapping[str, Any]) -> List[dict]:
        """Copy this owner's capacity-bounded FIFO without consuming leases."""
        key = self._owner_key(bindings)
        if key is None:
            return []
        with self._lock:
            entry = self._pending.get(key)
            return copy.deepcopy(entry.events) if entry is not None else []

    def take(self, *, bindings: Mapping[str, Any]) -> Optional[dict]:
        """Remove and return a copy of the oldest pending event for ``bindings``.

        Returns None unless the exact trusted owner binding matches a pending
        entry. A foreign or malformed binding never mutates another entry.
        When the last event for an owner is taken, the owner's entry is
        removed but any existing wakeup lease is kept until expiry so racing
        duplicate background wakes stay suppressed.
        """
        key = self._owner_key(bindings)
        if key is None:
            return None
        with self._lock:
            self._expire_leases_locked()
            entry = self._pending.get(key)
            if entry is None or not entry.events:
                return None
            event = entry.events.pop(0)
            if not entry.events:
                self._pending.pop(key, None)
                if self._identities.get(entry.identity) == key:
                    self._identities.pop(entry.identity, None)
            return copy.deepcopy(event)

    def reserve_wakeup(self, *, bindings: Mapping[str, Any]) -> bool:
        """Reserve a finite wakeup lease for an owner with a pending event.

        True only when the mailbox has a pending event for this exact owner and
        no unexpired lease exists. Repeated calls before expiry return False
        (no immediate repeat wake). The caller remains responsible for
        checking owner-busy / provider-pause before reserving; the mailbox
        starts no timers or threads.
        """
        key = self._owner_key(bindings)
        if key is None:
            return False
        with self._lock:
            self._expire_leases_locked()
            entry = self._pending.get(key)
            if entry is None or not entry.events:
                return False
            if not self._lease_expired_locked(key):
                return False
            if key not in self._leases and len(self._leases) >= self._capacity:
                # Bound owner tombstones; fail closed if lease capacity is full.
                return False
            self._leases[key] = self._now() + self._lease_seconds
            return True

    def pending_owners(self, limit: int = 8) -> List[dict]:
        """Return copies of the explicit bindings of owners with pending events.

        Insertion ordered and bounded; ``limit`` must be positive and at most
        ``_MAX_PENDING_OWNERS_LIMIT``. The returned identity is always the
        stored caller binding, never derived from an event body.
        """
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be a positive integer")
        if limit <= 0 or limit > _MAX_PENDING_OWNERS_LIMIT:
            raise ValueError(
                "limit must be positive and no greater than "
                + str(_MAX_PENDING_OWNERS_LIMIT)
            )
        with self._lock:
            self._expire_leases_locked()
            out: List[dict] = []
            for entry in self._pending.values():
                if not entry.events:
                    continue
                out.append(copy.deepcopy(entry.bindings))
                if len(out) >= limit:
                    break
            return out

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _require_positive_int(name: str, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(name + " must be a positive integer")
        return value

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("clock returned a non-numeric value")
        current = float(value)
        if not math.isfinite(current):
            raise ValueError("clock returned a non-finite value")
        return current

    def _expire_leases_locked(self) -> None:
        now = self._now()
        expired = [key for key, deadline in self._leases.items() if now >= deadline]
        for key in expired:
            self._leases.pop(key, None)

    def _lease_expired_locked(self, key: tuple) -> bool:
        deadline = self._leases.get(key)
        if deadline is None:
            return True
        return self._now() >= deadline

    def _total_events_locked(self) -> int:
        return sum(len(entry.events) for entry in self._pending.values())

    def _remember_delegation_locked(self, delegation_id: str, key: tuple) -> bool:
        existing = self._delegations.get(delegation_id)
        if existing is not None:
            return existing == key
        if len(self._delegations) >= self._capacity:
            evicted = False
            for candidate, owner in list(self._delegations.items()):
                if not self._is_live_delegation_locked(candidate, owner):
                    self._delegations.pop(candidate, None)
                    evicted = True
                    break
            if not evicted:
                return False
        self._delegations[delegation_id] = key
        return True

    def _is_live_delegation_locked(self, delegation_id: str, owner: tuple) -> bool:
        """True while this exact delegation id still has a pending event."""
        entry = self._pending.get(owner)
        if entry is None:
            return False
        for event in entry.events:
            if event.get("delegation_id") == delegation_id:
                return True
        return False

    @staticmethod
    def _is_nonempty_str(value: Any) -> bool:
        return isinstance(value, str) and value != ""

    @classmethod
    def _owner_key(cls, bindings: Any) -> Optional[tuple]:
        """Exact trusted binding -> hashable owner key, or None if malformed."""
        if not isinstance(bindings, Mapping):
            return None
        if set(bindings.keys()) not in (_REQUIRED_BINDING_KEYS, _REQUIRED_BINDING_KEYS | {"release_hash"}):
            return None
        release_hash = bindings.get("release_hash")
        if release_hash is not None and (
            not isinstance(release_hash, str) or len(release_hash) != 64
            or any(c not in "0123456789abcdef" for c in release_hash)
        ):
            return None
        owner_session_id = bindings.get("owner_session_id")
        owner_profile = bindings.get("owner_profile")
        route = bindings.get("route")
        policy_hash = bindings.get("policy_hash")
        if not cls._is_nonempty_str(owner_session_id):
            return None
        if not cls._is_nonempty_str(owner_profile):
            return None
        if not cls._is_nonempty_str(policy_hash):
            return None
        if not isinstance(route, Mapping) or not route:
            return None
        try:
            route_key = cls._freeze(route)
        except TypeError:
            return None
        return (owner_session_id, owner_profile, route_key, policy_hash, release_hash)

    @classmethod
    def _event_copy(cls, event: Any) -> Optional[dict]:
        """Validate the native envelope shape and return an unmutated deep copy."""
        if not isinstance(event, Mapping):
            return None
        if event.get("type") != "async_delegation":
            return None
        if not cls._is_nonempty_str(event.get("delegation_id")):
            return None
        if not isinstance(event.get("external_completion_v1"), Mapping):
            return None
        try:
            return copy.deepcopy(dict(event))
        except Exception:
            return None

    @classmethod
    def _canonical(cls, value: Any) -> Any:
        return cls._freeze(value)

    @classmethod
    def _freeze(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return tuple(
                sorted((cls._freeze(k), cls._freeze(v)) for k, v in value.items())
            )
        if isinstance(value, (list, tuple)):
            return tuple(cls._freeze(item) for item in value)
        if isinstance(value, set):
            return tuple(sorted(cls._freeze(item) for item in value))
        return value


_SHARED_MAILBOX: Optional[ExternalCompletionMailbox] = None
_SHARED_MAILBOX_LOCK = threading.Lock()


def get_shared_mailbox(
    capacity: int = 256,
    per_owner: int = 8,
    wakeup_lease_seconds: float = 30,
    clock: Callable[[], float] = time.monotonic,
) -> ExternalCompletionMailbox:
    """Return the process-wide mailbox shared by producer and consumer paths.

    Lazy so importing this module never has import-order side effects; the
    first caller's bounds win (all call sites pass the documented defaults).
    """
    global _SHARED_MAILBOX
    with _SHARED_MAILBOX_LOCK:
        if _SHARED_MAILBOX is None:
            _SHARED_MAILBOX = ExternalCompletionMailbox(
                capacity=capacity,
                per_owner=per_owner,
                wakeup_lease_seconds=wakeup_lease_seconds,
                clock=clock,
            )
        return _SHARED_MAILBOX


__all__ = ("ExternalCompletionMailbox", "get_shared_mailbox")
