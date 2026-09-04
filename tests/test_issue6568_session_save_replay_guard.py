"""Regression coverage for exact persisted-message replay amplification (#6568)."""

from copy import deepcopy
import json

import pytest

import api.config as config
import api.models as models
from api.models import Session


@pytest.fixture
def isolated_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()
    yield session_dir
    models.SESSIONS.clear()


def test_save_suppresses_only_complete_exact_identity_replays(isolated_session_store):
    exact = {
        "id": "user-1",
        "timestamp": 100.25,
        "role": "user",
        "content": "same text",
    }
    variant = {**exact, "attachments": [{"name": "different.png"}]}
    different_timestamp = {**exact, "timestamp": 101.25}
    different_id = {**exact, "id": "user-2"}
    missing_id = {"timestamp": 102.25, "role": "user", "content": "same text"}
    blank_timestamp = {"id": "blank-ts", "timestamp": " ", "role": "user", "content": "x"}
    legacy_timestamp = {"id": "legacy-1", "_ts": "103.25", "role": "assistant", "content": "ok"}
    original = [
        exact,
        deepcopy(exact),
        variant,
        different_timestamp,
        different_id,
        missing_id,
        deepcopy(missing_id),
        blank_timestamp,
        deepcopy(blank_timestamp),
        legacy_timestamp,
        deepcopy(legacy_timestamp),
        "non-dict marker",
        "non-dict marker",
    ]
    expected = [
        exact,
        variant,
        different_timestamp,
        different_id,
        missing_id,
        deepcopy(missing_id),
        blank_timestamp,
        deepcopy(blank_timestamp),
        legacy_timestamp,
        "non-dict marker",
        "non-dict marker",
    ]

    sidecar = isolated_session_store / "issue6568.json"
    sidecar.write_text(json.dumps({"session_id": "issue6568", "messages": original}), encoding="utf-8")

    session = Session(session_id="issue6568", messages=deepcopy(original))
    session.save(touch_updated_at=False)

    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["messages"] == expected
    assert persisted["message_count"] == len(expected)
    assert session.messages == expected

    backup = json.loads(sidecar.with_suffix(".json.bak").read_text(encoding="utf-8"))
    assert backup["messages"] == original


def test_replay_guard_fails_closed_when_canonicalization_is_unsafe():
    unsafe = {
        "id": "unsafe-1",
        "timestamp": 200,
        "role": "user",
        "content": float("nan"),
    }
    original = [unsafe, deepcopy(unsafe)]

    guarded, removed = models._guard_persisted_exact_message_replays(original)

    assert removed == 0
    assert guarded == original


def test_guarded_shrink_is_abandoned_when_backup_cannot_commit(
    isolated_session_store,
    monkeypatch,
):
    exact = {"id": "u-1", "timestamp": 300, "role": "user", "content": "prompt"}
    original = [exact, deepcopy(exact)]
    sidecar = isolated_session_store / "issue6568_backup_failure.json"
    sidecar.write_text(
        json.dumps({"session_id": "issue6568_backup_failure", "messages": original}),
        encoding="utf-8",
    )

    real_safe_replace = models._safe_replace

    def fail_backup_replace(src, dst):
        if str(dst).endswith(".json.bak"):
            raise OSError("simulated backup failure")
        return real_safe_replace(src, dst)

    monkeypatch.setattr(models, "_safe_replace", fail_backup_replace)

    session = Session(
        session_id="issue6568_backup_failure",
        messages=deepcopy(original),
    )
    session.save(touch_updated_at=False, skip_index=True)

    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["messages"] == original
    assert persisted["message_count"] == len(original)
    assert session.messages == original


def test_unparseable_existing_sidecar_is_backed_up_before_guarded_rewrite(
    isolated_session_store,
):
    exact = {"id": "u-2", "timestamp": 400, "role": "user", "content": "prompt"}
    sidecar = isolated_session_store / "issue6568_corrupt.json"
    corrupt_source = '{"session_id":"issue6568_corrupt","messages":['
    sidecar.write_text(corrupt_source, encoding="utf-8")

    session = Session(
        session_id="issue6568_corrupt",
        messages=[exact, deepcopy(exact)],
    )
    session.save(touch_updated_at=False, skip_index=True)

    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["messages"] == [exact]
    assert sidecar.with_suffix(".json.bak").read_text(encoding="utf-8") == corrupt_source
