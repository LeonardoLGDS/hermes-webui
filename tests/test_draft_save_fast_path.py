"""Regression coverage for the multi-megabyte composer-draft save defect."""

import hashlib
import io
import json
import os
from urllib.parse import urlparse

import pytest

import api.models as models


SID = "draft_fast_path"


@pytest.fixture
def isolated_session_store(tmp_path, monkeypatch):
    """Keep every session artifact in the test's temporary store."""
    # WHY: draft-save defect — prove the fast path without touching real state.
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", {})
    return session_dir


class _Handler:
    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


@pytest.fixture
def http_state(tmp_path, monkeypatch):
    from collections import OrderedDict

    from api import config, helpers, models, profiles, routes, wsbound

    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "SESSIONS", config.SESSIONS)
    monkeypatch.setattr(routes, "SESSIONS", config.SESSIONS)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(routes, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(
        routes, "_active_state_db_path", lambda: tmp_path / "state.db"
    )
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: tmp_path)
    monkeypatch.setattr(
        models, "_active_state_db_path", lambda: tmp_path / "state.db"
    )
    monkeypatch.setattr(helpers, "_security_headers", lambda handler: None)
    monkeypatch.setattr(
        helpers, "flush_pending_auth_cookies", lambda handler: None
    )
    monkeypatch.setattr(
        wsbound, "RESPONSES", wsbound.ByteLRU(8 * 1024 * 1024)
    )
    monkeypatch.setattr(
        wsbound, "COMPILES", wsbound.AdmissionGate(512 * 1024 * 1024)
    )
    monkeypatch.setattr(wsbound, "pressure_health", lambda: {"shed": False})
    monkeypatch.setattr(
        config, "session_writeback_owner", lambda sid: None
    )
    return routes, models, tmp_path


def _write_large_session(session_dir, *, draft=None):
    messages = [
        {
            "id": f"message-{index}",
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"{index:04d}-" * 1_000,
            "timestamp": 1_700_000_000.0 + index,
        }
        for index in range(4_240)
    ]
    payload = {
        "session_id": SID,
        "title": "Draft fast path",
        "workspace": "/tmp/hermes-draft-test",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_100_000.0,
        "composer_draft": draft or {"text": "embedded", "files": []},
        "message_count": len(messages),
        "messages": messages,
    }
    path = session_dir / f"{SID}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path, messages


def test_draft_save_writes_only_small_sidecar_and_never_messages(
    isolated_session_store, monkeypatch
):
    session_path, messages = _write_large_session(isolated_session_store)
    assert session_path.stat().st_size >= 20 * 1024 * 1024
    before_hash = hashlib.sha256(session_path.read_bytes()).hexdigest()
    before_stat = session_path.stat()
    before_signature = (
        before_stat.st_dev,
        before_stat.st_ino,
        before_stat.st_size,
        before_stat.st_mtime_ns,
    )
    session = models.Session.load(SID)
    dumped_objects = []
    real_dumps = models.json.dumps

    def spy_dumps(value, *args, **kwargs):
        dumped_objects.append(value)
        return real_dumps(value, *args, **kwargs)

    draft = {"text": "newest draft", "files": [{"name": "notes.txt"}]}
    monkeypatch.setattr(models.json, "dumps", spy_dumps)
    session.save_composer_draft(draft)

    sidecar = isolated_session_store / f"{SID}.json.draft"
    assert sidecar.stat().st_size < 64 * 1024
    assert hashlib.sha256(session_path.read_bytes()).hexdigest() == before_hash
    after_stat = session_path.stat()
    after_signature = (
        after_stat.st_dev,
        after_stat.st_ino,
        after_stat.st_size,
        after_stat.st_mtime_ns,
    )
    assert after_signature == before_signature
    assert dumped_objects, "the sidecar write should serialize its small envelope"
    assert all(not isinstance(value, list) for value in dumped_objects)
    assert all(value is not messages for value in dumped_objects)
    assert all(not isinstance(value, dict) or "messages" not in value for value in dumped_objects)

    session.composer_draft = draft
    del session
    reloaded = models.Session.load(SID)
    assert reloaded.composer_draft == draft
    assert reloaded.messages == messages


