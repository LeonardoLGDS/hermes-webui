"""Working-set regression tests: bytes, queue cleanup, identity and HTTP reuse."""

import io
import json
import threading
import time
from urllib.parse import urlparse

import pytest

from api.wsbound import (AdmissionGate, ByteLRU, MemoryBudgetExceeded,
                         WindowTooLarge, bounded_window)


def test_byte_lru_bound_replacement_and_eviction():
    cache = ByteLRU(8192)
    for index in range(40):
        cache.put(str(index), b"x" * 1000)
        assert cache.snapshot()["bytes"] <= 8192
    assert cache.get("0") is None
    assert cache.get("39") == b"x" * 1000
    cache.put("39", b"x" * 8192)
    assert cache.get("39") is None
    with pytest.raises(TypeError):
        cache.put("graph", {})
    generation = cache.generation
    cache.clear()
    assert cache.used == 0 and cache.generation == generation + 1


def test_expiry_removes_charge():
    cache = ByteLRU(8192)
    cache.put("old", b"hello", ttl=-1)
    assert cache.get("old") is None
    assert cache.used == 0


def test_gate_budget_timeout_and_exception_cleanup():
    gate = AdmissionGate(100, slots=2)
    with pytest.raises(MemoryBudgetExceeded):
        with gate.admit(101):
            pytest.fail("oversized admitted")
    with gate.admit(75):
        with pytest.raises(MemoryBudgetExceeded):
            with gate.admit(30, timeout=0):
                pytest.fail("over-budget admitted")
        assert gate.snapshot()["queued"] == 0
    with pytest.raises(RuntimeError):
        with gate.admit(50):
            raise RuntimeError("compile failed")
    assert gate.snapshot()["bytes"] == 0
    assert gate.snapshot()["active"] == 0


def test_gate_fifo_human_not_starved_by_small_poll():
    gate = AdmissionGate(100, slots=2)
    order = []
    def request(label, cost):
        with gate.admit(cost):
            order.append(label)
    with gate.admit(90):
        human = threading.Thread(target=request, args=("human", 100))
        human.start()
        deadline = time.monotonic() + 1
        while gate.snapshot()["queued"] != 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert gate.snapshot()["queued"] == 1
        poll = threading.Thread(target=request, args=("poll", 1))
        poll.start()
    human.join(2)
    poll.join(2)
    assert order == ["human", "poll"]
    assert gate.snapshot()["queued"] == 0


def test_window_is_contiguous_and_preserves_input():
    rows = [{"content": "x" * 40, "row": index} for index in range(10)]
    window, offset = bounded_window(rows, 7, budget=160)
    assert len(window) < 10
    assert window == rows[offset - 7:]
    assert len(json.dumps(window).encode()) <= 160
    assert len(rows) == 10
    with pytest.raises(WindowTooLarge):
        bounded_window([{"content": "x" * 300}], 0, budget=100)


class Handler:
    def __init__(self, path, headers=None):
        self.path = path
        self.headers = headers or {}
        self.wfile = io.BytesIO()
        self.response_headers = {}
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


@pytest.fixture
def route_state(monkeypatch, tmp_path):
    from api import routes, profiles, helpers, wsbound
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "main")
    monkeypatch.setattr(helpers, "_security_headers", lambda handler: None)
    monkeypatch.setattr(helpers, "flush_pending_auth_cookies", lambda handler: None)
    monkeypatch.setattr(wsbound, "RESPONSES", ByteLRU(1024 * 1024))
    monkeypatch.setattr(wsbound, "COMPILES", AdmissionGate(512 * 1024 * 1024))
    return routes, profiles, wsbound, tmp_path


