"""Regression coverage for the desktop live-session status port.

The tests intentionally exercise the landed Python and vanilla-JS behavior in
the same style as the other focused browser-logic regressions: Python asserts
wire contracts and source chokepoints, while small Node programs load or extract
the real frontend functions and report observable results as JSON.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
import tempfile
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
    """Extract a complete function body, including nested braces/functions."""
    start = source.find(signature)
    assert start != -1, f"missing {signature}"
    opening_brace = source.find("{", start)
    assert opening_brace != -1, f"missing opening brace for {signature}"
    depth = 0
    for index in range(opening_brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[opening_brace + 1 : index]
    raise AssertionError(f"could not extract complete function body for {signature}")


def _run_node_json(program: str) -> Any:
    assert NODE is not None
    with tempfile.TemporaryDirectory(prefix="hermes-status-node-") as directory:
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


_STATUS_NODE_PRELUDE = f"""
const statusModulePath = {json.dumps(str(SESSION_STATUS_JS_PATH))};
const timeoutCalls = [];
const intervalCalls = [];
globalThis.window = globalThis;
globalThis.setTimeout = (fn, delay) => {{
  timeoutCalls.push({{fn, delay}});
  return fn;
}};
globalThis.clearTimeout = () => undefined;
globalThis.setInterval = (fn, delay) => {{
  intervalCalls.push({{fn, delay}});
  return {{fn, delay}};
}};
globalThis.clearInterval = () => undefined;
function makeElement(tagName) {{
  const element = {{
    tagName,
    children: [],
    dataset: {{}},
    style: {{}},
    appendChild(child) {{
      this.children.push(child);
      return child;
    }},
    remove() {{
      this.removed = true;
    }},
  }};
  Object.defineProperty(element, 'innerHTML', {{
    get() {{ return ''; }},
    set(value) {{ if (value === '') this.children.length = 0; }},
  }});
  return element;
}}
globalThis.document = {{createElement: makeElement}};
require(statusModulePath);
"""


def test_bg_status_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """The v2 background frame carries process rows and fails closed."""
    from api.background_process import bg_status_for_session

    def list_sessions(*, session_key: str) -> list[dict[str, Any]]:
        assert session_key == "session-v2"
        return [
            {
                "session_id": "p-running",
                "command": "npm run dev\n# long command",
                "status": "running",
                "exit_code": None,
                "started_at": 1756800000.0,
            },
            {
                "session_id": "p-success",
                "command": "pytest tests/",
                "status": "exited",
                "exit_code": 0,
                "started_at": 1756800001.0,
                "output_preview": "",
            },
            {
                "session_id": "p-failed",
                "command": "npm run build",
                "status": "exited",
                "exit_code": 1,
                "started_at": 1756800002.0,
                "output_preview": "build failed",
            },
        ]

    registry_module = types.ModuleType("tools.process_registry")
    registry_module.process_registry = types.SimpleNamespace(  # type: ignore[attr-defined]
        list_sessions=list_sessions
    )
    monkeypatch.setitem(sys.modules, "tools.process_registry", registry_module)

    payload = bg_status_for_session("session-v2")

    assert "runs" not in payload
    assert "runs_total" not in payload
    assert payload == {
        "session_id": "session-v2",
        "active": True,
        "processes": [
            {
                "id": "p-running",
                "title": "npm run dev",
                "state": "running",
                "exit_code": None,
                "started_at": 1756800000.0,
            },
            {
                "id": "p-success",
                "title": "pytest tests/",
                "state": "done",
                "exit_code": 0,
                "started_at": 1756800001.0,
            },
            {
                "id": "p-failed",
                "title": "npm run build",
                "state": "failed",
                "exit_code": 1,
                "started_at": 1756800002.0,
                "output": "build failed",
            },
        ],
    }

    def unavailable(*, session_key: str) -> list[dict[str, Any]]:
        raise RuntimeError("registry unavailable")

    registry_module.process_registry = types.SimpleNamespace(  # type: ignore[attr-defined]
        list_sessions=unavailable
    )

    assert bg_status_for_session("session-v2") == {
        "session_id": "session-v2",
        "active": False,
        "processes": [],
    }


def test_session_dot_state() -> None:
    """Dot priority, spinner suppression, stalled refinement, and aliases."""
    observed = _run_node_json(
        _STATUS_NODE_PRELUDE
        + r"""
