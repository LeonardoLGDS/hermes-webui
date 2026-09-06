import json

import pytest

from api.routes import _message_window_for_display
from api.wsbound import WindowTooLarge, bounded_window


def test_older_join_preserves_hidden_gap_source_coordinates():
    messages = [{"role": "user", "content": "synthetic"}] + [
        {"role": "assistant", "content": "", "reasoning": "synthetic"}
        for index in range(40)
    ] + [{"role": "assistant", "content": "synthetic"} for index in range(500)]
    for index, row in enumerate(messages):
        row["source_id"] = index
    newer, offset = _message_window_for_display(messages, msg_limit=500)
    older, older_offset = _message_window_for_display(messages, msg_limit=30, msg_before=offset)
    joined = older + newer
    assert [row["source_id"] for row in joined] == list(range(older_offset, older_offset + len(joined)))


def test_long_prefix_keeps_immediately_matching_tool_result():
    messages = [{"role": "user", "content": "synthetic"} for index in range(499)] + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "synthetic-call"}]},
        {"role": "tool", "tool_call_id": "synthetic-call", "content": "synthetic-result"},
    ]
    page, offset = _message_window_for_display(messages, msg_limit=30)
    assert (offset, len(page)) == (470, 31)
    assert page[-1] == messages[-1]


@pytest.mark.parametrize("gap", [40, 550, 4200])
@pytest.mark.parametrize("budget", [2500, 1_572_864])
def test_multiple_byte_bounded_prepend_joins(gap, budget):
    messages = []
    for block in range(3):
        messages += [{"role": "user", "content": "reply" * 80} for index in range(35)]
        messages += [{"role": "assistant", "content": "", "reasoning": "hidden"} for index in range(gap)]
    messages += [{"role": "assistant", "content": "newest"}]
    for index, row in enumerate(messages):
        row["source_id"] = index
    original = json.dumps(messages)
    joined, offset = _message_window_for_display(messages, msg_limit=30)
    joined, offset = bounded_window(joined, offset, budget=budget)
    requests = 0
    while offset:
        older, older_offset = _message_window_for_display(messages, msg_limit=30, msg_before=offset)
        assert len(older) <= 500
        older, older_offset = bounded_window(older, older_offset, budget=budget)
        # WHY: mirror the existing guard's per-row encoding and comma charge,
        # rather than adding json.dumps' optional inter-row whitespace.
        assert 2 + sum(1 + len(json.dumps(row, ensure_ascii=False).encode()) for row in older) <= budget
        assert older_offset < offset
        assert older_offset + len(older) == offset
        joined = older + joined
        offset = older_offset
        assert all(row["source_id"] == offset + local_index for local_index, row in enumerate(joined))
        requests += 1
        assert requests <= len(messages)
    assert requests > 1
    assert joined == messages
    assert json.dumps(messages) == original


@pytest.mark.parametrize("limit,results", [(30, 2), (498, 2), (499, 1), (500, 0)])
def test_selected_tool_tail_obeys_row_cap(limit, results):
    messages = [{"role": "user", "content": "prefix"} for index in range(499)] + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "one"}, {"id": "two"}]},
        {"role": "tool", "tool_call_id": "one", "content": "first"},
        {"role": "tool", "tool_call_id": "two", "content": "second"},
        {"role": "tool", "tool_call_id": "orphan", "content": "orphan"},
    ]
    page, offset = _message_window_for_display(messages, msg_limit=limit)
    assert len(page) == limit + results <= 500
    assert sum(row["role"] == "tool" for row in page) == results
    assert page == messages[offset:offset + len(page)]
    if offset:
        older, older_offset = _message_window_for_display(messages, msg_limit=30, msg_before=offset)
        assert older_offset < offset == older_offset + len(older)


def test_unused_lookback_call_cannot_admit_orphan_result():
    messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": "unused"}]}] + [
        {"role": "user", "content": "prefix"} for index in range(499)
    ] + [{"role": "tool", "tool_call_id": "unused", "content": "orphan"}]
    page, offset = _message_window_for_display(messages, msg_limit=30)
    assert offset == 470
    assert all(row["role"] == "user" for row in page)


def test_matching_tool_tail_preserves_byte_refusal_and_suffix():
    messages = [{"role": "user", "content": "prefix" * 100} for index in range(499)] + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call"}]},
        {"role": "tool", "tool_call_id": "call", "content": "result" * 50},
    ]
    page, offset = _message_window_for_display(messages, msg_limit=30)
    bounded, bounded_offset = bounded_window(page, offset, budget=1000)
    assert bounded == messages[bounded_offset:]
    assert bounded[-2:] == messages[-2:]
    assert bounded_offset > offset
    with pytest.raises(WindowTooLarge):
        bounded_window(page, offset, budget=100)
