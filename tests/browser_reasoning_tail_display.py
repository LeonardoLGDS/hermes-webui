"""Read-only browser gate; private fixtures and artifacts stay outside Git."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

from browser_conversation_lifecycle import _start_webui_server, _terminate_process


def source_coordinates(page, messages):
    # WHY: cursor movement alone cannot detect a hidden gap shifting edit targets.
    # Compare browser-owned rows and DOM local indices to the original sidecar.
    # WHY: tool content is preview-clipped and may be secret-redacted by GETs;
    # its call ID/name/timestamp identify it instead. Editable reply content
    # must match exactly. Synthetic rows additionally have unique source IDs.
    return page.evaluate("""source => {
        const same = (row, original) => !!original && row.role === original.role &&
            (row.id || null) === (original.id || null) &&
            (row.timestamp || row._ts || null) === (original.timestamp || original._ts || null) &&
            (row.role === 'tool' ? row.tool_call_id === original.tool_call_id &&
                row.tool_name === original.tool_name && row.name === original.name :
                JSON.stringify(row.content) === JSON.stringify(original.content));
        const rows = S.messages || [];
        const nodes = [...document.querySelectorAll('[data-msg-idx]')];
        return {offset: _oldestIdx, rows: rows.length, indexedNodes: nodes.length,
            rowMismatches: rows.flatMap((row, index) => same(row, source[_oldestIdx + index]) ? [] : [{index,
                role: row.role === source[_oldestIdx + index]?.role,
                content: JSON.stringify(row.content) === JSON.stringify(source[_oldestIdx + index]?.content),
                id: (row.id || null) === (source[_oldestIdx + index]?.id || null)}]),
            invalidNodeIndices: nodes.map(node => Number(node.dataset.msgIdx)).filter(index => !rows[index]),
            valid: rows.every((row, index) => same(row, source[_oldestIdx + index])) &&
                nodes.every(node => {
                    const index = Number(node.dataset.msgIdx);
                    return Number.isInteger(index) && !!rows[index] &&
                        same(rows[index], source[_oldestIdx + index]);
                })};
    }""", messages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--session-id", default="ad46d8289337")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--expect-blank", action="store_true")
    parser.add_argument("--controlled-checks", action="store_true", help="Also exercise hidden-tail and active-run browser-memory invariants")
    parser.add_argument("--gap-checks", action="store_true", help="Also page a separate synthetic sidecar through real GET/rendering")
    parser.add_argument("--auth-env", help="Environment variable holding an existing Cookie header; never saved")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    args.artifacts.mkdir(parents=True, exist_ok=True)
    fixture_hash = hashlib.sha256(args.fixture.read_bytes()).hexdigest() if args.fixture else None
    proc = log = None
    with tempfile.TemporaryDirectory(prefix="display-gate-", dir=args.artifacts.parent) as temporary:
        root = Path(temporary)
        try:
            base_url = args.base_url
            if not base_url:
                assert args.fixture, "isolated candidate requires --fixture"
                state = root / "state"
                (root / "hermes" / "profiles" / "main").mkdir(parents=True)
                (root / "hermes" / "active_profile").write_text("main\n")
                (state / "sessions").mkdir(parents=True)
                shutil.copyfile(args.fixture, state / "sessions" / f"{args.session_id}.json")
                if args.gap_checks:
                    # WHY: a separate synthetic session forces ceiling pagination
                    # across several gaps without modifying the immutable fixture.
                    gap_rows = []
                    for block in range(3):
                        gap_rows.extend({"role": "user", "content": "R2 reply"} for index in range(35))
                        gap_rows.extend({"role": "assistant", "content": "", "reasoning": "hidden"} for index in range(40))
                    gap_rows.extend({"role": "assistant" if index % 2 else "user", "content": "R2 reply"} for index in range(500))
                    for index, row in enumerate(gap_rows):
                        row["id"] = f"r2-source-{index}"
                        row["timestamp"] = 1_700_000_000 + index
                        if row["content"]:
                            row["content"] = f"R2 source row {index}"
                    gap_session = {"session_id": "display-r2-gap", "title": "R2 synthetic gaps", "profile": "main",
                                   "messages": gap_rows, "context_messages": gap_rows}
                    (state / "sessions" / "display-r2-gap.json").write_text(json.dumps(gap_session))
                agent = root / "no-agent"
                agent.mkdir()
                (agent / "run_agent.py").write_text('"""No model runtime in display gate."""\n')
                guard = root / "guard"
                guard.mkdir()
                (guard / "sitecustomize.py").write_text(
                    'import sys\n'
                    'def guard(event, args):\n'
                    '    if event == "socket.connect" and isinstance(args[1], tuple):\n'
                    '        if args[1][0] not in ("127.0.0.1", "::1", "localhost"):\n'
                    '            raise RuntimeError("Display gate forbids outbound network")\n'
                    'sys.addaudithook(guard)\n'
                )
                env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
                env.update({
                    "HOME": str(root / "home"),
                    "PYTHONPATH": str(guard),
                    "HERMES_WEBUI_HOST": "127.0.0.1",
                    "HERMES_WEBUI_STATE_DIR": str(state),
                    "HERMES_HOME": str(root / "hermes"),
                    "HERMES_BASE_HOME": str(root / "hermes"),
                    "HERMES_CONFIG_PATH": str(root / "hermes" / "config.yaml"),
                    "HERMES_WEBUI_SKIP_ONBOARDING": "1",
                    "HERMES_WEBUI_AGENT_DIR": str(agent),
                    "HERMES_WEBUI_DEFAULT_WORKSPACE": str(root),
                })
                proc, log, _, base_url = _start_webui_server(Path(__file__).resolve().parents[1], env, root)
            results = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                for label, width, height in (("desktop", 1280, 800), ("mobile", 390, 844)):
                    context = browser.new_context(viewport={"width": width, "height": height}, base_url=base_url)
                    failures, errors, session_responses, blocked = [], [], [], []

                    def boundary(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        if parsed.netloc != urlsplit(base_url).netloc or request.method not in ("GET", "HEAD"):
                            blocked.append({"path": parsed.path, "method": request.method})
                            route.abort()
                            return
                        headers = dict(request.headers)
                        if args.auth_env:
                            headers["cookie"] = os.environ[args.auth_env]
                        route.continue_(headers=headers)

                    context.route("**/*", boundary)
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(type(error).__name__))
                    page.on("requestfailed", lambda request: failures.append("transport") if urlsplit(request.url).path == "/api/session" else None)

                    def response_seen(response):
                        if urlsplit(response.url).path == "/api/session":
                            if response.status != 200:
                                failures.append(response.status)
                            else:
                                payload = response.json().get("session", {})
                                session_responses.append({
                                    "bytes": len(response.body()),
                                    "rows": len(payload.get("messages", [])),
                                    "offset": payload.get("_messages_offset"),
                                    "total": payload.get("message_count"),
                                    "end": (payload.get("_messages_offset") or 0) + len(payload.get("messages", [])),
                                    "active": bool(payload.get("active_stream_id")),
                                    "before": parse_qs(urlsplit(response.url).query).get("msg_before", [None])[0],
                                })

                    page.on("response", response_seen)
                    page.goto("/", wait_until="domcontentloaded")
                    page.wait_for_function("() => typeof loadSession === 'function'")
                    page.wait_for_timeout(1000)
                    page.evaluate("() => { window._showThinking = false; }")
                    started = time.perf_counter()
                    page.evaluate("sid => loadSession(sid)", args.session_id)
                    load_seconds = time.perf_counter() - started
                    page.wait_for_timeout(1800)

                    def snapshot(stage):
                        stats = page.evaluate("""() => ({
                            user: [...document.querySelectorAll('[data-role="user"] .msg-body')].filter(el => el.getClientRects().length && el.innerText.trim()).length,
                            assistant: [...document.querySelectorAll('.assistant-turn .msg-body')].filter(el => el.getClientRects().length && el.innerText.trim()).length,
                            dividers: document.querySelectorAll('.msg-date-sep').length,
                            oldest: typeof _oldestIdx === 'number' ? _oldestIdx : null,
                            busy: S.busy,
                            active: !!(S.session && S.session.active_stream_id)
                        })""")
                        results.append({"viewport": label, "stage": stage, **stats})
                        if args.fixture:
                            identity = source_coordinates(page, json.loads(args.fixture.read_bytes())["messages"])
                            assert identity["valid"], f"Original fixture source-coordinate mismatch: {identity}"
                            results.append({"viewport": label, "stage": stage, "source_coordinates": identity})
                        page.screenshot(path=str(args.artifacts / f"{label}-{stage}.png"))
                        if args.expect_blank:
                            assert stats["user"] + stats["assistant"] == 0
                        else:
                            assert stats["user"] > 0 and stats["assistant"] > 0, "No readable user/assistant bodies"
                        return stats

                    initial = snapshot("initial")
                    results.append({"viewport": label, "initial_load_seconds": round(load_seconds, 3)})
                    assert initial["active"] == session_responses[-1]["active"]
                    if not args.expect_blank:
                        page.reload(wait_until="domcontentloaded")
                        page.wait_for_timeout(1800)
                        snapshot("reload")
                        page.evaluate("() => _loadOlderMessages()")
                        page.wait_for_timeout(1000)
                        older = snapshot("older")
                        assert older["oldest"] < initial["oldest"] or initial["oldest"] == 0
                        page.locator("#messages").hover()
                        page.mouse.wheel(0, -600)
                        page.wait_for_timeout(500)
                        page.locator("#scrollToBottomBtn").click()
                        page.wait_for_timeout(400)
                        snapshot("latest")
                        assert page.evaluate("() => _scrollPinned === true")
                    if args.controlled_checks:
                        assert args.fixture, "controlled checks require private fixture"
                        tail = json.loads(args.fixture.read_bytes())["messages"][-30:]
                        controlled = page.evaluate("""tail => {
                            const saved = {messages: S.messages, busy: S.busy, active: S.activeStreamId,
                                session: S.session.active_stream_id, tools: S.toolCalls};
                            try {
                                S.messages = tail;
                                S.toolCalls = [];
                                window._showThinking = false;
                                renderMessages();
                                const dividers = document.querySelectorAll('.msg-date-sep').length;
                                S.messages = saved.messages;
                                S.toolCalls = saved.tools;
                                S.busy = true;
                                S.activeStreamId = 'display-gate-controlled';
                                S.session.active_stream_id = 'display-gate-controlled';
                                renderMessages();
                                return {dividers, activePreserved: S.busy === true &&
                                    S.activeStreamId === 'display-gate-controlled' &&
                                    S.session.active_stream_id === 'display-gate-controlled'};
                            } finally {
                                S.messages = saved.messages;
                                S.toolCalls = saved.tools;
                                S.busy = saved.busy;
                                S.activeStreamId = saved.active;
                                S.session.active_stream_id = saved.session;
                                renderMessages();
                            }
                        }""", tail)
                        assert controlled["activePreserved"]
                        assert controlled["dividers"] > 0 if args.expect_blank else controlled["dividers"] == 0
                        results.append({"viewport": label, "controlled_renderer_only": controlled})
                    assert not errors and not failures, f"Browser/session failure: {errors}, {failures}"
                    assert session_responses and len(session_responses) <= 16
                    assert all(item["rows"] <= 500 and item["bytes"] <= 2_000_000 for item in session_responses)
                    assert sum(item["bytes"] for item in session_responses) <= 16 * 2_000_000
                    results.append({"viewport": label, "requests": list(session_responses), "errors": errors, "failed_sessions": failures, "blocked": blocked})
                    if args.gap_checks:
                        assert not args.base_url, "synthetic checks are isolated-server only"
                        gap_start = len(session_responses)
                        page.evaluate("() => loadSession('display-r2-gap')")
                        page.wait_for_function("() => S.session?.session_id === 'display-r2-gap' && S.messages.length > 0")
                        gap_metrics = []
                        for attempt in range(30):
                            identity = source_coordinates(page, gap_rows)
                            assert identity["valid"] and identity["indexedNodes"] > 0, f"Synthetic source-coordinate mismatch: {identity}"
                            gap_metrics.append(identity)
                            if identity["offset"] == 0:
                                break
                            page.evaluate("() => _loadOlderMessages()")
                            assert page.evaluate("() => _oldestIdx") < identity["offset"], "Synthetic cursor stalled"
                        assert gap_metrics[-1]["offset"] == 0
                        assert gap_metrics[-1]["rows"] == len(gap_rows)
                        assert sum(item["rows"] > 500 for item in gap_metrics) >= 2
                        page.screenshot(path=str(args.artifacts / f"{label}-gap-joined.png"))
                        results.append({"viewport": label, "synthetic_gap_joins": gap_metrics})
                        gap_requests = session_responses[gap_start:]
                        assert len(gap_requests) <= 40
                        assert sum(item["before"] is not None for item in gap_requests) >= 2
                        assert all(item["rows"] <= 500 and item["bytes"] <= 2_000_000 for item in gap_requests)
                        results.append({"viewport": label, "synthetic_gap_requests": gap_requests})
                        assert not errors and not failures
                    context.close()
                browser.close()
            (args.artifacts / "metrics.json").write_text(json.dumps(results, indent=2))
        finally:
            if proc:
                _terminate_process(proc)
            if log:
                log.close()
            if args.fixture:
                assert hashlib.sha256(args.fixture.read_bytes()).hexdigest() == fixture_hash
                if not args.base_url:
                    original = json.loads(args.fixture.read_bytes())
                    candidate = json.loads((state / "sessions" / f"{args.session_id}.json").read_bytes())
                    for key in ("messages", "context_messages", "active_stream_id"):
                        assert bool(candidate.get(key) == original.get(key)), f"Candidate changed {key}"
                    if args.gap_checks:
                        saved_gap = json.loads((state / "sessions" / "display-r2-gap.json").read_bytes())
                        assert saved_gap["messages"] == gap_rows
                        assert saved_gap["context_messages"] == gap_rows


if __name__ == "__main__":
    main()
