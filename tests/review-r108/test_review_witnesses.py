"""Independent review witnesses; expected contracts, not implementation mocks."""
import json
import os
import io
from collections import OrderedDict
from contextlib import contextmanager
from urllib.parse import urlparse

import pytest

@pytest.fixture(scope="session")
def test_server():
    yield


class Handler:
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
def state(monkeypatch, tmp_path):
    from api import config, helpers, models, profiles, routes, wsbound

    for module in (config, models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", tmp_path)
        monkeypatch.setattr(module, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "SESSIONS", config.SESSIONS)
    monkeypatch.setattr(routes, "SESSIONS", config.SESSIONS)
    monkeypatch.setattr(routes, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: tmp_path)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(helpers, "_security_headers", lambda handler: None)
    monkeypatch.setattr(helpers, "flush_pending_auth_cookies", lambda handler: None)
    monkeypatch.setattr(wsbound, "RESPONSES", wsbound.ByteLRU(8 * 1024 * 1024))
    monkeypatch.setattr(wsbound, "COMPILES", wsbound.AdmissionGate(512 * 1024 * 1024))
    monkeypatch.setattr(wsbound, "pressure_health", lambda: {"shed": False})
    monkeypatch.setattr(config, "session_writeback_owner", lambda sid: None)
    return routes, models, wsbound, tmp_path


def write_session(state, sid, size=22600000, resident=False):
    routes, models, wsbound, directory = state
    payload = dict(session_id=sid, title="Review", created_at=1, updated_at=2,
                   message_count=1000, model="test-model", profile="default",
                   context_length=32768, composer_draft={"text": "old"})
    payload["messages"] = [dict(role="user" if index % 2 else "assistant",
                                content=f"row-{index}", timestamp=index + 1)
                           for index in range(1000)]
    payload["messages"][0]["content"] += "x" * (size - len(json.dumps(payload).encode()))
    path = directory / f"{sid}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert path.stat().st_size == size
    session = models.Session(**payload)
    if resident:
        routes.SESSIONS[sid] = session
    return session, path


def request(state, sid, query):
    handler = Handler(f"/api/session?session_id={sid}&resolve_model=0&{query}")
    state[0].handle_get(handler, urlparse(handler.path))
    body = json.loads(handler.wfile.getvalue())
    print(f"HTTP sid={sid} query={query} status={handler.status} "
          f"error={body.get('error')} draft={body.get('session', {}).get('composer_draft')} "
          f"rows={len(body.get('session', {}).get('messages', []))}")
    return handler.status, body


def observe(state, monkeypatch):
    wsbound = state[2]
    evidence = {"charges": [], "reads": []}
    original_admit = wsbound.COMPILES.admit
    original_read = wsbound.read_source_text

    @contextmanager
    def admit(cost, *args, **kwargs):
        evidence["charges"].append(cost)
        with original_admit(cost, *args, **kwargs):
            yield

    def read(path):
        evidence["reads"].append((path.name, path.stat().st_size,
                                  wsbound.READ_BUDGET.get().remaining))
        return original_read(path)

    monkeypatch.setattr(wsbound.COMPILES, "admit", admit)
    monkeypatch.setattr(wsbound, "read_source_text", read)
    return evidence


@pytest.mark.parametrize("query,rows", [("messages=1&msg_limit=30", 30),
                                       ("messages=1", 200),
                                       ("messages=1&msg_limit=30&msg_before=500", 30)])
def test_resident_default_gate(state, monkeypatch, query, rows):
    session, path = write_session(state, "resident", resident=True)
    evidence = observe(state, monkeypatch)
    status, body = request(state, session.session_id, query)
    print(f"RESIDENT file={path.stat().st_size} old_charge={path.stat().st_size * 24} "
          f"gate={state[2].COMPILES.budget} evidence={evidence} released={state[2].COMPILES.snapshot()}")
    assert status == 200
    assert len(body["session"]["messages"]) == rows
    assert evidence["reads"] == []
    assert evidence["charges"] == [(8388608 + rows * 4096) * 12 + 1572864]
    assert state[2].COMPILES.snapshot()["bytes"] == 0


def test_valid_cold_file_still_has_fixed_guard(state, monkeypatch):
    session, path = write_session(state, "cold", size=64 * 1024 * 1024)
    evidence = observe(state, monkeypatch)
    status, body = request(state, session.session_id, "messages=1")
    print(f"COLD file={path.stat().st_size} old_charge={path.stat().st_size * 24} "
          f"gate={state[2].COMPILES.budget} evidence={evidence}")
    assert status == 429 and body.get("error") == "memory_budget", evidence
    assert evidence["reads"] == []


@pytest.mark.parametrize("mode", ["full", "metadata", "bounded"])
def test_newer_draft_survives_all_load_paths(state, mode):
    session, path = write_session(state, "draft", size=100000)
    session.save_composer_draft({"text": "new durable draft"})
    draft_path = path.with_suffix(".json.draft")
    newer = path.stat().st_mtime_ns + 1000000000
    os.utime(draft_path, ns=(newer, newer))
    state[0].SESSIONS.clear()
    if mode == "full":
        actual = state[1].Session.load(session.session_id).composer_draft
    else:
        query = "messages=0" if mode == "metadata" else "messages=1&msg_limit=30"
        status, body = request(state, session.session_id, query)
        assert status == 200
        actual = body["session"]["composer_draft"]
    print(f"DRAFT mode={mode} actual={actual}")
    assert actual == {"text": "new durable draft"}


def test_metadata_cache_refreshes_after_real_draft_post(state):
    session, path = write_session(state, "cache-draft", size=100000, resident=True)
    status, body = request(state, session.session_id, "messages=0")
    assert status == 200 and body["session"]["composer_draft"] == {"text": "old"}
    serialized = json.dumps({"session_id": session.session_id, "text": "after POST"}).encode()
    handler = Handler("/api/session/draft")
    handler.command = "POST"
    handler.rfile = io.BytesIO(serialized)
    handler.headers = {"Content-Length": str(len(serialized))}
    state[0].handle_post(handler, urlparse(handler.path))
    reply = json.loads(handler.wfile.getvalue())
    print(f"POST draft status={handler.status} reply={reply}")
    assert handler.status == 200 and reply["draft"]["text"] == "after POST"
    status, body = request(state, session.session_id, "messages=0")
    print(f"CACHE current_resident={session.composer_draft} returned={body['session']['composer_draft']}")
    assert status == 200
    assert body["session"]["composer_draft"]["text"] == "after POST"


@pytest.mark.parametrize("resident", [False, True])
def test_tiny_gate_still_refuses_and_releases(state, resident):
    session, path = write_session(state, "tiny", size=100000, resident=resident)
    state[2].COMPILES.budget = 1
    status, body = request(state, session.session_id, "messages=1")
    print(f"TINY resident={resident} gate={state[2].COMPILES.snapshot()}")
    assert status == 429 and body["error"] == "memory_budget"
    snapshot = state[2].COMPILES.snapshot()
    assert snapshot["bytes"] == snapshot["active"] == snapshot["queued"] == 0


def test_clean_resident_refreshes_after_external_append(state):
    session, path = write_session(state, "external-append", size=100000, resident=True)
    payload = json.loads(path.read_text())
    payload["messages"].append({"role": "assistant", "content": "external latest", "timestamp": 2000})
    payload["message_count"] = len(payload["messages"])
    payload["updated_at"] = 2000
    path.write_text(json.dumps(payload), encoding="utf-8")
    status, body = request(state, session.session_id, "messages=1&msg_limit=30")
    actual = body["session"]["messages"][-1]["content"]
    print(f"EXTERNAL disk_last=external latest response_last={actual}")
    assert status == 200
    assert actual == "external latest"
