"""Regression coverage for async-delegation wakeup provenance and rendering (#7361)."""

import json
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

from api.process_event_utils import wakeup_display_meta
from api.streaming import _settle_current_turn_boundary


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"

SINGLE_BODY = """[ASYNC DELEGATION COMPLETE — deleg_single]
A background subagent you dispatched earlier has finished.
Status: completed   API calls: 3   Duration: 4.2s
--- RESULT ---
finished cleanly"""

BATCH_BODY = """[ASYNC DELEGATION BATCH COMPLETE — deleg_batch]
A background fan-out of 2 subagent(s) you dispatched earlier has finished.

--- ✓ TASK 1/2: inspect  (status=completed, api_calls=2, 1.0s) ---
done

--- ✗ TASK 2/2: verify  (status=failed, api_calls=1, 0.5s) ---
failed"""


def _identity(body: str, source: str = "process_wakeup") -> dict:
    return {
        "token": "stream-1:123",
        "turn_id": "turn-1",
        "agent_turn_boundary_resolved": True,
        "current_turn_user_idx": 0,
        "text": body,
        "source": source,
        "session_id": "child-session",
    }


def test_adopted_async_wakeup_keeps_source_and_display_metadata():
    result = [
        {"role": "user", "content": SINGLE_BODY},
        {"role": "assistant", "content": "acknowledged"},
    ]

    settled = _settle_current_turn_boundary([], result, _identity(SINGLE_BODY), SINGLE_BODY, "process_wakeup")

    assert settled[0]["_active_turn_token"] == "stream-1:123"
    assert settled[0]["_source"] == "process_wakeup"
    assert settled[0]["_wakeup_meta"] == {
        "type": "async_delegation",
        "task_id": "deleg_single",
        "batch": False,
    }


def test_adopted_fork_turn_keeps_regeneration_marker():
    body = "continue in the fork"
    result = [{"role": "user", "content": body}]

    settled = _settle_current_turn_boundary([], result, _identity(body, "fork"), body, "fork")

    assert settled[0]["_source"] == "fork"
    assert settled[0]["_fork_child_turn"] == "child-session"


def test_async_delegation_metadata_recognizes_single_and_batch_headers():
    assert wakeup_display_meta(SINGLE_BODY) == {
        "type": "async_delegation",
        "task_id": "deleg_single",
        "batch": False,
    }
    assert wakeup_display_meta(BATCH_BODY) == {
        "type": "async_delegation",
        "task_id": "deleg_batch",
        "batch": True,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_async_delegation_rows_render_as_collapsed_wakeups():
    driver = textwrap.dedent(
        f"""
        const fs=require('fs');
        const src=fs.readFileSync({json.dumps(str(UI_JS))},'utf8');
        function extractFunc(name){{
          const start=src.indexOf('function '+name);
          if(start<0) throw new Error(name+' missing');
          const brace=src.indexOf('{{',start);
          let depth=0;
          for(let i=brace;i<src.length;i++){{
            if(src[i]==='{{') depth++;
            else if(src[i]==='}}'){{depth--;if(depth===0) return src.slice(start,i+1);}}
          }}
          throw new Error(name+' unterminated');
        }}
        function msgContent(m){{return m&&m.content||'';}}
        function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
        function li(name){{return '<svg data-icon="'+name+'"></svg>';}}
        function t(key){{return key;}}
        eval(extractFunc('_parseProcessWakeupBody'));
        eval(extractFunc('_isProcessWakeupMessage'));
        eval(extractFunc('_processWakeupInfo'));
        eval(extractFunc('_processWakeupCardHtml'));
        const single={json.dumps(SINGLE_BODY)};
        const batch={json.dumps(BATCH_BODY)};
        const singleInfo=_processWakeupInfo({{}},single);
        const batchInfo=_processWakeupInfo({{}},batch);
        const extras={{timeHtml:'',filesHtml:'',footHtml:''}};
        console.log(JSON.stringify({{
          singleInfo,
          batchInfo,
          stamped:_isProcessWakeupMessage({{role:'user',content:'future shape',_source:'process_wakeup'}}),
          legacy:_isProcessWakeupMessage({{role:'user',content:single,_active_turn_token:'stream:1'}}),
          spoof:_isProcessWakeupMessage({{role:'user',content:single}}),
          singleCard:_processWakeupCardHtml(singleInfo,single,extras),
          batchCard:_processWakeupCardHtml(batchInfo,batch,extras),
        }}));
        """
    )
    result = subprocess.run(
        ["node", "-e", driver],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)

    assert payload["stamped"] is True
    assert payload["legacy"] is True
    assert payload["spoof"] is False
    assert payload["singleInfo"]["output"].startswith("A background subagent")
    assert payload["batchInfo"]["batch"] is True
    assert payload["singleCard"].startswith('<details class="process-wakeup-card">')
    assert "exit ?" not in payload["singleCard"]
    assert "deleg_single" in payload["singleCard"]
    assert "delegation" in payload["singleCard"]
    assert "deleg_batch" in payload["batchCard"]
    assert "batch" in payload["batchCard"]