const api = window._sessionStatus;
const now = Date.now();
const rows = [
  {session_id: 'unread-working', unread: true, is_streaming: true},
  {session_id: 'bg-working', bg_active: true, pending_user_message: 'next turn'},
  {session_id: 'bg-only', bg_active: true},
  {session_id: 'attention-working', attention: true, is_streaming: true},
  {session_id: 'idle-stale', updated_at: now - 30000},
];
api.noteSessionRowsUpdated(rows, {session: {session_id: 'idle-stale'}, busy: false});
const nonWorkingStale = api.dotStates()['idle-stale'];
api.noteSessionRowsUpdated(
  rows.concat([{session_id: 'stalled-working', updated_at: now - 30000, is_streaming: true}]),
  {session: {session_id: 'stalled-working'}, busy: true},
);
api.noteSessionRowUpdated('child-session', 'lineage-tip', {bg_active: true});
const dots = api.dotStates();
const result = {};
for (const key of [
  'unread-working', 'bg-working', 'bg-only', 'attention-working',
  'stalled-working', 'child-session', 'lineage-tip',
]) {
  result[key] = {
    state: dots[key].state,
    bucket: dots[key].bucket,
    spinner: api.showsRunningArc(dots[key].state),
  };
}
result.nonWorkingStale = {state: nonWorkingStale.state, bucket: nonWorkingStale.bucket};
console.log(JSON.stringify(result));
"""
    )

    assert observed == {
        "unread-working": {"state": "working", "bucket": "working", "spinner": True},
        "bg-working": {"state": "working", "bucket": "working", "spinner": True},
        "bg-only": {"state": "background", "bucket": "working", "spinner": False},
        "attention-working": {
            "state": "needs-input",
            "bucket": "needs-input",
            "spinner": False,
        },
        "stalled-working": {"state": "stalled", "bucket": "working", "spinner": True},
        "child-session": {
            "state": "background",
            "bucket": "working",
            "spinner": False,
        },
        "lineage-tip": {
            "state": "background",
            "bucket": "working",
            "spinner": False,
        },
        "nonWorkingStale": {"state": "idle", "bucket": "idle"},
    }


def test_sidebar_no_runs_chip() -> None:
    """The sidebar paints background work as a dot, not a runs chip/spinner."""
    static_sources = [
        path
        for path in (REPO_ROOT / "static").rglob("*")
        if path.is_file() and path.suffix in {".js", ".css", ".html"}
    ]
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in static_sources
        if "sess-runs-chip" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == []

    render_body = _function_body(SESSIONS_SRC, "function _renderOneSession(")
    assert "dot.className='session-dot'" in render_body
    assert "dot.dataset.dotState=dotRecord.state" in render_body

    observed = _run_node_json(
        _STATUS_NODE_PRELUDE
        + "const sessionsSource = "
        + json.dumps(SESSIONS_SRC)
        + ";\n"
        + r"""
function extractFunction(name) {
  const signature = 'function ' + name + '(';
  const start = sessionsSource.indexOf(signature);
  if (start < 0) throw new Error('missing function ' + name);
  const openingBrace = sessionsSource.indexOf('{', start);
  let depth = 0;
  for (let index = openingBrace; index < sessionsSource.length; index++) {
    if (sessionsSource[index] === '{') depth++;
    else if (sessionsSource[index] === '}') {
      depth--;
      if (depth === 0) return sessionsSource.slice(openingBrace + 1, index);
    }
  }
  throw new Error('could not extract function ' + name);
}
let S = {session: {session_id: 'active-bg'}, busy: false};
eval('function _sidebarLineageKeyForRow(s){' + extractFunction('_sidebarLineageKeyForRow') + '}');
eval('function _sessionDotStateForRow(s){' + extractFunction('_sessionDotStateForRow') + '}');
eval('function _hasPendingUserMessageSignal(s){' + extractFunction('_hasPendingUserMessageSignal') + '}');
eval('function _isSessionLocallyStreaming(s){' + extractFunction('_isSessionLocallyStreaming') + '}');
eval('function _isSessionEffectivelyStreaming(s){' + extractFunction('_isSessionEffectivelyStreaming') + '}');
const row = {session_id: 'active-bg', bg_active: true, is_streaming: false};
window._sessionStatus.noteSessionRowUpdated(row.session_id, undefined, {bg_active: true});
const dot = _sessionDotStateForRow(row);
console.log(JSON.stringify({
  dotState: dot.state,
  bucket: dot.bucket,
  spinner: window._sessionStatus.showsRunningArc(dot.state),
  locallyStreaming: _isSessionLocallyStreaming(row),
  effectivelyStreaming: _isSessionEffectivelyStreaming(row),
}));
"""
    )

    assert observed == {
        "dotState": "background",
        "bucket": "working",
        "spinner": False,
        "locallyStreaming": False,
        "effectivelyStreaming": False,
    }


def test_session_list_render_signature() -> None:
    """A dot-map-only transition must invalidate the sidebar render skip."""
    signature_body = _function_body(SESSIONS_SRC, "function _sessionListRenderSignature(")
    assert "window._sessionDotState," in signature_body


def test_status_stack_rows() -> None:
    """Stack rows, terminal dismissal staging, and poll arming are deterministic."""
    # Timing caveat: we do not sleep for 4/12 seconds.  The Node harness replaces
    # timers, proves the exact terminal delays are staged, invokes their callbacks
    # synchronously, and observes dismissal.  It likewise records the 5s interval.
    assert "const SUCCESS_LINGER_MS = 4000;" in SESSION_STATUS_SRC
    assert "const FAILURE_LINGER_MS = 12000;" in SESSION_STATUS_SRC
    poll_body = _function_body(SESSION_STATUS_SRC, "function startStatusPoll(")
    render_body = _function_body(SESSION_STATUS_SRC, "function renderStatusStack(")
    assert "hasLiveStatusWork(_statusPollContext.sessionId)" in poll_body
    assert "hasLiveStatusWork(sid)" in render_body

    observed = _run_node_json(
        _STATUS_NODE_PRELUDE
        + r"""
