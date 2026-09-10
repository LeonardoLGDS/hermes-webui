"""Regression coverage for the multi-megabyte composer-draft save defect."""

import hashlib
import json
import os

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