def test_newer_sidecar_wins_and_newer_transcript_save_suppresses_it(
    isolated_session_store
):
    _path, messages = _write_large_session(
        isolated_session_store, draft={"text": "embedded old", "files": []}
    )
    newest = {"text": "sidecar newest", "files": []}
    session = models.Session.load(SID)
    session.save_composer_draft(newest)

    assert models.Session.load(SID).composer_draft == newest

    session.composer_draft = {"text": "embedded newest", "files": []}
    session.save(touch_updated_at=False, skip_index=True)
    reloaded = models.Session.load(SID)
    assert reloaded.composer_draft == {"text": "embedded newest", "files": []}
    assert reloaded.messages == messages


def test_corrupt_newer_sidecar_falls_back_and_preserves_transcript(
    isolated_session_store, caplog
):
    session_path, messages = _write_large_session(
        isolated_session_store, draft={"text": "safe fallback", "files": []}
    )
    sidecar = isolated_session_store / f"{SID}.json.draft"
    sidecar.write_text("{not-json", encoding="utf-8")
    newer_ns = session_path.stat().st_mtime_ns + 1
    os.utime(sidecar, ns=(newer_ns, newer_ns))

    with caplog.at_level("WARNING", logger="api.models"):
        reloaded = models.Session.load(SID)

    assert reloaded.composer_draft == {"text": "safe fallback", "files": []}
    assert reloaded.messages == messages
    assert any(
        "Unusable composer draft sidecar" in record.getMessage()
        for record in caplog.records
    )


def test_boolean_version_is_not_a_valid_draft_sidecar(isolated_session_store, caplog):
    session_path, messages = _write_large_session(
        isolated_session_store, draft={"text": "safe fallback", "files": []}
    )
    sidecar = isolated_session_store / f"{SID}.json.draft"
    sidecar.write_text(
        json.dumps({"version": True, "draft": {"text": "hostile"}}),
        encoding="utf-8",
    )
    newer_ns = session_path.stat().st_mtime_ns + 1
    os.utime(sidecar, ns=(newer_ns, newer_ns))

    with caplog.at_level("WARNING", logger="api.models"):
        reloaded = models.Session.load(SID)

    assert reloaded.composer_draft == {"text": "safe fallback", "files": []}
    assert reloaded.messages == messages
    assert any(
        "sidecar is not a version-1 draft envelope" in record.getMessage()
        for record in caplog.records
    )


def test_first_draft_for_new_empty_session_remains_reloadable(isolated_session_store):
    session = models.Session(
        session_id=SID,
        workspace=str(isolated_session_store),
        composer_draft={"text": "old", "files": []},
    )
    first = {"text": "draft before first message", "files": []}

    session.save_composer_draft(first)
    shell = isolated_session_store / f"{SID}.json"
    shell_signature = shell.stat()
    shell_signature = (
        shell_signature.st_dev,
        shell_signature.st_ino,
        shell_signature.st_size,
        shell_signature.st_mtime_ns,
    )

    assert models.Session.load(SID).composer_draft == first
    assert models.Session.load(SID).messages == []

    second = {"text": "newest new-chat draft", "files": []}
    session.save_composer_draft(second)
    after_shell = shell.stat()
    assert (
        after_shell.st_dev,
        after_shell.st_ino,
        after_shell.st_size,
        after_shell.st_mtime_ns,
    ) == shell_signature
    assert models.Session.load(SID).composer_draft == second


def test_session_without_sidecar_loads_exactly_as_before(isolated_session_store):
    _path, messages = _write_large_session(
        isolated_session_store, draft={"text": "legacy", "files": ["legacy.txt"]}
    )
    session = models.Session.load(SID)
    assert session.composer_draft == {"text": "legacy", "files": ["legacy.txt"]}
    assert session.messages == messages