const api = window._sessionStatus;
function makeRoot() {
  const root = document.createElement('div');
  root.querySelector = () => null;
  root.isConnected = true;
  return root;
}
function collectRows(node, output = []) {
  for (const child of node.children || []) {
    if (child.dataset && child.dataset.itemType) output.push(child);
    collectRows(child, output);
  }
  return output;
}
function rowSummary(row) {
  return {
    itemType: row.dataset.itemType,
    itemState: row.dataset.itemState,
    tool: (row.children.find(child => child.className === 'status-row-tool') || {}).textContent || null,
    exit: (row.children.find(child => child.className === 'status-row-exit') || {}).textContent || null,
  };
}
const toolRoot = makeRoot();
api.ingestSubagentFrame({
  name: 'subagent_progress',
  done: false,
  args: {
    subagent_id: 'sub-tool',
    goal: 'Research status behavior',
    status: 'running',
    current_tool: 'web_search',
    tool_preview: 'desktop dot',
    session_id: 'child-session',
  },
}, 'tool-session');
const toolCounts = api.renderStatusStack(toolRoot, 'tool-session');
const subagentRows = collectRows(toolRoot).map(rowSummary);

const terminalRoot = makeRoot();
api.ingestBgStatus({
  session_id: 'terminal-session',
  active: false,
  processes: [
    {id: 'success-process', title: 'Successful job', state: 'done', exit_code: 0, started_at: 1},
    {id: 'failed-process', title: 'Failed job', state: 'failed', exit_code: 1, started_at: 2, output: 'boom'},
  ],
}, 'terminal-session');
const terminalCounts = api.renderStatusStack(terminalRoot, 'terminal-session');
const terminalRows = collectRows(terminalRoot).map(rowSummary);
const terminalPollHandle = api.startStatusPoll();
const stagedDelays = timeoutCalls.map(call => call.delay).sort((a, b) => a - b);
timeoutCalls.forEach(call => call.fn());
const bgAfterDismiss = api.itemsForSession('terminal-session').bg;

const runningRoot = makeRoot();
api.ingestBgStatus({
  session_id: 'running-session',
  active: true,
  processes: [
    {id: 'running-process', title: 'Running job', state: 'running', exit_code: null, started_at: 3},
  ],
}, 'running-session');
const runningCounts = api.renderStatusStack(runningRoot, 'running-session');
const runningPollHandle = api.startStatusPoll();
console.log(JSON.stringify({
  toolCounts,
  subagentRows,
  terminalCounts,
  terminalRows,
  terminalPollHandle,
  stagedDelays,
  bgAfterDismiss,
  runningCounts,
  runningPollHandle: Boolean(runningPollHandle),
  intervalDelays: intervalCalls.map(call => call.delay),
}));
"""
    )

    assert observed == {
        "toolCounts": {"running": 1, "bgRunning": 0},
        "subagentRows": [
            {
                "itemType": "subagent",
                "itemState": "running",
                "tool": 'Web Search("desktop dot")',
                "exit": None,
            }
        ],
        "terminalCounts": {"running": 0, "bgRunning": 0},
        "terminalRows": [
            {
                "itemType": "background",
                "itemState": "done",
                "tool": None,
                "exit": None,
            },
            {
                "itemType": "background",
                "itemState": "failed",
                "tool": None,
                "exit": "exit 1",
            },
        ],
        "terminalPollHandle": None,
        "stagedDelays": [4000, 12000],
        "bgAfterDismiss": [],
        "runningCounts": {"running": 0, "bgRunning": 1},
        "runningPollHandle": True,
        "intervalDelays": [5000],
    }


def test_subagent_status_normalization() -> None:
    """Terminal subagent frames normalize safely and never keep spinning."""
    observed = _run_node_json(
        _STATUS_NODE_PRELUDE
        + r"""
