"""Transcript admission probes using synthetic state, never the live server."""

import io
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
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


def make_session(state, sid, resident=False, oversized=False, clean=False, large=True):
    routes, models, wsbound, directory = state
    messages = [
        {"role": "user" if index % 2 == 0 else "assistant",
         "content": str(index) + "x" * (24 * 1024), "timestamp": index + 1}
        for index in range(1024 if large else 64)
    ]
    if oversized:
        messages[-1]["content"] = "z" * (2 * 1024 * 1024)
    payload = dict(session_id=sid, title="Synthetic", workspace=str(directory),
                   model="test-model", profile="default", context_length=32768)
    if clean:
        payload.update(created_at=1, updated_at=len(messages), message_count=len(messages))
    payload["messages"] = messages
    path = directory / f"{sid}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    if large:
        assert path.stat().st_size >= 24 * 1024 * 1024
    session = models.Session(**payload)
    if resident:
        routes.SESSIONS[sid] = session
    return session, path


def probe(state, sid, suffix="&msg_limit=30"):
    routes, models, wsbound, directory = state
    handler = Handler(f"/api/session?session_id={sid}&messages=1&resolve_model=0{suffix}")
    started = time.monotonic()
    routes.handle_get(handler, urlparse(handler.path))
    payload = json.loads(handler.wfile.getvalue())
    print(f"PROBE {sid}{suffix}: HTTP {handler.status}, "
          f"{(time.monotonic() - started) * 1000:.1f} ms, "
          f"messages={len(payload.get('session', {}).get('messages', []))}, "
          f"error={payload.get('error')}")
    return handler.status, payload


@pytest.mark.parametrize("resident,clean", [(False, False), (False, True), (True, True)])
def test_large_tail(state, resident, clean):
    make_session(state, "large", resident=resident, clean=clean)
    status, payload = probe(state, "large")
    expected = 200 if resident else 429
    assert status == expected
    if expected == 200:
        assert 0 < len(payload["session"]["messages"]) <= 30
    else:
        assert payload == {"error": "memory_budget"}


@pytest.mark.parametrize("suffix", ["&msg_limit=30", "&msg_limit=5", ""])
def test_resident_never_reads_sidecar_body(state, monkeypatch, suffix):
    session, path = make_session(state, "resident", resident=True)
    session.messages[-1]["content"] = "unsaved resident answer"
    session.active_stream_id = "resident-worker"
    original_open = Path.open

    def checked_open(source, *args, **kwargs):
        if source == path:
            pytest.fail("resident transcript read its sidecar body")
        return original_open(source, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    status, payload = probe(state, "resident", suffix)
    assert status == 200
    assert payload["session"]["messages"][-1]["content"] == "unsaved resident answer"
    assert session.active_stream_id == "resident-worker"


@pytest.mark.parametrize("resident", [False, True])
def test_oversized_window_is_413(state, resident):
    make_session(state, "oversized", resident=resident, oversized=True, large=False)
    status, payload = probe(state, "oversized")
    assert status == 413
    assert payload == {"error": "message_window_too_large", "max_bytes": 1572864}


def test_legacy_large_bare_load(state):
    make_session(state, "legacy")
    status, payload = probe(state, "legacy", "")
    assert status == 429
    assert payload == {"error": "memory_budget"}


@pytest.mark.parametrize("resident,clean", [(False, False), (False, True), (True, True)])
def test_two_large_loads_default_gate(state, monkeypatch, resident, clean):
    for sid in ("concurrent-a", "concurrent-b"):
        make_session(state, sid, resident=resident, clean=clean)
    reaches_response_builder = resident
    barrier = threading.Barrier(2)
    overlap = []
    admitted = threading.Barrier(
        2, action=lambda: overlap.append(state[2].COMPILES.snapshot()["active"]),
    ) if reaches_response_builder else None
    routes = state[0]
    original_impl = routes._handle_get_impl

    def overlapping_impl(handler, parsed):
        if admitted is not None:
            admitted.wait(timeout=5)
        return original_impl(handler, parsed)

    if reaches_response_builder:
        monkeypatch.setattr(routes, "_handle_get_impl", overlapping_impl)

    def load(sid):
        barrier.wait(timeout=5)
        return probe(state, sid)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(load, ("concurrent-a", "concurrent-b")))
    print(f"CONCURRENT resident={resident}, clean={clean}, default=536870912: {statuses}")
    expected = [200, 200] if reaches_response_builder else [429, 429]
    assert statuses == expected
    if reaches_response_builder:
        assert overlap == [2]
    gate = state[2].COMPILES.snapshot()
    assert gate["bytes"] == gate["active"] == gate["queued"] == 0


