"""Sidebar recency selection with recovered stream-anchor rows."""

import json

import pytest

import api.models as models
from api.models import Session, _last_message_timestamp


@pytest.fixture(autouse=True)
def _isolate_session_index(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    if hasattr(models, "_PERSISTED_SESSION_IDS_CACHE"):
        models._PERSISTED_SESSION_IDS_CACHE = (None, None, frozenset())

    yield session_dir, session_dir / "_index.json"

    models.SESSIONS.clear()
    if hasattr(models, "_PERSISTED_SESSION_IDS_CACHE"):
        models._PERSISTED_SESSION_IDS_CACHE = (None, None, frozenset())


def _real_message_then_empty_recovered_anchors(*, anchor_count=12):
    """Model the recovered sidecar tail that produced the stale sidebar date."""
    stale_stream_start = 1000.0
    messages = [
        {"role": "user", "content": "current question", "timestamp": 2000.0},
        {"role": "assistant", "content": "current answer", "timestamp": 2100.0},
    ]
    messages.extend(
        {
            "role": "assistant",
            "content": "",
            "_recovered_from_run_journal": True,
            "timestamp": stale_stream_start,
        }
        for _ in range(anchor_count)
    )
    return messages


def test_trailing_empty_recovered_anchors_do_not_override_real_assistant():
    messages = _real_message_then_empty_recovered_anchors()

    assert _last_message_timestamp(messages) == 2100.0


def test_all_anchor_tail_falls_back_to_newest_eligible_real_message():
    messages = [
        {"role": "assistant", "content": "older answer", "timestamp": 1700.0},
        {"role": "user", "content": "newest activity", "timestamp": 1800.0},
    ]
    messages.extend(
        {
            "role": "assistant",
            "content": "   ",
            "_recovered_from_run_journal": True,
            "timestamp": 1000.0,
        }
        for _ in range(12)
    )

    assert _last_message_timestamp(messages, tail_window=4) == 1800.0


def test_nonempty_recovered_assistant_remains_eligible():
    messages = [
        {"role": "assistant", "content": "current answer", "timestamp": 2100.0},
        {
            "role": "assistant",
            "content": "recovered answer",
            "_recovered_from_run_journal": True,
            "timestamp": 2200.0,
        },
    ]

    assert _last_message_timestamp(messages) == 2200.0


def test_normal_empty_assistant_preserves_existing_last_message_behavior():
    messages = [
        {"role": "assistant", "content": "real answer", "timestamp": 2100.0},
        {"role": "assistant", "content": "", "timestamp": 2200.0},
    ]

    assert _last_message_timestamp(messages) == 2200.0


def test_no_eligible_messages_preserves_none_and_compact_fallback():
    messages = [{"role": "tool", "content": "result", "timestamp": 1000.0}]

    assert _last_message_timestamp(messages) is None
    assert Session(session_id="fallback", updated_at=300.0, messages=messages).compact()[
        "last_message_at"
    ] == 300.0


def test_compact_and_index_use_real_message_timestamp_after_anchor_tail(tmp_path):
    messages = _real_message_then_empty_recovered_anchors()
    session = Session(
        session_id="recovered_anchor_tail",
        updated_at=4000.0,
        messages=messages,
    )

    assert session.compact()["last_message_at"] == 2100.0
    session.save(touch_updated_at=False)
    index = json.loads((tmp_path / "sessions" / "_index.json").read_text(encoding="utf-8"))
    entry = next(row for row in index if row["session_id"] == session.session_id)

    assert entry["message_count"] == len(messages)
    assert entry["last_message_at"] == 2100.0
