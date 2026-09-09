"""Hermetic R61 durable sidebar-count regressions."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

import api.models as models
from api.models import Session


@pytest.fixture(autouse=True)
def isolate_sidebar_store(tmp_path, monkeypatch):
    """Keep every sidecar, index, and state.db in this test's tmp_path."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    state_db = tmp_path / "state.db"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(models, "SESSIONS", {})
    monkeypatch.setattr(
        models,
        "_PERSISTED_SESSION_IDS_CACHE",
        (None, None, frozenset()),
    )
    models._SIDECAR_METADATA_CACHE.clear()
    models._LEGACY_SIDECAR_FACTS.clear()

    yield session_dir, session_dir / "_index.json", state_db

    models._SIDECAR_METADATA_CACHE.clear()
    models._LEGACY_SIDECAR_FACTS.clear()


@pytest.fixture
def session_dir(isolate_sidebar_store):
    return isolate_sidebar_store[0]


@pytest.fixture
def index_file(isolate_sidebar_store):
    return isolate_sidebar_store[1]


@pytest.fixture
def state_db(isolate_sidebar_store):
    return isolate_sidebar_store[2]


def _message(i, role="user"):
    return {
        "role": role,
        "content": f"hermetic message {i}",
        "timestamp": 1_000.0 + i,
    }


def _write_sidecar(
    session_dir,
    sid,
    *,
    messages,
    message_count=None,
    title="Durable sidebar",
    truncation_watermark=None,
    include_message_count=True,
    **extra,
):
    count = len(messages) if message_count is None else max(0, len(messages))
    payload = {
        "session_id": sid,
        "title": title,
        "workspace": "/tmp/hermetic-r62-workspace",
        "created_at": 900.0,
        "updated_at": 1_000.0 + max(0, count),
        "anchor_scene_index": {},
        "messages": messages,
        "truncation_watermark": truncation_watermark,
        **extra,
    }
    if include_message_count:
        payload["message_count"] = len(messages) if message_count is None else message_count
    path = session_dir / f"{sid}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _make_state_db(path, sid, count):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    for i in range(count):
        conn.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (
                sid,
                "user" if i % 2 == 0 else "assistant",
                f"hermetic db message {i}",
                1_000.0 + i,
            ),
        )
    conn.commit()
    conn.close()


def _make_tool_projection_state_db(path, sid):
    """Seed active/inactive user, assistant, tool, and tool-only DB rows."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, timestamp REAL, active INTEGER, "
        "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, api_content TEXT)"
    )
    rows = [
        (sid, "user", "inspect durable replay", 1_000.0, 1, None, None, None, "provider user"),
        (sid, "assistant", "archived non-replayable answer", 1_000.5, 0, None, None, None, None),
        (
            sid,
            "assistant",
            "calling search fixture",
            1_002.0,
            1,
            None,
            json.dumps([{"id": "call-search", "type": "function", "function": {"name": "search"}}]),
            None,
            "provider assistant tool call",
        ),
        (
            sid,
            "tool",
            json.dumps({"results": ["identity-preserving-result"]}),
            1_003.0,
            1,
            "call-search",
            None,
            "search",
            "provider tool result",
        ),
        (sid, "assistant", "final replay answer", 1_004.0, 1, None, None, None, "provider final"),
        (sid, "user", "inactive tail must not replay", 1_005.0, 0, None, None, None, None),
    ]
    conn.executemany(
        "INSERT INTO messages("
        "session_id, role, content, timestamp, active, tool_call_id, tool_calls, tool_name, api_content"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    tool_only_sid = f"{sid}_tools_only"
    conn.executemany(
        "INSERT INTO messages("
        "session_id, role, content, timestamp, active, tool_call_id, tool_calls, tool_name, api_content"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (tool_only_sid, "tool", "internal result one", 1_100.0, 1, "call-one", None, "fs", "provider one"),
            (tool_only_sid, "tool", "internal result two", 1_101.0, 1, "call-two", None, "shell", "provider two"),
            (tool_only_sid, "tool", "inactive internal result", 1_102.0, 0, "call-old", None, "old", None),
        ],
    )
    conn.commit()
    conn.close()


def _make_partial_recovery_state_db(path, sid, *, first_row_id=31):
    """Seed only the durable DB tail behind a real two-message sidecar prefix."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
        "timestamp REAL, active INTEGER, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT, api_content TEXT)"
    )
    conn.executemany(
        "INSERT INTO messages("
        "id, session_id, role, content, timestamp, active, tool_call_id, tool_calls, "
        "tool_name, api_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (first_row_id, sid, "user", "recovered tail user", 1_002.0, 1, None, None, None, "provider recovered user"),
            (
                first_row_id + 1,
                sid,
                "assistant",
                "calling recovered search",
                1_003.0,
                1,
                None,
                json.dumps([{"id": "call-recovered", "type": "function", "function": {"name": "search"}}]),
                None,
                "provider recovered call",
            ),
            (
                first_row_id + 2,
                sid,
                "tool",
                json.dumps({"answer": "exact recovered tool result"}),
                1_004.0,
                1,
                "call-recovered",
                None,
                "search",
                "provider recovered tool",
            ),
            (first_row_id + 3, sid, "assistant", "exact recovered final answer", 1_005.0, 1, None, None, None, "provider recovered final"),
        ],
    )
    conn.commit()
    conn.close()