def _write_http_draft_session(http_state, sid="http-draft"):
    routes, models, directory = http_state
    payload = {
        "session_id": sid,
        "title": "Draft load paths",
        "workspace": str(directory),
        "created_at": 1.0,
        "updated_at": 2.0,
        "message_count": 2,
        "composer_draft": {"text": "embedded", "files": []},
        "messages": [
            {"role": "user", "content": "one", "timestamp": 1.0},
            {"role": "assistant", "content": "two", "timestamp": 2.0},
        ],
    }
    path = directory / f"{sid}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return models.Session(**payload), path


def _request_draft(http_state, sid):
    handler = _Handler(
        f"/api/session?session_id={sid}&resolve_model=0&messages=1&msg_limit=30"
    )
    http_state[0].handle_get(handler, urlparse(handler.path))
    body = json.loads(handler.wfile.getvalue())
    assert handler.status == 200
    return body["session"]["composer_draft"]


@pytest.mark.parametrize("query", ["messages=0", "messages=1&msg_limit=30"])
def test_http_metadata_and_bounded_responses_overlay_durable_draft(
    http_state, query
):
    session, _path = _write_http_draft_session(http_state)
    session.save_composer_draft({"text": "durable", "files": []})
    handler = _Handler(
        f"/api/session?session_id={session.session_id}&resolve_model=0&{query}"
    )
    http_state[0].handle_get(handler, urlparse(handler.path))
    body = json.loads(handler.wfile.getvalue())
    assert handler.status == 200
    assert body["session"]["composer_draft"] == {"text": "durable", "files": []}


def test_http_cold_full_reopen_overlays_durable_draft(http_state, monkeypatch):
    from api import bounded_session_tail

    session, _path = _write_http_draft_session(http_state, "http-cold-full")
    session.save_composer_draft({"text": "cold durable", "files": []})

    def unsupported(*_args, **_kwargs):
        raise bounded_session_tail.BoundedTailUnsupported("forced cold reopen")

    monkeypatch.setattr(
        bounded_session_tail, "read_bounded_session_tail", unsupported
    )
    assert _request_draft(http_state, session.session_id) == {
        "text": "cold durable",
        "files": [],
    }


def test_http_sidecar_identity_selects_between_stale_equal_and_corrupt_drafts(
    http_state,
):
    session, path = _write_http_draft_session(http_state)
    sidecar = path.with_suffix(".json.draft")
    session.save_composer_draft({"text": "durable", "files": []})
    assert _request_draft(http_state, session.session_id) == {
        "text": "durable",
        "files": [],
    }
    # An unchanged identity must continue returning the same durable value.
    assert _request_draft(http_state, session.session_id) == {
        "text": "durable",
        "files": [],
    }

    # An equal sidecar is safe: it intentionally preserves the same value.
    session.save_composer_draft({"text": "durable", "files": []})
    assert _request_draft(http_state, session.session_id) == {
        "text": "durable",
        "files": [],
    }

    # A stale sidecar cannot outrank the newer transcript's embedded draft.
    sidecar.write_text(
        json.dumps(
            {"version": 1, "draft": {"text": "stale", "files": []}}
        ),
        encoding="utf-8",
    )
    stale_ns = path.stat().st_mtime_ns - 1_000_000_000
    os.utime(sidecar, ns=(stale_ns, stale_ns))
    assert _request_draft(http_state, session.session_id) == {
        "text": "embedded",
        "files": [],
    }

    # A corrupt newer sidecar fails closed to the embedded draft.
    sidecar.write_text("{corrupt", encoding="utf-8")
    corrupt_ns = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(sidecar, ns=(corrupt_ns, corrupt_ns))
    assert _request_draft(http_state, session.session_id) == {
        "text": "embedded",
        "files": [],
    }


def test_http_response_cache_misses_after_new_draft_sidecar_write(http_state):
    session, _path = _write_http_draft_session(http_state, "draft-cache")
    assert _request_draft(http_state, session.session_id) == {
        "text": "embedded",
        "files": [],
    }
    session.save_composer_draft({"text": "after cache", "files": []})
    assert _request_draft(http_state, session.session_id) == {
        "text": "after cache",
        "files": [],
    }
