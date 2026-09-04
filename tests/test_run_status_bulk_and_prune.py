"""Regression coverage for batched sidebar background status and client pruning."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
SESSION_STATUS_JS_PATH = REPO_ROOT / "static" / "session-status.js"
SESSIONS_SRC = SESSIONS_JS_PATH.read_text(encoding="utf-8")
SESSION_STATUS_SRC = SESSION_STATUS_JS_PATH.read_text(encoding="utf-8")

_NODE_CANDIDATES = (
    Path("/home/ops/.local/bin/node"),
    Path(shutil.which("node") or ""),
)
NODE = next((candidate for candidate in _NODE_CANDIDATES if candidate.is_file()), None)

pytestmark = pytest.mark.skipif(NODE is None, reason="node executable unavailable")


def _function_body(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start != -1, f"missing {signature}"
    opening_brace = source.find("{", start)
    assert opening_brace != -1, f"missing opening brace for {signature}"
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening_brace + 1 : index]
    raise AssertionError(f"could not extract complete function body for {signature}")


def _run_node_json(program: str) -> Any:
    assert NODE is not None
    with tempfile.TemporaryDirectory(prefix="hermes-status-prune-") as directory:
        driver = Path(directory) / "driver.js"
        driver.write_text(program, encoding="utf-8")
        result = subprocess.run(
            [str(NODE), str(driver)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"node failed ({result.returncode}):\n{result.stderr}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AssertionError(
                f"node did not return JSON: stdout={result.stdout!r} "
                f"stderr={result.stderr!r}"
            ) from error


def test_bulk_bg_active_maps_running_processes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """One registry listing supplies all active sidebar owner IDs."""
    from api import config
    from api.background_process import bulk_bg_active_by_session

    calls = {"list_sessions": 0, "get": 0}
    processes = {
        "proc-mapped": types.SimpleNamespace(session_key="process-key", exited=False),
        "proc-direct": types.SimpleNamespace(session_key="direct-webui", exited=False),
        "proc-exited": types.SimpleNamespace(session_key="finished-key", exited=True),
    }

    def list_sessions() -> list[dict[str, Any]]:
        calls["list_sessions"] += 1
        return [
            {"session_id": "proc-mapped", "status": "running"},
            {"session_id": "proc-direct", "status": "running"},
            {"session_id": "proc-exited", "status": "exited"},
        ]

    def get(process_id: str) -> types.SimpleNamespace | None:
        calls["get"] += 1
        return processes.get(process_id)

    registry_module = types.ModuleType("tools.process_registry")
    registry_module.process_registry = types.SimpleNamespace(  # type: ignore[attr-defined]
        list_sessions=list_sessions,
        get=get,
    )
    monkeypatch.setitem(sys.modules, "tools.process_registry", registry_module)
    monkeypatch.setitem(config.PROCESS_SESSION_INDEX, "process-key", "mapped-webui")
    monkeypatch.setitem(config.PROCESS_SESSION_INDEX, "finished-key", "finished-webui")

    observed = bulk_bg_active_by_session()

    assert observed == {"mapped-webui", "direct-webui"}
    assert calls == {"list_sessions": 1, "get": 2}

    def unavailable() -> list[dict[str, Any]]:
        raise RuntimeError("registry unavailable")

    registry_module.process_registry = types.SimpleNamespace(  # type: ignore[attr-defined]
        list_sessions=unavailable,
        get=get,
    )
    assert bulk_bg_active_by_session() == set()


def test_sidebar_payload_threads_one_bulk_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """/api/sessions resolves background rows from one shared owner set."""
    import api.background_process as background_process
    import api.routes as routes

    calls = {"bulk": 0}

    def bulk_bg_active_by_session() -> set[str]:
        calls["bulk"] += 1
        return {"active-session", "active-reference"}

    monkeypatch.setattr(
        background_process,
        "bulk_bg_active_by_session",
        bulk_bg_active_by_session,
    )
    monkeypatch.setattr(routes, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setattr(routes, "_session_list_cache_overlay_runtime_rows", lambda rows: rows)
    monkeypatch.setattr(routes, "_session_attention_summary", lambda _session_id: None)

    response = routes._session_list_payload_to_response(
        {
            "sessions": [
                {"session_id": "active-session", "title": "Active"},
                {"session_id": "idle-session", "title": "Idle"},
            ],
            "sidebar_reference_sessions": [
                {"session_id": "active-reference", "title": "Reference"},
                {"session_id": "idle-reference", "title": "Idle reference"},
            ],
            "cli_count": 0,
        }
    )

    assert calls == {"bulk": 1}
    assert [(row["session_id"], row["bg_active"]) for row in response["sessions"]] == [
        ("active-session", True),
        ("idle-session", False),
    ]
    assert [(row["session_id"], row["bg_active"]) for row in response["sidebar_reference_sessions"]] == [
        ("active-reference", True),
        ("idle-reference", False),
    ]


def test_session_status_prune_removes_browser_lifecycle_state() -> None:
    """Deleting a session drops its status registries and dot claims."""
    observed = _run_node_json(
        f"""
globalThis.window = globalThis;
globalThis.setTimeout = fn => fn;
globalThis.clearTimeout = () => undefined;
globalThis.document = {{createElement: () => ({{children: [], dataset: {{}}, style: {{}}}})}};
require({json.dumps(str(SESSION_STATUS_JS_PATH))});
const api = window._sessionStatus;
api.noteSessionRowsUpdated([
  {{session_id: 'deleted-session', bg_active: true}},
  {{session_id: 'survivor-session'}},
]);
api.ingestSubagentFrame({{
  name: 'subagent_progress',
  done: false,
  args: {{subagent_id: 'sub-agent', goal: 'Do work', status: 'running'}},
}}, 'deleted-session');
api.ingestBgStatus({{
  session_id: 'deleted-session',
  active: true,
  processes: [{{id: 'bg-agent', title: 'Job', state: 'running', started_at: 1}}],
}}, 'deleted-session');
const pruned = api.pruneSession('deleted-session');
const dots = api.dotStates();
console.log(JSON.stringify({{
  pruned,
  items: api.itemsForSession('deleted-session'),
  deletedDot: Object.prototype.hasOwnProperty.call(dots, 'deleted-session'),
  survivorDot: Object.prototype.hasOwnProperty.call(dots, 'survivor-session'),
  rawDeletedDot: Object.prototype.hasOwnProperty.call(window._sessionDotState, 'deleted-session'),
}}));
"""
    )

    assert observed == {
        "pruned": True,
        "items": {"subagents": [], "bg": []},
        "deletedDot": False,
        "survivorDot": True,
        "rawDeletedDot": False,
    }

    prune_body = _function_body(SESSION_STATUS_SRC, "function pruneSession(")
    for map_name in (
        "_sessionRowsById",
        "_subagentsBySession",
        "_bgProcsBySession",
        "_dismissedBySession",
    ):
        assert f"{map_name}.delete(sessionId)" in prune_body
    assert "delete window._sessionDotState[sessionId]" in prune_body
    assert "_recomputeSessionDotStates()" in prune_body

    delete_body = _function_body(SESSIONS_SRC, "async function deleteSession(")
    assert "window._sessionStatus" in delete_body
    assert "pruneSession(sid)" in delete_body
    assert SESSIONS_SRC.count("pruneSession(sid)") >= 2