def _sidecar_prefix_messages():
    return [
        {
            "id": "sidecar-user-1",
            "role": "user",
            "content": "inspect durable replay",
            "timestamp": 1_000.0,
        },
        {
            "id": "sidecar-assistant-1",
            "role": "assistant",
            "content": "sidecar checkpoint",
            "timestamp": 1_001.0,
        },
    ]


def _expected_recovered_sequence(*, first_row_id=31):
    return [
        ("sidecar-user-1", "user", "inspect durable replay", None, None, None),
        ("sidecar-assistant-1", "assistant", "sidecar checkpoint", None, None, None),
        (first_row_id, "user", "recovered tail user", None, None, None),
        (
            first_row_id + 1,
            "assistant",
            "calling recovered search",
            None,
            [{"id": "call-recovered", "type": "function", "function": {"name": "search"}}],
            None,
        ),
        (
            first_row_id + 2,
            "tool",
            json.dumps({"answer": "exact recovered tool result"}),
            "call-recovered",
            None,
            "search",
        ),
        (first_row_id + 3, "assistant", "exact recovered final answer", None, None, None),
    ]


def _message_projection(message):
    return (
        message.get("id") or message.get("_state_db_row_id"),
        message.get("role"),
        message.get("content"),
        message.get("tool_call_id"),
        message.get("tool_calls"),
        message.get("name"),
    )