def test_clean_nonresident_uses_bounded_reader(state, monkeypatch):
    from api import bounded_session_tail

    make_session(state, "clean", clean=True, large=False)
    original_read = bounded_session_tail.read_bounded_session_tail
    reads = []

    def observed_read(*args, **kwargs):
        try:
            result = original_read(*args, **kwargs)
        except bounded_session_tail.BoundedTailUnsupported as error:
            pytest.fail(f"clean sidecar rejected: {error}")
        reads.append(result)
        return result

    monkeypatch.setattr(bounded_session_tail, "read_bounded_session_tail", observed_read)
    monkeypatch.setattr(state[0], "get_session", lambda *args, **kwargs: pytest.fail("full load"))
    status, payload = probe(state, "clean")
    assert status == 200
    assert len(reads) == 1
    assert len(payload["session"]["messages"]) == 30
    assert "clean" not in state[0].SESSIONS


def test_clean_metadata_stub_still_uses_bounded_reader(state, monkeypatch):
    """Registry residency is not writeback ownership or a loaded transcript."""
    from api import bounded_session_tail

    session, path = make_session(state, "metadata-stub", clean=True, large=False)
    session._loaded_metadata_only = True
    state[0].SESSIONS[session.session_id] = session
    reads = []
    original_read = bounded_session_tail.read_bounded_session_tail

    def observed_read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        reads.append(result)
        return result

    monkeypatch.setattr(bounded_session_tail, "read_bounded_session_tail", observed_read)
    monkeypatch.setattr(state[0], "get_session", lambda *args, **kwargs: pytest.fail("full load"))
    status, payload = probe(state, session.session_id)
    assert status == 200
    assert len(reads) == 1
    assert len(payload["session"]["messages"]) == 30


def test_large_clean_session_attempts_bounded_proof(state, monkeypatch):
    from api import bounded_session_tail

    make_session(state, "large-clean", clean=True)
    original_read = bounded_session_tail.read_bounded_session_tail
    attempts = []

    def observed_read(*args, **kwargs):
        attempts.append(kwargs["expected_source"])
        return original_read(*args, **kwargs)

    monkeypatch.setattr(bounded_session_tail, "read_bounded_session_tail", observed_read)
    assert probe(state, "large-clean")[0] == 429
    assert len(attempts) == 1


def test_resident_bypasses_disk_stamped_response_cache(state):
    session, path = make_session(state, "cache", clean=True, large=False)
    assert probe(state, "cache")[0] == 200
    assert state[2].RESPONSES.snapshot()["entries"] > 0
    stamp = path.stat()
    state[0].SESSIONS["cache"] = session
    for text in ("first unsaved answer", "second unsaved answer"):
        session.messages[-1]["content"] = text
        status, payload = probe(state, "cache")
        assert status == 200
        assert payload["session"]["messages"][-1]["content"] == text
    assert path.stat() == stamp


def test_resident_appearing_during_bounded_read_wins(state, monkeypatch):
    from api import bounded_session_tail

    session, path = make_session(state, "appeared", clean=True, large=False)
    session.messages[-1]["content"] = "new resident answer"
    original_read = bounded_session_tail.read_bounded_session_tail

    def read_then_publish(*args, **kwargs):
        result = original_read(*args, **kwargs)
        state[0].SESSIONS[session.session_id] = session
        return result

    monkeypatch.setattr(bounded_session_tail, "read_bounded_session_tail", read_then_publish)
    status, payload = probe(state, session.session_id)
    assert status == 200
    assert payload["session"]["messages"][-1]["content"] == "new resident answer"
    assert state[2].COMPILES.snapshot()["bytes"] == 0


def test_resident_appearing_before_response_builder_wins(state, monkeypatch):
    session, path = make_session(state, "late-resident", clean=True, large=False)
    session.messages[-1]["content"] = "late resident answer"
    routes = state[0]
    original_impl = routes._handle_get_impl

    def publish_then_build(handler, parsed):
        routes.SESSIONS[session.session_id] = session
        return original_impl(handler, parsed)

    monkeypatch.setattr(routes, "_handle_get_impl", publish_then_build)
    status, payload = probe(state, session.session_id)
    assert status == 200
    assert payload["session"]["messages"][-1]["content"] == "late resident answer"