const api = window._sessionStatus;
function ingest(id, status, name = 'subagent_progress') {
  return api.ingestSubagentFrame({
    name,
    done: true,
    args: {
      subagent_id: id,
      goal: 'Goal ' + id,
      status,
      current_tool: 'terminal_tool',
    },
  }, 'normalize-session');
}
console.log(JSON.stringify({
  timeout: ingest('timeout-agent', 'timeout'),
  error: ingest('error-agent', 'error'),
  cancelled: ingest('cancelled-agent', 'cancelled'),
  canceled: ingest('canceled-agent', 'canceled'),
  unknown: ingest('unknown-agent', 'wormhole'),
  delegateTaskName: ingest('delegate-agent', 'error', 'delegate_task'),
}));
"""
    )

    def terminal_projection(item: dict[str, Any]) -> dict[str, Any]:
        return {"status": item["status"], "currentTool": item.get("currentTool")}

    assert {
        name: terminal_projection(item) for name, item in observed.items()
    } == {
        "timeout": {"status": "failed", "currentTool": None},
        "error": {"status": "failed", "currentTool": None},
        "cancelled": {"status": "interrupted", "currentTool": None},
        "canceled": {"status": "interrupted", "currentTool": None},
        "unknown": {"status": "failed", "currentTool": None},
        "delegateTaskName": {"status": "failed", "currentTool": None},
    }


def test_streaming_poll_terminates() -> None:
    """Background-only work must not feed the semantic streaming predicate."""
    local_body = _function_body(SESSIONS_SRC, "function _isSessionLocallyStreaming(")
    effective_body = _function_body(SESSIONS_SRC, "function _isSessionEffectivelyStreaming(")
    assert "bg_active" not in local_body + effective_body
    assert "bgState" not in local_body + effective_body

    observed = _run_node_json(
        _STATUS_NODE_PRELUDE
        + "const sessionsSource = "
        + json.dumps(SESSIONS_SRC)
        + ";\n"
        + r"""
function extractFunction(name) {
  const signature = 'function ' + name + '(';
  const start = sessionsSource.indexOf(signature);
  if (start < 0) throw new Error('missing function ' + name);
  const openingBrace = sessionsSource.indexOf('{', start);
  let depth = 0;
  for (let index = openingBrace; index < sessionsSource.length; index++) {
    if (sessionsSource[index] === '{') depth++;
    else if (sessionsSource[index] === '}') {
      depth--;
      if (depth === 0) return sessionsSource.slice(openingBrace + 1, index);
    }
  }
  throw new Error('could not extract function ' + name);
}
let S = {session: {session_id: 'bg-only-active'}, busy: false};
eval('function _hasPendingUserMessageSignal(s){' + extractFunction('_hasPendingUserMessageSignal') + '}');
eval('function _isSessionLocallyStreaming(s){' + extractFunction('_isSessionLocallyStreaming') + '}');
eval('function _isSessionEffectivelyStreaming(s){' + extractFunction('_isSessionEffectivelyStreaming') + '}');
const row = {
  session_id: 'bg-only-active',
  bg_active: true,
  is_streaming: false,
  cron_running: false,
  pending_user_message: null,
  has_pending_user_message: false,
};
window._sessionStatus.noteSessionRowUpdated(row.session_id, undefined, {bg_active: true});
console.log(JSON.stringify({
  locallyStreaming: _isSessionLocallyStreaming(row),
  effectivelyStreaming: _isSessionEffectivelyStreaming(row),
  parallelPaintState: window._sessionStatus.dotStates()[row.session_id].state,
}));
"""
    )

    assert observed == {
        "locallyStreaming": False,
        "effectivelyStreaming": False,
        "parallelPaintState": "background",
    }