def _raw_db_message_count(path, sid):
    conn = sqlite3.connect(path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _write_index(path, entries):
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _read_index(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _index_row(sid, *, message_count, last_message_at=None, title="Indexed"):
    row = {
        "session_id": sid,
        "title": title,
        "profile": "default",
        "updated_at": 1_100.0,
        "message_count": message_count,
        "created_at": 900.0,
        "pinned": False,
        "archived": False,
    }
    if last_message_at is not None:
        row["last_message_at"] = last_message_at
    return row


def test_compact_prefers_loaded_nonempty_sidecar_over_zero_metadata():
    session = Session(session_id="loaded_sidecar", messages=[_message(0)])
    session._metadata_message_count = 0

    assert session.compact()["message_count"] == 1


def test_compact_retains_positive_metadata_and_keeps_new_sessions_empty():
    trimmed = Session(session_id="trimmed_metadata")
    trimmed._metadata_message_count = 7
    genuine_empty = Session(session_id="genuine_empty")

    assert trimmed.compact()["message_count"] == 7
    assert genuine_empty.compact()["message_count"] == 0


def test_metadata_only_absent_count_scans_real_sidecar_without_full_load(session_dir, monkeypatch):
    sid = "metadata_absent_count"
    _write_sidecar(
        session_dir,
        sid,
        messages=[_message(i) for i in range(4)],
        include_message_count=False,
    )
    monkeypatch.setattr(
        models.Session,
        "load",
        classmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full load used"))),
    )

    loaded = models.Session.load_metadata_only(sid)

    assert loaded._loaded_metadata_only is True
    assert loaded._metadata_message_count is None
    assert loaded.compact()["message_count"] == 4


def test_metadata_only_malformed_count_scans_real_sidecar_without_db(session_dir, monkeypatch):
    sid = "metadata_malformed_count"
    _write_sidecar(
        session_dir,
        sid,
        messages=[_message(i) for i in range(3)],
        message_count="not-a-number",
    )

    def fail_db(*_args, **_kwargs):
        raise AssertionError("state.db consulted by durable sidecar count")

    monkeypatch.setattr(models, "get_state_db_session_summary", fail_db)
    monkeypatch.setattr(models, "get_state_db_session_messages", fail_db)
    loaded = models.Session.load_metadata_only(sid)

    assert loaded._loaded_metadata_only is True
    assert loaded._metadata_message_count is None
    assert loaded.compact()["message_count"] == 3


def test_metadata_only_zero_header_is_repaired_by_structural_sidecar_count(session_dir):
    sid = "metadata_zero_count"
    _write_sidecar(
        session_dir,
        sid,
        messages=[_message(i) for i in range(2)],
        message_count=0,
    )

    loaded = models.Session.load_metadata_only(sid)

    assert loaded.compact()["message_count"] == 2


def test_genuine_empty_sidecar_without_count_scans_to_zero(session_dir):
    sid = "genuine_empty_absent_count"
    _write_sidecar(session_dir, sid, messages=[], include_message_count=False)

    loaded = models.Session.load_metadata_only(sid)

    assert loaded._loaded_metadata_only is True
    assert loaded._metadata_message_count is None
    assert loaded.compact()["message_count"] == 0


def test_bounded_scanner_fails_closed_when_array_close_is_unread(session_dir):
    sid = "bounded_open_array_proof"
    # Keep the first element itself incomplete at the fixed prefix boundary.
    huge_value = "x" * (models._SIDECAR_COUNT_SCAN_PREFIX_BYTES - 32)
    payload = {
        "session_id": sid,
        "title": "Bounded proof",
        "created_at": 1.0,
        "updated_at": 2.0,
        "messages": [
            {"role": "user", "content": huge_value, "timestamp": 1.0},
            {"role": "assistant", "content": "unread tail", "timestamp": 2.0},
        ],
    }
    sidecar = session_dir / f"{sid}.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    assert models._scan_sidecar_message_count(sidecar) is None


def test_scanner_rejects_malformed_first_value_and_duplicate_messages_member(session_dir):
    invalid_first = session_dir / "invalid_first_value.json"
    invalid_first.write_text(
        '{"session_id":"x","messages":[xxxxxxxxxxxxxxx', encoding="utf-8"
    )
    assert models._scan_sidecar_message_count(invalid_first, max_prefix_bytes=16) is None

    duplicate_messages = session_dir / "duplicate_messages.json"
    duplicate_messages.write_text(
        # Python would collapse duplicate dictionary keys before serialization,
        # so write the ambiguous JSON bytes literally to exercise the scanner.
        '{"session_id":"duplicate",'
        '"message_count":1,'
        '"messages":[{"role":"user","content":"first"}],'
        '"anchor_scene_index":{},'
        '"messages":[]}',
        encoding="utf-8",
    )
    assert models._scan_sidecar_message_count(duplicate_messages) is None


def test_eight_mib_boundary_after_complete_element_does_not_overcount(session_dir):
    budget = models._SIDECAR_COUNT_SCAN_PREFIX_BYTES
    element_before = '{"messages":[{"content":"'
    element_after = '","role":"user"}'
    padding = budget - len(element_before.encode()) - len(element_after.encode())
    assert padding > 0
    # The fixed prefix ends immediately after the sole complete element.  The
    # array close and a valid trailing member remain unread, so one complete
    # element must not be promoted to a count of two (or to an exact floor).
    prefix = element_before + ("x" * padding) + element_after
    assert len(prefix.encode()) == budget
    payload = prefix + '],"trailing_metadata":"' + ("y" * budget) + '"}'

    sidecar = session_dir / "complete_element_prefix_boundary.json"
    sidecar.write_text(payload, encoding="utf-8")

    assert sidecar.stat().st_size > budget
    assert len(json.loads(sidecar.read_text(encoding="utf-8"))["messages"]) == 1
    assert models._scan_sidecar_message_count(sidecar) is None


def test_scanner_keeps_exact_count_when_only_trailing_metadata_is_truncated(session_dir):
    sidecar = session_dir / "truncated_trailing_metadata.json"
    sidecar.write_text(
        '{"session_id":"trailing",'
        '"messages":[{"role":"user"},{"role":"assistant"}],'
        '"anchor_activity_scenes":{"huge":"'
        + "x" * 256
        + '"}}',
        encoding="utf-8",
    )

    assert models._scan_sidecar_message_count(sidecar, max_prefix_bytes=128) == 2


def test_unusable_sidecar_header_outranks_stale_index_with_structural_count(
    session_dir, monkeypatch
):
    cases = {
        "metadata_malformed_stale_index": "not-a-number",
        "metadata_absent_stale_index": None,
    }
    _write_index(
        session_dir / "_index.json",
        [_index_row(sid, message_count=9, last_message_at=1_009.0) for sid in cases],
    )
    monkeypatch.setattr(
        models.Session,
        "load",
        classmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full load used"))),
    )

    for sid, header_count in cases.items():
        if header_count is None:
            _write_sidecar(
                session_dir,
                sid,
                messages=[_message(i) for i in range(3)],
                include_message_count=False,
            )
        else:
            _write_sidecar(
                session_dir,
                sid,
                messages=[_message(i) for i in range(3)],
                message_count=header_count,
            )

        loaded = models.Session.load_metadata_only(sid)
        assert loaded._loaded_metadata_only is True
        assert loaded._sidecar_structural_message_count == 3
        assert loaded._metadata_message_count is None
        assert loaded.compact()["message_count"] == 3


def test_state_db_projection_filters_inactive_and_preserves_tool_identity(state_db):
    sid = "tool_projection"
    tool_only_sid = f"{sid}_tools_only"
    _make_tool_projection_state_db(state_db, sid)

    mixed = models.get_state_db_session_messages(sid)
    tool_only = models.get_state_db_session_messages(tool_only_sid)

    assert _raw_db_message_count(state_db, sid) == 6
    assert len(mixed) == 4
    assert [message["role"] for message in mixed] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert mixed[1]["tool_calls"] == [
        {"id": "call-search", "type": "function", "function": {"name": "search"}}
    ]
    assert (mixed[2]["tool_call_id"], mixed[2]["tool_name"], mixed[2]["name"]) == (
        "call-search",
        "search",
        "search",
    )

    assert _raw_db_message_count(state_db, tool_only_sid) == 3
    assert [(m["_state_db_row_id"], m["content"]) for m in tool_only] == [
        (7, "internal result one"),
        (8, "internal result two"),
    ]


def test_materializer_projects_tool_and_mixed_rows_without_raw_db_count(
    session_dir, index_file, state_db
):
    sid = "tool_replay"
    _write_sidecar(
        session_dir,
        sid,
        messages=_sidecar_prefix_messages(),
        include_message_count=False,
    )
    _make_tool_projection_state_db(state_db, sid)
    anchor = "tool_anchor"
    _write_sidecar(session_dir, anchor, messages=[_message(0)])
    _write_index(index_file, [_index_row(anchor, message_count=1, last_message_at=1_000.0)])

    materialized = models._materialize_index_update_from_state_db(
        models.Session.load_metadata_only(sid)
    )

    expected = [
        ("sidecar-user-1", "user", "inspect durable replay", None, None, None),
        ("sidecar-assistant-1", "assistant", "sidecar checkpoint", None, None, None),
        (3, "assistant", "calling search fixture", None, [
            {"id": "call-search", "type": "function", "function": {"name": "search"}}
        ], None),
        (4, "tool", json.dumps({"results": ["identity-preserving-result"]}), "call-search", None, "search"),
        (5, "assistant", "final replay answer", None, None, None),
    ]
    assert [_message_projection(m) for m in materialized.messages] == expected
    assert materialized.compact()["message_count"] == 5
    sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert [_message_projection(m) for m in sidecar["messages"]] == expected
    persisted = {row["session_id"]: row for row in _read_index(index_file)}
    assert persisted[sid]["message_count"] == 5


def test_load_repairs_bad_legacy_metadata_from_nonempty_sidecar(session_dir):
    sid = "legacy_bad_count"
    _write_sidecar(session_dir, sid, messages=[_message(i) for i in range(3)])
    path = session_dir / f"{sid}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["message_count"] = 0
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    loaded = Session.load(sid)

    assert loaded._metadata_message_count == 0
    assert loaded.compact()["message_count"] == 3


def test_ordinary_save_never_persists_zero_for_nonempty_sidecar(session_dir, index_file):
    sid = "ordinary_save"
    _write_sidecar(session_dir, sid, messages=[_message(i) for i in range(3)])
    # Recreate the recovery failure: durable sidecar bytes are non-empty, but the
    # loaded metadata snapshot says zero.
    path = session_dir / f"{sid}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["message_count"] = 0
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_index(index_file, [])

    Session.load(sid).save(touch_updated_at=False)

    persisted = {row["session_id"]: row for row in _read_index(index_file)}
    assert persisted[sid]["message_count"] == 3
    assert json.loads(path.read_text(encoding="utf-8"))["message_count"] == 3


def test_legacy_backfill_reconciles_permitted_db_tail_before_write(
    session_dir, index_file, state_db
):
    sid = "legacy_backfill"
    _write_sidecar(session_dir, sid, messages=[], message_count=3)
    _make_state_db(state_db, sid, 5)
    # The missing last_message_at exercises the startup bulk backfill caller.
    _write_index(index_file, [_index_row(sid, message_count=3, last_message_at=None)])

    rows = models.all_sessions()

    assert next(row for row in rows if row["session_id"] == sid)["message_count"] == 5
    persisted = {row["session_id"]: row for row in _read_index(index_file)}
    assert persisted[sid]["message_count"] == 5
    sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert len(sidecar["messages"]) == 5
    assert sidecar["message_count"] == 5


def test_recovery_count_never_drifts_down_to_a_smaller_db_snapshot(
    session_dir, index_file, state_db
):
    sid = "no_downward_drift"
    _write_sidecar(session_dir, sid, messages=[], message_count=7)
    _make_state_db(state_db, sid, 3)
    _write_index(index_file, [_index_row(sid, message_count=7, last_message_at=None)])

    rows = models.all_sessions()

    assert next(row for row in rows if row["session_id"] == sid)["message_count"] == 7
    persisted = {row["session_id"]: row for row in _read_index(index_file)}
    assert persisted[sid]["message_count"] == 7
    sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    # The smaller DB snapshot is not materialized over the larger known count.
    assert sidecar["message_count"] == 7
    assert sidecar["messages"] == []


def test_missing_index_recovery_materializes_db_tail_before_bulk_write(
    session_dir, index_file, state_db
):
    sid = "missing_index_recovery"
    anchor = "anchor_session"
    _write_sidecar(session_dir, sid, messages=[], message_count=0)
    _write_sidecar(session_dir, anchor, messages=[_message(0)])
    _make_state_db(state_db, sid, 4)
    # Keep one valid indexed row so all_sessions enters its missing-sidecar path
    # rather than treating an empty index as a rebuild condition.
    _write_index(index_file, [_index_row(anchor, message_count=1, last_message_at=1_000.0)])

    rows = models.all_sessions()

    by_id = {row["session_id"]: row for row in rows}
    assert by_id[sid]["message_count"] == 4
    persisted = {row["session_id"]: row for row in _read_index(index_file)}
    assert persisted[sid]["message_count"] == 4
    sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert len(sidecar["messages"]) == 4
    assert sidecar["message_count"] == 4


def test_truncation_watermark_blocks_replay_and_remains_sidecar_authoritative(
    session_dir, state_db
):
    sid = "truncated_to_empty"
    _write_sidecar(
        session_dir,
        sid,
        messages=[],
        message_count=0,
        truncation_watermark=0.0,
    )
    _make_state_db(state_db, sid, 5)
    before = (session_dir / f"{sid}.json").read_text(encoding="utf-8")

    materialized = models._materialize_index_update_from_state_db(
        Session.load_metadata_only(sid)
    )

    assert materialized.messages == []
    assert materialized.compact()["message_count"] == 0
    assert (session_dir / f"{sid}.json").read_text(encoding="utf-8") == before


def test_api_session_pipeline_shows_repaired_row_and_hides_genuine_empty(
    session_dir, index_file, state_db
):
    repaired = "api_repaired"
    empty = "api_genuine_empty"
    anchor = "api_anchor"
    _write_sidecar(session_dir, repaired, messages=[], message_count=0)
    _write_sidecar(
        session_dir,
        empty,
        messages=[],
        message_count=0,
        title="Untitled",
    )
    _write_sidecar(session_dir, anchor, messages=[_message(0)])
    _make_state_db(state_db, repaired, 2)
    _write_index(index_file, [_index_row(anchor, message_count=1, last_message_at=1_000.0)])

    rows = models.all_sessions()

    by_id = {row["session_id"]: row for row in rows}
    assert by_id[repaired]["message_count"] == 2
    assert empty not in by_id


def test_concurrent_incremental_index_writers_never_tear_or_lose_rows(
    session_dir, index_file
):
    writer_count = 24
    for index in range(writer_count):
        messages = [_message(i) for i in range(index + 1)]
        _write_sidecar(
            session_dir,
            f"atomic_writer_{index:02d}",
            messages=messages,
        )
    sessions = [
        Session(
            session_id=f"atomic_writer_{index:02d}",
            title=f"Atomic writer {index}",
            created_at=900.0 + index,
            updated_at=1_000.0 + index,
                messages=[_message(i) for i in range(index + 1)],
        )
        for index in range(writer_count)
    ]
    _write_index(index_file, [])
    barrier = threading.Barrier(writer_count)

    def writer(session):
        barrier.wait()
        models._write_session_index(updates=[session])

    with ThreadPoolExecutor(max_workers=writer_count) as pool:
        futures = [pool.submit(writer, session) for session in sessions]
        for future in as_completed(futures):
            assert future.result() is None

    persisted = _read_index(index_file)
    assert len(persisted) == writer_count
    assert {row["session_id"] for row in persisted} == {
        session.session_id for session in sessions
    }
    by_id = {row["session_id"]: row for row in persisted}
    for index, session in enumerate(sessions):
        # WHY: a zero overwrite is the durable-sidebar disappearance failure R65
        # required this race to prove cannot happen behind serialized writers.
        assert by_id[session.session_id]["message_count"] == index + 1
    assert not list(session_dir.glob("_index.json.tmp*"))


def test_backfill_preserves_exact_partial_sidecar_and_db_tail_identity(
    session_dir, index_file, state_db
):
    sid = "exact_backfill"
    _write_sidecar(
        session_dir,
        sid,
        messages=_sidecar_prefix_messages(),
        message_count=2,
        updated_at=1_001.0,
    )
    _make_partial_recovery_state_db(state_db, sid)
    _write_index(index_file, [_index_row(sid, message_count=2, last_message_at=None)])

    rows = models.all_sessions()

    persisted_sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert [_message_projection(m) for m in persisted_sidecar["messages"]] == (
        _expected_recovered_sequence()
    )
    assert persisted_sidecar["message_count"] == 6
    index_row = {row["session_id"]: row for row in _read_index(index_file)}[sid]
    assert index_row["message_count"] == 6
    assert index_row["last_message_at"] == 1_005.0
    assert next(row for row in rows if row["session_id"] == sid)["message_count"] == 6


def test_missing_index_recovery_preserves_exact_partial_transcript_order(
    session_dir, index_file, state_db
):
    sid = "exact_missing_index"
    anchor = "exact_recovery_anchor"
    _write_sidecar(
        session_dir,
        sid,
        messages=_sidecar_prefix_messages(),
        include_message_count=False,
        updated_at=1_001.0,
    )
    _make_partial_recovery_state_db(state_db, sid)
    _write_sidecar(session_dir, anchor, messages=[_message(0)])
    _write_index(index_file, [_index_row(anchor, message_count=1, last_message_at=1_000.0)])

    models.all_sessions()

    persisted_sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    # Exact stable IDs, tool identity, content, and timestamp order prove the
    # recovery did more than make six opaque rows visible.
    assert [_message_projection(m) for m in persisted_sidecar["messages"]] == (
        _expected_recovered_sequence()
    )
    assert [m["timestamp"] for m in persisted_sidecar["messages"]] == [
        1_000.0,
        1_001.0,
        1_002.0,
        1_003.0,
        1_004.0,
        1_005.0,
    ]
    persisted_index = {row["session_id"]: row for row in _read_index(index_file)}
    assert persisted_index[sid]["message_count"] == 6
    assert persisted_index[anchor]["message_count"] == 1


def test_partially_trimmed_sidecar_replays_tail_but_watermark_blocks_resurrect(
    session_dir, state_db
):
    sid = "partial_trimmed"
    prefix = _sidecar_prefix_messages()
    _write_sidecar(
        session_dir,
        sid,
        messages=prefix,
        message_count=2,
        updated_at=1_001.0,
    )
    _make_partial_recovery_state_db(state_db, sid)
    permitted = models._materialize_index_update_from_state_db(
        Session.load_metadata_only(sid)
    )
    assert [_message_projection(m) for m in permitted.messages] == (
        _expected_recovered_sequence()
    )

    blocked_sid = "partial_trimmed_watermark"
    _write_sidecar(
        session_dir,
        blocked_sid,
        messages=_sidecar_prefix_messages(),
        message_count=2,
        truncation_watermark=1_001.0,
        truncation_boundary=1_001.0,
        updated_at=1_001.0,
    )
    _make_partial_recovery_state_db(state_db, blocked_sid, first_row_id=41)
    before = (session_dir / f"{blocked_sid}.json").read_text(encoding="utf-8")
    blocked = models._materialize_index_update_from_state_db(
        Session.load_metadata_only(blocked_sid)
    )
    assert [_message_projection(m) for m in blocked.messages] == [
        ("sidecar-user-1", "user", "inspect durable replay", None, None, None),
        ("sidecar-assistant-1", "assistant", "sidecar checkpoint", None, None, None),
    ]
    assert blocked.compact()["message_count"] == 2
    assert (session_dir / f"{blocked_sid}.json").read_text(encoding="utf-8") == before


def test_restart_boundary_does_not_drift_down_after_two_materializations(
    session_dir, index_file, state_db
):
    sid = "restart_boundary"
    anchor = "restart_anchor"
    _write_sidecar(
        session_dir,
        sid,
        messages=_sidecar_prefix_messages(),
        include_message_count=False,
        updated_at=1_001.0,
    )
    _make_partial_recovery_state_db(state_db, sid)
    _write_sidecar(session_dir, anchor, messages=[_message(0)])
    _write_index(index_file, [_index_row(anchor, message_count=1, last_message_at=1_000.0)])

    counts = []
    sequences = []
    for _ in range(2):
        recovered = models._materialize_index_update_from_state_db(
            models.Session.load_metadata_only(sid)
        )
        models._write_session_index(updates=[recovered])
        rows = models.all_sessions()
        counts.append({row["session_id"]: row["message_count"] for row in rows}[sid])
        persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
        sequences.append([_message_projection(m) for m in persisted["messages"]])

    assert counts == [6, 6]
    assert sequences[0] == _expected_recovered_sequence()
    assert sequences[1] == sequences[0]


def test_recovery_preserves_archived_title_and_approval_runtime_state(
    session_dir, index_file, state_db
):
    sid = "preserve_recovery"
    anchor = "preserve_anchor"
    _write_sidecar(
        session_dir,
        sid,
        messages=_sidecar_prefix_messages(),
        message_count=2,
        title="Manually named recovery",
        archived=True,
        manual_title=True,
        llm_title_generated=True,
        active_stream_id="approval-stream",
        pending_user_message="awaited approval prompt",
        pending_started_at=1_006.0,
        composer_draft={"text": "composer draft after modal"},
        updated_at=1_001.0,
    )
    _make_partial_recovery_state_db(state_db, sid)
    _write_sidecar(session_dir, anchor, messages=[_message(0)])
    _write_index(index_file, [_index_row(anchor, message_count=1, last_message_at=1_000.0)])

    recovered = models._materialize_index_update_from_state_db(
        Session.load_metadata_only(sid)
    )
    persisted = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    persisted_index = {row["session_id"]: row for row in _read_index(index_file)}

    assert recovered.archived is True
    assert recovered.title == "Manually named recovery"
    assert recovered.manual_title is True
    assert recovered.llm_title_generated is True
    assert recovered.active_stream_id == "approval-stream"
    assert recovered.pending_user_message == "awaited approval prompt"
    assert recovered.pending_started_at == 1_006.0
    assert recovered.composer_draft == {"text": "composer draft after modal"}
    assert persisted["archived"] is True
    assert persisted["manual_title"] is True
    assert persisted["llm_title_generated"] is True
    assert persisted["active_stream_id"] == "approval-stream"
    assert persisted["pending_user_message"] == "awaited approval prompt"
    assert persisted["pending_started_at"] == 1_006.0
    assert persisted["composer_draft"] == {"text": "composer draft after modal"}
    assert [_message_projection(m) for m in persisted["messages"]] == (
        _expected_recovered_sequence()
    )
    assert persisted_index[sid]["message_count"] == 6
    assert persisted_index[sid]["last_message_at"] == 1_006.0
    assert persisted_index[sid]["archived"] is True
    assert persisted_index[sid]["manual_title"] is True
    assert persisted_index[sid]["llm_title_generated"] is True


def test_title_only_non_untitled_session_remains_visible_across_backfill(
    session_dir, index_file, state_db
):
    sid = "title_only_visible"
    anchor = "title_backfill_anchor"
    _write_sidecar(
        session_dir,
        sid,
        messages=[],
        message_count=0,
        title="Saved empty draft",
        manual_title=True,
        llm_title_generated=False,
    )
    _write_sidecar(session_dir, anchor, messages=[_message(0)])
    _write_index(index_file, [
        _index_row(sid, message_count=0, last_message_at=None, title="Saved empty draft"),
        _index_row(anchor, message_count=1, last_message_at=1_000.0),
    ])

    rows = models.all_sessions()
    by_id = {row["session_id"]: row for row in rows}

    assert by_id[sid]["title"] == "Saved empty draft"
    assert by_id[sid]["message_count"] == 0
    assert by_id[sid]["manual_title"] is True
    assert by_id[sid]["llm_title_generated"] is False


def test_full_rebuild_repairs_unusable_metadata_without_zeroing_nonempty_row(
    session_dir, index_file
):
    malformed = "rebuild_malformed_metadata"
    empty = "rebuild_genuine_empty"
    _write_sidecar(
        session_dir,
        malformed,
        messages=_sidecar_prefix_messages(),
        message_count="malformed",
    )
    _write_sidecar(session_dir, empty, messages=[], include_message_count=False)

    models._write_session_index(updates=None)

    persisted = {row["session_id"]: row for row in _read_index(index_file)}
    assert persisted[malformed]["message_count"] == 2
    assert persisted[empty]["message_count"] == 0