def test_resident_appearing_after_cold_admission_wins(state, monkeypatch):
    """A late graph must rescue a request without nesting reservations."""
    from api import bounded_session_tail

    session, _path = make_session(state, "admission-resident", clean=True, large=False)
    session.messages[-1]["content"] = "admission resident answer"
    session.pending_user_message = "unsaved prompt"
    routes, _models, wsbound, _directory = state
    original_admit = wsbound.COMPILES.admit
    charges = []

    @contextmanager
    def publish_during_cold_admission(cost):
        charges.append(cost)
        with original_admit(cost):
            # The first admission is the bounded proof. Publishing on the
            # second (cold) admission models residency arriving after queueing.
            if len(charges) == 2:
                routes.SESSIONS[session.session_id] = session
            yield

    monkeypatch.setattr(wsbound.COMPILES, "admit", publish_during_cold_admission)
    monkeypatch.setattr(
        bounded_session_tail,
        "read_bounded_session_tail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            bounded_session_tail.BoundedTailUnsupported("forced cold admission")
        ),
    )
    status, payload = probe(state, session.session_id)
    assert status == 200
    assert payload["session"]["messages"][-1]["content"] == "admission resident answer"
    cold_cost = max(8 * 1024 * 1024, 2 * (_path).stat().st_size) * 12
    resident_cost = (8 * 1024 * 1024 + 30 * 4096) * 12 + 1572864
    assert charges == [
        bounded_session_tail.BOUNDED_GATE_COST,
        cold_cost,
        resident_cost,
    ]
    assert wsbound.COMPILES.snapshot()["active"] == 0


def test_writeback_owner_blocks_disk_proof(state, monkeypatch):
    from api import bounded_session_tail, config

    make_session(state, "owned", clean=True)
    monkeypatch.setattr(config, "session_writeback_owner", lambda sid: "worker")
    monkeypatch.setattr(
        bounded_session_tail, "read_bounded_session_tail",
        lambda *args, **kwargs: pytest.fail("dirty disk proof"),
    )
    assert probe(state, "owned")[0] == 429


@pytest.mark.parametrize("writer_authoritative", [False, True])
def test_external_append_refreshes_clean_but_not_dirty_resident(
    state, writer_authoritative
):
    sid = "dirty" if writer_authoritative else "clean-external"
    session, path = make_session(state, sid, clean=True, large=False)
    state[0].SESSIONS[sid] = session
    if writer_authoritative:
        session.pending_user_message = "unsaved prompt"
        session.messages[-1]["content"] = "unsaved resident answer"

    disk_payload = json.loads(path.read_text(encoding="utf-8"))
    disk_payload["message_count"] = len(disk_payload["messages"]) + 1
    disk_payload["updated_at"] = len(disk_payload["messages"]) + 1
    disk_payload["messages"].append(
        {
            "role": "assistant",
            "content": "external append",
            "timestamp": len(disk_payload["messages"]) + 1,
        }
    )
    path.write_text(json.dumps(disk_payload), encoding="utf-8")

    status, payload = probe(state, sid)
    assert status == 200
    if writer_authoritative:
        assert payload["session"]["messages"][-1]["content"] == "unsaved resident answer"
        assert "external append" not in payload["session"]["messages"]
    else:
        assert payload["session"]["messages"][-1]["content"] == "external append"
    assert state[2].COMPILES.snapshot()["bytes"] == 0


@pytest.mark.parametrize("rows", [[], [{"role": "user", "content": "one"}]])
def test_resident_empty_and_single_row(state, rows):
    session = state[1].Session(session_id="small", messages=rows, context_length=32768)
    state[0].SESSIONS["small"] = session
    status, payload = probe(state, "small")
    assert status == 200
    assert len(payload["session"]["messages"]) == len(rows)


def test_resident_profile_boundary_remains(state):
    session, path = make_session(state, "other-profile", resident=True)
    session.profile = "other"
    status, payload = probe(state, session.session_id)
    assert status in (404, 409)
    assert "session" not in payload


def test_resident_disconnect_releases_context_and_gate(state, monkeypatch):
    make_session(state, "disconnected", resident=True)
    routes, models, wsbound, directory = state

    def disconnected_impl(handler, parsed):
        assert routes._RESIDENT_SESSION_READ.get() is routes.SESSIONS["disconnected"]
        raise BrokenPipeError("client disconnected")

    monkeypatch.setattr(routes, "_handle_get_impl", disconnected_impl)
    with pytest.raises(BrokenPipeError):
        probe(state, "disconnected", "")
    assert routes._RESIDENT_SESSION_READ.get() is None
    assert wsbound.READ_BUDGET.get() is None
    assert not wsbound.DERIVED_READ.get()
    assert wsbound.RESPONSE_CAPTURE.get() is None
    assert wsbound.COMPILES.snapshot()["bytes"] == 0
