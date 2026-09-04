"""Fail-closed guard for in-process Hermes Agent source revisions.

Hermes WebUI currently imports ``run_agent.AIAgent`` into its long-lived server
process. If the Agent checkout changes while that process is alive, Python may
combine already-cached modules with newly-read source. Refuse to reuse that
mixed runtime and require a clean WebUI restart instead.
"""

from __future__ import annotations

from pathlib import Path
import sys
import subprocess
import threading

# Retain the discovered path as a diagnostic/test-visible compatibility value;
# runtime identity is deliberately captured from the loaded module below.
from api.config import _AGENT_DIR  # noqa: F401
from api.subprocess_utils import windows_hide_flags

_RESTART_MESSAGE = (
    "Hermes Agent source revision changed (was {old_revision}, now {new_revision}). "
    "Latest commit: {latest_commit}. "
    "Restart Hermes WebUI before retrying this action."
)


def _read_agent_revision(
    agent_dir: Path | None,
    *,
    module_path: Path | None = None,
) -> str | None:
    """Return the loaded Agent checkout HEAD, or ``None`` if it is not tracked."""
    if agent_dir is None:
        return None

    if module_path is None:
        module = sys.modules.get("run_agent")
        module_file = getattr(module, "__file__", None)
        if not module_file:
            return None
        try:
            module_path = Path(module_file).resolve()
        except (OSError, RuntimeError, TypeError):
            return None

    try:
        worktree_result = subprocess.run(
            ["git", "-C", str(agent_dir), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=windows_hide_flags(),
        )
        if worktree_result.returncode != 0:
            return None
        worktree = Path(worktree_result.stdout.strip()).resolve()
        relative_module = module_path.relative_to(worktree).as_posix()
        tracked_result = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(worktree),
                "ls-files",
                "--error-unmatch",
                "--",
                relative_module,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=windows_hide_flags(),
        )
        if tracked_result.returncode != 0:
            return None
        revision_result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
        return None

    revision = revision_result.stdout.strip()
    return revision if revision_result.returncode == 0 and revision else None


_AGENT_SOURCE_DIR: Path | None = None
_AGENT_MODULE_PATH: Path | None = None
_AGENT_REVISION: str | None = None
_AGENT_MODULE_MTIME_NS: int | None = None
_AIAgent = None
_RUNTIME_LOCK = threading.Lock()


class AgentRuntimeChangedError(RuntimeError):
    """Raised when the loaded Agent runtime no longer matches its source tree."""


def _loaded_agent_source_identity() -> tuple[Path, Path] | None:
    """Return the source directory and file that supplied ``run_agent``."""
    module = sys.modules.get("run_agent")
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return None
    try:
        module_path = Path(module_file).resolve()
        return module_path.parent, module_path
    except (OSError, RuntimeError, TypeError):
        return None


def _capture_loaded_agent_revision() -> None:
    """Bind the guard to the checkout that supplied the loaded Agent module."""
    global _AGENT_SOURCE_DIR, _AGENT_MODULE_PATH, _AGENT_REVISION
    global _AGENT_MODULE_MTIME_NS

    if _AGENT_REVISION is not None:
        ensure_agent_runtime_current()
        return

    identity = _loaded_agent_source_identity()
    if identity is None:
        return
    source_dir, module_path = identity
    current_revision = _read_agent_revision(source_dir, module_path=module_path)
    try:
        current_module_mtime_ns = module_path.stat().st_mtime_ns
    except OSError:
        current_module_mtime_ns = None
    _AGENT_SOURCE_DIR = source_dir
    _AGENT_MODULE_PATH = module_path
    _AGENT_REVISION = current_revision
    _AGENT_MODULE_MTIME_NS = current_module_mtime_ns


def ensure_agent_runtime_current() -> None:
    """Reject a known Git checkout change instead of mixing Python modules."""
    if _AGENT_REVISION is None:
        return
    fresh_revision = _read_agent_revision(
        _AGENT_SOURCE_DIR, module_path=_AGENT_MODULE_PATH
    )
    if fresh_revision == _AGENT_REVISION:
        return

    # WHY: removing .git from a loaded Git worktree makes revision discovery return
    # None without changing the loaded module. Treating that identity loss alone as
    # drift created a false-positive chat barrier on 2026-09-02. The captured
    # _AGENT_MODULE_MTIME_NS lets unchanged module content pass while a real source
    # change (or an unreadable mtime) still fails closed.
    if fresh_revision is None:
        try:
            fresh_module_mtime_ns = _AGENT_MODULE_PATH.stat().st_mtime_ns
        except OSError:
            fresh_module_mtime_ns = None
        if (
            _AGENT_MODULE_MTIME_NS is not None
            and fresh_module_mtime_ns == _AGENT_MODULE_MTIME_NS
        ):
            return

    latest_commit = "unavailable (author/date unavailable)"
    if _AGENT_SOURCE_DIR is not None:
        try:
            latest_commit_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(_AGENT_SOURCE_DIR),
                    "log",
                    "-1",
                    "--format=%h by %an at %aI",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
                creationflags=windows_hide_flags(),
            )
        except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
            pass
        else:
            commit_info = latest_commit_result.stdout.strip()
            if latest_commit_result.returncode == 0 and commit_info:
                latest_commit = commit_info

    raise AgentRuntimeChangedError(
        _RESTART_MESSAGE.format(
            old_revision=_AGENT_REVISION,
            new_revision=fresh_revision or "unavailable",
            latest_commit=latest_commit,
        )
    )


def require_ai_agent_class():
    """Import ``AIAgent`` after proving the loaded source revision is current."""
    ensure_agent_runtime_current()
    from run_agent import AIAgent  # noqa: PLC0415

    _capture_loaded_agent_revision()
    return AIAgent


def get_ai_agent_class():
    """Return ``AIAgent`` while preserving the existing lazy-import retry."""
    global _AIAgent, _AGENT_REVISION

    with _RUNTIME_LOCK:
        ensure_agent_runtime_current()
        if _AIAgent is None:
            try:
                agent_class = require_ai_agent_class()
            except ImportError:
                return None
            _AIAgent = agent_class
        return _AIAgent