def test_route_cache_skips_compile_and_misses_on_write_profile_and_generation(route_state, monkeypatch):
    routes, profiles, wsbound, directory = route_state
    calls = []
    def compile_response(handler, parsed):
        calls.append(parsed.path)
        assert wsbound.DERIVED_READ.get()
        return routes.j(handler, {"session": {"messages": ["redacted"]}})
    monkeypatch.setattr(routes, "_handle_get_impl", compile_response)
    path = "/api/session?session_id=sample"
    for count in range(2):
        handler = Handler(path)
        routes.handle_get(handler, urlparse(path))
        assert handler.status == 200
    assert len(calls) == 1
    assert handler.response_headers["X-WebUI-Cache"] == "hit"
    (directory / "sample.json").write_text("{}")
    routes.handle_get(Handler(path), urlparse(path))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "other")
    routes.handle_get(Handler(path), urlparse(path))
    wsbound.RESPONSES.clear()
    routes.handle_get(Handler(path), urlparse(path))
    assert len(calls) == 4
    assert not wsbound.DERIVED_READ.get()


def test_oversized_file_never_enters_route(route_state, monkeypatch):
    routes, profiles, wsbound, directory = route_state
    with (directory / "large.json").open("wb") as output:
        output.truncate(50 * 1024 * 1024)
    monkeypatch.setattr(routes, "_handle_get_impl", lambda *args: pytest.fail("load called"))
    handler = Handler("/api/session?session_id=large")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 429
    assert handler.response_headers["Retry-After"] == "2"


def test_metadata_etag_304(route_state, monkeypatch):
    routes, profiles, wsbound, directory = route_state
    monkeypatch.setattr(routes, "_handle_get_impl", lambda handler, parsed: routes.j(handler, {"sessions": []}))
    first = Handler("/api/sessions")
    routes.handle_get(first, urlparse(first.path))
    second = Handler(first.path, {"If-None-Match": first.response_headers["ETag"]})
    routes.handle_get(second, urlparse(second.path))
    assert second.status == 304 and second.wfile.getvalue() == b""


def test_cold_metadata_never_runs_fallback_on_request_thread(monkeypatch):
    from api import routes, wsbound
    event = threading.Event()
    monkeypatch.setattr(routes, "_session_list_cache_get", lambda *args, **kwargs: (None, False))
    monkeypatch.setattr(routes, "_session_list_cache_claim_rebuild", lambda key: (event, False))
    with pytest.raises(wsbound.SnapshotPending):
        routes._get_bounded_session_list_payload(
            key=("main",), builder=lambda: pytest.fail("inline rebuild"),
            fallback_builder=lambda: pytest.fail("inline fallback"))


def test_opened_file_is_bounded_before_json_parse(tmp_path):
    from api.wsbound import READ_BUDGET, ReadBudget, read_source_text
    path = tmp_path / "growing.json"
    path.write_text('{"content":"' + 'x' * 2048 + '"}')
    token = READ_BUDGET.set(ReadBudget(1024))
    try:
        with pytest.raises(MemoryBudgetExceeded):
            read_source_text(path)
    finally:
        READ_BUDGET.reset(token)


def test_sql_history_budget_refusal_is_not_swallowed_as_empty(monkeypatch, tmp_path):
    import sqlite3
    from api import models
    from api.wsbound import READ_BUDGET, ReadBudget
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
        connection.execute("INSERT INTO messages VALUES(1,'foreign','user',?,1)", ("x" * 2048,))
    monkeypatch.setattr(models, "_active_state_db_path", lambda: database)
    token = READ_BUDGET.set(ReadBudget(1024))
    try:
        with pytest.raises(MemoryBudgetExceeded):
            models.get_state_db_session_messages("foreign")
    finally:
        READ_BUDGET.reset(token)
    token = READ_BUDGET.set(ReadBudget(4096))
    try:
        rows = models.get_state_db_session_messages("foreign")
        assert rows[0]["content"] == "x" * 2048
    finally:
        READ_BUDGET.reset(token)


def test_marked_polls_throttle_but_humans_do_not(monkeypatch):
    from api import wsbound
    monkeypatch.setattr(wsbound, "POLL_COOLDOWNS", ByteLRU(16384))
    assert wsbound.admit_poll(("profile", "sid"), "visible") == 0
    assert 1 <= wsbound.admit_poll(("profile", "sid"), "visible") <= 5
    assert wsbound.admit_poll(("profile", "sid"), "") == 0
    assert wsbound.admit_poll(("profile", "other"), "hidden") == 0
    assert 1 <= wsbound.admit_poll(("profile", "other"), "hidden") <= 60
