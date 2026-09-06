import hashlib
import json
import os
from pathlib import Path

import pytest

from api.routes import _message_window_for_display
from api.wsbound import bounded_window
import api.routes as routes


def test_recovered_reasoning_tail_does_not_hide_replies():
    messages = [
        {"role": "user", "content": "Compare the alternatives"},
        {"role": "assistant", "content": "Here is the comparison"},
    ] + [
        {"role": "assistant", "content": "", "reasoning": "private activity",
         "_recovered_from_run_journal": True, "_recovered_stream_id": str(index)}
        for index in range(200)
    ]
    before = json.dumps(messages)
    window, offset = _message_window_for_display(messages, msg_limit=30, expand_renderable=True)
    assert any(row.get("content") for row in window)
    assert bool(window == messages[offset:offset + len(window)])
    assert json.dumps(messages) == before


def test_private_fixture_initial_window():
    fixture = os.environ.get("DISPLAY_SESSION_FIXTURE")
    if not fixture:
        pytest.skip("private fixture supplied out of tree")
    raw = Path(fixture).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "fc2fe298f639c6cd029b3c08e3f9bf52fde3d6a8c8666255424a96df6a5acc6f"
    messages = json.loads(raw)["messages"]
    window, offset = _message_window_for_display(messages, msg_limit=30, expand_renderable=True)
    window, offset = bounded_window(window, offset)
    assert any(row.get("role") == "user" and row.get("content") for row in window)
    assert any(row.get("role") == "assistant" and row.get("content") for row in window)
    assert bool(window == messages[offset:offset + len(window)])


@pytest.mark.parametrize("content", ["", "(empty)", [{"type": "reasoning", "text": "activity"}]])
def test_reasoning_payload_shapes_do_not_consume_reply_budget(content):
    messages = [{"role": "user", "content": "question"}] + [
        {"role": "assistant", "content": content, "reasoning_content": "activity"}
        for _ in range(40)
    ]
    window, offset = _message_window_for_display(messages, msg_limit=5)
    assert offset == 0
    assert window == messages[:1]


def test_reasoning_with_attachment_or_tool_anchor_is_still_renderable():
    for payload in ({"attachments": ["image.png"]}, {"tool_calls": [{"id": "call"}]}):
        row = {"role": "assistant", "content": "", "reasoning": "activity", **payload}
        assert routes._message_counts_as_renderable_for_window(row)


def test_hidden_scan_and_raw_window_have_finite_budgets(monkeypatch):
    calls = 0
    original = routes._message_counts_as_renderable_for_window

    def counted(message):
        nonlocal calls
        calls += 1
        return original(message)

    monkeypatch.setattr(routes, "_message_counts_as_renderable_for_window", counted)
    messages = [{"role": "assistant", "content": "", "reasoning": "activity"}] * 20_000
    window, offset = _message_window_for_display(messages, msg_limit=30)
    assert calls == routes._DISPLAY_WINDOW_SCAN_LIMIT
    assert offset == len(messages) - 30
    assert len(window) == 30
    messages = [{"role": "user", "content": "start"}] + [
        {"role": "tool", "content": "result"} for _ in range(1500)
    ] + [{"role": "assistant", "content": "end"}]
    window, offset = _message_window_for_display(messages, msg_limit=30)
    assert len(window) <= routes._DISPLAY_WINDOW_ROW_LIMIT
    assert window == messages[offset:offset + len(window)]
    older, older_offset = _message_window_for_display(messages, msg_limit=30, msg_before=offset)
    assert older_offset < offset
    assert older == messages[older_offset:older_offset + len(older)]


def test_byte_bound_keeps_original_coordinates():
    messages = [{"role": "assistant", "content": "reply" * 1000} for _ in range(50)]
    window, offset = _message_window_for_display(messages, msg_limit=30)
    window, offset = bounded_window(window, offset, budget=20_000)
    assert len(json.dumps(window).encode()) < 20_000
    assert window == messages[offset:offset + len(window)]
