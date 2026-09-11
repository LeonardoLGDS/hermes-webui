"""R115 — the shutdown drain must wait for the turn's result persistence.

R114 added a bounded SIGTERM drain that waits for in-flight turn workers, but it
waits on the RUN registry (ACTIVE_RUNS/STREAMS) only. Tonight's live acceptance
(2026-09-11, session dbc734a8c477) showed the remaining defect: the registry
empties when the WORKER unwinds, while the completed turn's final transcript
persist (owned through SESSION_WRITEBACK_OWNERS) can still be in flight. The
process exited, the persist was lost, ``active_stream_id`` stayed set on disk,
and the next load stamped "Response interrupted ... the WebUI process started
after this turn began".

These tests are subprocess-level end-to-end cases. Each case boots a real
``server.py`` in an isolated state dir with a stand-in deferred-save run that
uses the SAME production registries a real worker uses
(``register_session_writeback_owner`` / ``register_active_run``), removes its
RUN row first, then performs a real ``Session.save()`` and finally releases the
writeback owner. The observable that matters is the persisted transcript plus a
simulated next-load recovery, not elapsed time.

The drain outcome-line cases cover R115's auditability requirement: the final
``[shutdown-drain] outcome=...`` line must reach the journal on the finished
and bound_hit paths (tonight it never appeared, making the failure
unattributable).
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSION_ID = "r115-deferred-session"
STREAM_ID = "r115-deferred-stream"
REPLY_TEXT = "R115 deferred reply"
PROMPT_TEXT = "R115 question"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX SIGTERM drain semantics (systemd/ctl.sh stop path)",
)

# Wrapper executed by `python -c` from the repo root. It seeds one deferred-save
# session whose RUN row clears before its result persist lands — the R115 shape:
# the registry tracks the run, the writeback owner tracks the durable outcome.
_WRAPPER = textwrap.dedent(
    """
    import os, runpy, sys, threading, time
    repo = os.environ["R115_REPO"]
    sys.path.insert(0, repo)
    if os.environ.get("R115_DEFERRED_RUN") == "1":
        from api import config as cfg
        from api.models import Session

        sid = os.environ["R115_SESSION_ID"]
        stream_id = os.environ["R115_STREAM_ID"]
        s = Session(
            session_id=sid,
            title="R115",
            messages=[
                {"role": "user", "content": "prior question"},
                {"role": "assistant", "content": "prior reply"},
            ],
        )
        # A deferred-save turn in flight: the sidecar carries the pending user
        # turn + active_stream_id and no assistant row yet (exactly the shape
        # that fooled the pre-deploy idle check).
        s.active_stream_id = stream_id
        s.pending_user_message = os.environ["R115_PROMPT"]
        s.pending_started_at = time.time()
        s.save()
        # Same production registration order a real route+worker uses: the
        # writeback owner is recorded at admission, the RUN row when the worker
        # starts.
        cfg.register_session_writeback_owner(sid, stream_id)
        cfg.register_active_run(stream_id, session_id=sid, phase="running")

        def _finish():
            trigger = os.environ["R115_TRIGGER"]
            deadline = time.time() + 120
            while not os.path.exists(trigger) and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(float(os.environ.get("R115_RUN_SECONDS", "0.3")))
            # The RUN registry empties here (worker liveness ends) ...
            cfg.unregister_active_run(stream_id)
            print("R115_RUN_REGISTRY_EMPTY", flush=True)
            # ... while the turn's deferred final persist is still in flight.
            time.sleep(float(os.environ.get("R115_PERSIST_DELAY", "2.0")))
            fresh = Session.load(sid)
            fresh.messages = list(fresh.messages) + [
                {"role": "assistant", "content": os.environ["R115_REPLY"]}
            ]
            fresh.active_stream_id = None
            fresh.pending_user_message = None
            fresh.pending_attachments = []
            fresh.pending_started_at = None
            fresh.pending_user_source = None
            fresh.save()
            print("R115_RESULT_PERSISTED", flush=True)
            # Durable outcome landed; only now is the writeback released.
            cfg.clear_session_writeback_owner_if_owned(sid, stream_id)
            print("R115_WRITEBACK_RELEASED", flush=True)

        threading.Thread(target=_finish, name="r115-deferred-run", daemon=True).start()
    runpy.run_path(os.path.join(repo, "server.py"), run_name="__main__")
    """
)

# Simulated next load, run in a FRESH process (the new WebUI after restart).
_RECOVERY = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, sys.argv[1])
    import api.models as models
    # The real next load happened ~55s after SIGTERM tonight, well past the
    # grace guard; this simulates that without a 30s sleep.
    models._REPAIR_STALE_PENDING_GRACE_SECONDS = 0
    s = models.Session.load(sys.argv[2])
    repaired = models._repair_stale_pending(s)
    contents = [
        str(m.get("content") or "")
        for m in (s.messages or [])
        if isinstance(m, dict)
    ]
    print("R115_RECOVERY_JSON=" + json.dumps({
        "repaired": bool(repaired),
        "active_stream_id": getattr(s, "active_stream_id", None),
        "pending_user_message": getattr(s, "pending_user_message", None),
        "contents": contents,
    }))
    """
)


