"""R114 — a SIGTERM stop waits (bounded) for in-flight turns before exit.

The defect: turn workers run in daemon threads, so any managed restart that
lands during a turn kills it and the next session load stamps the user-visible
"Response interrupted ... the WebUI process started after this turn began"
marker. server.py's shutdown `finally` drained only memory commits.

These tests are subprocess-level: each case boots a real ``server.py`` in an
isolated state dir, optionally pre-registers a stand-in run in the SAME
``ACTIVE_RUNS`` registry a real worker uses (no provider call needed), sends
SIGTERM, and measures what the process did:

  * idle            → exits promptly (< 2s), drain sees no runs
  * registered run  → does not exit until the run finishes (within the bound)
  * run past bound  → exits after the bound, not later (small bound override)
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX SIGTERM drain semantics (systemd/ctl.sh stop path)",
)

# Wrapper executed by `python -c` from the repo root: register a stand-in run in
# ACTIVE_RUNS (identical registry + unregister path a real worker uses), then
# hand off to the real server entry point. The run waits for a trigger file so
# the test controls exactly how much of it is left when SIGTERM lands.
_WRAPPER = textwrap.dedent(
    """
    import os, runpy, sys, threading, time
    repo = os.environ["R114_REPO"]
    sys.path.insert(0, repo)
    if os.environ.get("R114_FAKE_RUN") == "1":
        from api import config as cfg
        cfg.register_active_run(
            "r114-fake-stream", session_id="r114-fake-session", phase="running"
        )
        def _finish():
            trigger = os.environ.get("R114_FAKE_RUN_TRIGGER")
            deadline = time.time() + 120
            while trigger and not os.path.exists(trigger) and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(float(os.environ.get("R114_FAKE_RUN_SECONDS", "3")))
            cfg.unregister_active_run("r114-fake-stream")
            print("R114_FAKE_RUN_FINISHED", flush=True)
        threading.Thread(target=_finish, name="r114-fake-run", daemon=True).start()
    runpy.run_path(os.path.join(repo, "server.py"), run_name="__main__")
    """
)


class _ServerProcess:
    """A real server subprocess plus its isolated env/state/output log."""

    def __init__(self, tmp_path, *, run_seconds=None, drain_seconds=None):
        self.scratch = Path(tmp_path)
        state = self.scratch / "state"
        (state / "ws").mkdir(parents=True, exist_ok=True)
        trigger = self.scratch / "run-trigger"
        self.trigger = trigger
        self.log_path = self.scratch / "server.log"

        env = os.environ.copy()
        env.update({
            # Mirror conftest's out-of-process server env: hard-isolated state,
            # no live credentials routing, no production ~/.hermes writes.
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_STATE_DIR": str(state),
            "HERMES_HOME": str(state),
            "HERMES_BASE_HOME": str(state),
            "HERMES_CONFIG_PATH": str(state / "config.yaml"),
            "HERMES_WEBUI_DEFAULT_WORKSPACE": str(state / "ws"),
            "HERMES_WEBUI_DEFAULT_MODEL": "openai/gpt-5.4-mini",
            "HERMES_WEBUI_PASSWORD": "",
            "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
            "R114_REPO": str(REPO_ROOT),
            "R114_FAKE_RUN": "1" if run_seconds is not None else "0",
            "R114_FAKE_RUN_TRIGGER": str(trigger),
            "R114_FAKE_RUN_SECONDS": str(run_seconds or 0),
        })
        if drain_seconds is None:
            env.pop("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", None)
        else:
            env["HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS"] = str(drain_seconds)

        self.port = _free_port()
        env["HERMES_WEBUI_PORT"] = str(self.port)
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
        """Release the stand-in run so its sleep begins now (deterministic)."""
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


def _terminate_and_wait(proc: _ServerProcess, timeout: float = 30.0) -> float:
    started = proc.sigterm()
    proc.wait_exit(timeout)
    elapsed = time.monotonic() - started
    proc.kill()  # close the log handle, reap anything left
    return elapsed


# ── observables ──────────────────────────────────────────────────────────────

def test_sigterm_with_no_runs_exits_promptly(tmp_path):
    """Idle stop must stay prompt: the drain must be inert with nothing live."""
    proc = _boot(tmp_path)
    elapsed = _terminate_and_wait(proc, timeout=15)
    assert elapsed < 2.0, f"idle shutdown took {elapsed:.2f}s"
    out = proc.log_text()
    assert "[shutdown-drain] outcome=finished waited=0" in out, out[-2000:]


def test_sigterm_waits_for_registered_run_to_finish(tmp_path):
    """A live run must finish (and be observable) before the process exits."""
    proc = _boot(tmp_path, run_seconds=4, drain_seconds=30)
    proc.start_run()
    time.sleep(0.4)
    started = proc.sigterm()
    try:
        time.sleep(1.0)
        assert proc.proc.poll() is None, (
            "server exited while a registered run was still in flight\n"
            f"{proc.log_text()[-2000:]}"
        )
    finally:
        proc.wait_exit(timeout=30)
    elapsed = time.monotonic() - started
    proc.kill()
    out = proc.log_text()
    assert "R114_FAKE_RUN_FINISHED" in out, out[-2000:]
    assert "[shutdown-drain] waiting for run stream=r114-fake-stream" in out, out[-2000:]
    assert "[shutdown-drain] outcome=finished waited=1" in out, out[-2000:]
    # The run completed during the drain, i.e. before the exit outcome line.
    assert out.index("R114_FAKE_RUN_FINISHED") < out.index("outcome=finished")
    assert elapsed >= 2.0, f"shutdown did not wait for the run ({elapsed:.2f}s)"
    assert elapsed < 20.0, f"shutdown overran the run ({elapsed:.2f}s)"


def test_run_that_outlives_the_bound_is_cut_after_the_bound(tmp_path):
    """A run longer than the bound is cut — after the bound, not much later."""
    bound = 2.0
    proc = _boot(tmp_path, run_seconds=30, drain_seconds=bound)
    proc.start_run()
    time.sleep(0.3)
    elapsed = _terminate_and_wait(proc, timeout=20)
    out = proc.log_text()
    assert "[shutdown-drain] outcome=bound_hit waited=1" in out, out[-2000:]
    assert "R114_FAKE_RUN_FINISHED" not in out, out[-2000:]
    assert elapsed >= bound - 0.2, f"shutdown exited before the bound ({elapsed:.2f}s)"
    assert elapsed < bound + 8.0, f"shutdown hung past the bound ({elapsed:.2f}s)"


def test_new_turn_starts_are_refused_once_the_drain_begins(monkeypatch):
    """Mid-drain turn starts get a retryable refusal, not a cut turn."""
    import server
    from api import streaming

    # Replace the process-wide flag so this test can set it without leaving the
    # real module marked as draining for the rest of the session.
    monkeypatch.setattr(streaming, "_SHUTDOWN_DRAIN_REQUESTED", threading.Event())
    parsed = urlparse("/api/chat/start")
    assert server._shutdown_turn_refusal(parsed) is None

    streaming.begin_shutdown_drain()
    refusal = server._shutdown_turn_refusal(parsed)
    assert refusal is not None
    assert refusal["retryable"] is True
    assert refusal["code"] == "webui_restarting"
    assert "restarting" in refusal["error"].lower()
    # Non-turn routes are untouched by the refusal.
    assert server._shutdown_turn_refusal(urlparse("/api/sessions")) is None


def test_drain_bound_is_clamped_inside_the_systemd_stop_budget(monkeypatch):
    """The bound is overridable but can never overrun the 90s stop budget."""
    from api import streaming

    monkeypatch.delenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", raising=False)
    assert streaming.shutdown_drain_seconds() == streaming._SHUTDOWN_DRAIN_DEFAULT_SECONDS

    monkeypatch.setenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", "600")
    assert streaming.shutdown_drain_seconds() == streaming._SHUTDOWN_DRAIN_MAX_SECONDS
    # 90s systemd default − 30s memory-commit drain − margin.
    assert streaming._SHUTDOWN_DRAIN_MAX_SECONDS < 90.0
    assert streaming._SHUTDOWN_DRAIN_DEFAULT_SECONDS <= streaming._SHUTDOWN_DRAIN_MAX_SECONDS

    monkeypatch.setenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", "not-a-number")
    assert streaming.shutdown_drain_seconds() == streaming._SHUTDOWN_DRAIN_DEFAULT_SECONDS

    monkeypatch.setenv("HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS", "0")
    assert streaming.shutdown_drain_seconds() == 0.0


def test_drain_disabled_by_env_keeps_pre_r114_exit(tmp_path):
    """HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS=0 = escape hatch, exits immediately."""
    proc = _boot(tmp_path, run_seconds=30, drain_seconds=0)
    proc.start_run()
    time.sleep(0.3)
    elapsed = _terminate_and_wait(proc, timeout=15)
    out = proc.log_text()
    assert "[shutdown-drain] outcome=disabled" in out, out[-2000:]
    assert elapsed < 5.0, f"disabled drain still waited ({elapsed:.2f}s)"