class _ServerProcess:
    """A real server subprocess plus its isolated state/output log."""

    def __init__(self, tmp_path, *, drain_seconds=30.0, persist_delay=2.0, run_seconds=0.3):
        self.scratch = Path(tmp_path)
        self.state = self.scratch / "state"
        (self.state / "sessions").mkdir(parents=True, exist_ok=True)
        (self.state / "ws").mkdir(parents=True, exist_ok=True)
        self.trigger = self.scratch / "run-trigger"
        self.log_path = self.scratch / "server.log"

        env = os.environ.copy()
        env.update({
            # Mirror conftest's out-of-process server env: hard-isolated state,
            # no live credentials routing, no production ~/.hermes writes.
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_STATE_DIR": str(self.state),
            "HERMES_HOME": str(self.state),
            "HERMES_BASE_HOME": str(self.state),
            "HERMES_CONFIG_PATH": str(self.state / "config.yaml"),
            "HERMES_WEBUI_DEFAULT_WORKSPACE": str(self.state / "ws"),
            "HERMES_WEBUI_DEFAULT_MODEL": "openai/gpt-5.4-mini",
            "HERMES_WEBUI_PASSWORD": "",
            "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
            "R115_REPO": str(REPO_ROOT),
            "R115_DEFERRED_RUN": "1",
            "R115_TRIGGER": str(self.trigger),
            "R115_SESSION_ID": SESSION_ID,
            "R115_STREAM_ID": STREAM_ID,
            "R115_PROMPT": PROMPT_TEXT,
            "R115_REPLY": REPLY_TEXT,
            "R115_RUN_SECONDS": str(run_seconds),
            "R115_PERSIST_DELAY": str(persist_delay),
            "HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS": str(drain_seconds),
        })
        self.port = _free_port()
        env["HERMES_WEBUI_PORT"] = str(self.port)
        self.env = env
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _WRAPPER],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            **_no_window_kwargs(),
        )

    # ── lifecycle helpers ────────────────────────────────────────────────
    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def wait_ready(self, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(
                    f"server exited early rc={self.proc.returncode}\n{self.log_text()[-4000:]}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.25):
                    return
            except OSError:
                time.sleep(0.1)
        pytest.fail(f"server did not listen on {self.port}\n{self.log_text()[-4000:]}")

    def start_run(self) -> None:
        self.trigger.write_text("go", encoding="utf-8")

    def sigterm(self) -> float:
        os.kill(self.proc.pid, signal.SIGTERM)
        return time.monotonic()

    def wait_exit(self, timeout: float) -> None:
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            pytest.fail(
                f"server did not exit within {timeout}s\n{self.log_text()[-4000:]}"
            )

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        try:
            self._log.close()
        except Exception:
            pass

    def recover(self) -> dict:
        """Load the persisted session in a fresh process and run the next-load repair."""
        result = subprocess.run(
            [sys.executable, "-c", _RECOVERY, str(REPO_ROOT), SESSION_ID],
            cwd=str(REPO_ROOT),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
            **_no_window_kwargs(),
        )
        assert result.returncode == 0, f"recovery subprocess failed:\n{result.stdout}\n{result.stderr}"
        for line in result.stdout.splitlines():
            if line.startswith("R115_RECOVERY_JSON="):
                return json.loads(line.split("=", 1)[1])
        raise AssertionError(f"recovery produced no verdict:\n{result.stdout}\n{result.stderr}")


def _no_window_kwargs() -> dict:
    if sys.platform == "win32":  # pragma: no cover - skipped on win32
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _boot(tmp_path, **kwargs) -> _ServerProcess:
    proc = _ServerProcess(tmp_path, **kwargs)
    try:
        proc.wait_ready()
    except BaseException:
        proc.kill()
        raise
    return proc


# ── observables ──────────────────────────────────────────────────────────────

def test_sigterm_waits_for_deferred_result_persist_and_next_load_has_no_marker(tmp_path):
    """END-TO-END: registry empties, persist still lands, next load stays clean.

    Fails before R115: R114's drain exits once the RUN row is gone, the process
    dies during the deferred persist, and the simulated next load stamps the
    "Response interrupted ... process started after this turn began" marker.
    """
    proc = _boot(tmp_path, drain_seconds=30.0, persist_delay=2.0, run_seconds=0.3)
    started = proc.sigterm()
    proc.start_run()
    proc.wait_exit(timeout=40)
    elapsed = time.monotonic() - started
    out = proc.log_text()
    proc.kill()

    # The drain must have waited for the writeback-owned persist, not just the
    # RUN registry.
    assert "R115_RUN_REGISTRY_EMPTY" in out, out[-3000:]
    assert f"waiting for result persistence session={SESSION_ID}" in out, out[-3000:]
    assert "R115_RESULT_PERSISTED" in out, out[-3000:]
    assert "R115_WRITEBACK_RELEASED" in out, out[-3000:]
    assert "[shutdown-drain] outcome=finished" in out, out[-3000:]
    assert out.index("R115_RUN_REGISTRY_EMPTY") < out.index("R115_RESULT_PERSISTED"), (
        "the run registry emptied before the persist landed; the drain must "
        f"wait past that point\n{out[-3000:]}"
    )
    assert out.index("R115_RESULT_PERSISTED") < out.index("outcome=finished"), (
        f"the drain's outcome predates the persisted result\n{out[-3000:]}"
    )
    assert elapsed >= 1.5, f"shutdown did not wait for the persist ({elapsed:.2f}s)"
    assert elapsed < 40.0, f"shutdown overran the persist ({elapsed:.2f}s)"

    verdict = proc.recover()
    assert REPLY_TEXT in verdict["contents"], verdict
    assert verdict["active_stream_id"] in (None, ""), verdict
    assert verdict["pending_user_message"] in (None, ""), verdict
    assert verdict["repaired"] is False, verdict
    assert not any("Response interrupted" in c for c in verdict["contents"]), verdict
    assert not any(
        "WebUI process started after this turn began" in c for c in verdict["contents"]
    ), verdict


def test_drain_outcome_line_present_on_finished_path(tmp_path):
    """Auditability: the finished-path outcome line reaches the captured output."""
    proc = _boot(tmp_path, drain_seconds=30.0, persist_delay=0.5, run_seconds=0.2)
    proc.sigterm()
    proc.start_run()
    proc.wait_exit(timeout=40)
    out = proc.log_text()
    proc.kill()
    assert "[shutdown-drain] outcome=finished" in out, out[-3000:]
    # The run-wait line from R114 is still emitted; the persist wait is the
    # R115 addition (observed because the owner is registered before the run).
    assert "waiting for result persistence" in out, out[-3000:]


def test_drain_outcome_line_present_on_bound_hit_path(tmp_path):
    """Auditability: bound_hit still logs its outcome line, bounded like R114."""
    bound = 2.0
    proc = _boot(tmp_path, drain_seconds=bound, persist_delay=30.0, run_seconds=0.2)
    started = proc.sigterm()
    proc.start_run()
    proc.wait_exit(timeout=30)
    elapsed = time.monotonic() - started
    out = proc.log_text()
    proc.kill()
    assert "waiting for result persistence" in out, out[-3000:]
    assert "[shutdown-drain] outcome=bound_hit" in out, out[-3000:]
    # The drain was cut at the bound; the deferred persist had not landed.
    assert "R115_RESULT_PERSISTED" not in out, out[-3000:]
    assert elapsed >= bound - 0.3, f"shutdown exited before the bound ({elapsed:.2f}s)"
    assert elapsed < bound + 15.0, f"shutdown hung past the bound ({elapsed:.2f}s)"


def test_drain_waits_for_writeback_owner_after_run_registry_empties():
    """Focused: only the writeback owner is pending → the drain still waits.

    This isolates the R115 mechanism from the server subprocess: with no RUN
    row at all, R114 returned immediately; the durable-outcome wait must hold
    until the owner record clears and must report it.
    """
    import threading as _threading

    from api import config as cfg
    from api import streaming

    cfg.ACTIVE_RUNS.clear()
    cfg.SESSION_WRITEBACK_OWNERS.clear()
    try:
        cfg.register_session_writeback_owner("r115-owner-session", "r115-owner-stream")

        def _release():
            time.sleep(0.6)
            cfg.clear_session_writeback_owner_if_owned(
                "r115-owner-session", "r115-owner-stream"
            )

        releaser = _threading.Thread(target=_release, daemon=True)
        releaser.start()
        t0 = time.monotonic()
        result = streaming.drain_in_flight_runs(
            deadline_seconds=10.0, poll_seconds=0.02, settle_seconds=0.02
        )
        elapsed = time.monotonic() - t0
        releaser.join(timeout=5)
    finally:
        cfg.clear_session_writeback_owner_if_owned(
            "r115-owner-session", "r115-owner-stream"
        )
        cfg.SESSION_WRITEBACK_OWNERS.clear()
        cfg.ACTIVE_RUNS.clear()

    assert result["outcome"] == "finished", result
    assert result["persist_waited"] == ["r115-owner-session"], result
    assert elapsed >= 0.5, f"drain did not wait for the writeback owner ({elapsed:.3f}s)"
    assert elapsed < 10.0, f"drain overran its deadline ({elapsed:.3f}s)"


def test_outcome_line_survives_detached_stdout(monkeypatch, capfd):
    """The outcome sink must survive interpreter teardown of sys.stdout.

    Tonight the outcome line never reached the journal while earlier lines did;
    the durable sink writes fd 1 directly, so a detached/closed ``sys.stdout``
    (the interpreter-shutdown candidate) cannot swallow it.
    """
    from api import streaming

    class _BrokenStdout:
        def write(self, *_args, **_kwargs):
            raise ValueError("stdout torn down during interpreter shutdown")

        def flush(self, *_args, **_kwargs):
            raise ValueError("stdout torn down during interpreter shutdown")

    monkeypatch.setattr(streaming.sys, "stdout", _BrokenStdout())
    monkeypatch.setattr(streaming.sys, "__stdout__", _BrokenStdout())

    streaming.log_shutdown_drain(
        "[shutdown-drain] outcome=finished waited=0 elapsed=0.01s bound=1.0s persist=0"
    )
    captured = capfd.readouterr().out
    assert "[shutdown-drain] outcome=finished" in captured, captured
