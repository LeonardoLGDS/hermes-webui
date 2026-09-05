"""Sidebar list must survive malformed rows and bounded-builder failures."""

import io
import json
from urllib.parse import urlparse

import pytest


class _Handler:
    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.wfile = io.BytesIO()
        self.response_headers = {}
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = self.headers[key] = value

    def end_headers(self):
        return None

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


@pytest.fixture
def list_route_state(monkeypatch, tmp_path):
    from api import helpers, profiles, routes, wsbound

    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_session_attention_summary", lambda _sid: None)
    monkeypatch.setattr(routes, "_session_bg_status_fields", lambda _sid, **_kwargs: {})
    monkeypatch.setattr(helpers, "_security_headers", lambda _handler: None)
    monkeypatch.setattr(helpers, "flush_pending_auth_cookies", lambda _handler: None)
    monkeypatch.setattr(wsbound, "RESPONSES", wsbound.ByteLRU(1024 * 1024))
    routes._session_list_cache_clear()
    try:
        yield routes, wsbound, tmp_path
    finally:
        routes._session_list_cache_clear()


def _edge_rows():
    return [
        {
            "session_id": "good-new",
            "title": "Good new",
            "profile": "default",
            "message_count": 2,
            "last_message_at": 200.0,
        },
        {
            "session_id": "edge-corrupt",
            "title": "Corrupt timestamp",
            "profile": "default",
            "message_count": 1,
            "last_message_at": "not-a-number",
            "updated_at": 50.0,
        },
        {
            "session_id": "good-old",
            "title": "Good old",
            "profile": "default",
            "message_count": 3,
            "last_message_at": 100.0,
        },
    ]


def test_builder_survives_nonnumeric_session_timestamp(monkeypatch):
    from api import routes

    rows = _edge_rows()
    monkeypatch.setattr(
        routes, "all_sessions", lambda diag=None, include_lineage_metadata=False: [dict(row) for row in rows]
    )
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_prune_orphaned_webui_zero_message_sessions", lambda rows, **_kwargs: list(rows))
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        exclude_hidden=True,
        visible_only=True,
        sidebar_source="webui",
    )

    assert [row["session_id"] for row in payload["sessions"]] == [
        "good-new",
        "good-old",
        "edge-corrupt",
    ]
    assert "degraded" not in payload


def test_cold_builder_failure_returns_degraded_index_payload(list_route_state, monkeypatch, caplog):
    routes, wsbound, directory = list_route_state
    index_rows = [
        {
            "session_id": "index-new",
            "title": "Index new",
            "profile": "default",
            "message_count": 2,
            "last_message_at": 200.0,
            "source": "webui",
            "source_tag": "webui",
        },
        {
            "session_id": "index-hidden",
            "title": "Hidden",
            "profile": "default",
            "message_count": 1,
            "last_message_at": 190.0,
            "default_hidden": True,
            "source": "webui",
            "source_tag": "webui",
        },
        {"session_id": ""},
        "not-a-row",
    ]
    (directory / "_index.json").write_text(json.dumps(index_rows), encoding="utf-8")

    def bounded_failure(*_args, **_kwargs):
        raise wsbound.SnapshotPending()

    monkeypatch.setattr(routes, "_get_bounded_session_list_payload", bounded_failure)
    handler = _Handler("/api/sessions?sidebar_source=webui&exclude_hidden=1")

    with caplog.at_level("WARNING", logger="api.routes"):
        routes.handle_get(handler, urlparse(handler.path))

    body = handler.json_body()
    assert handler.status == 200
    assert [row["session_id"] for row in body["sessions"]] == ["index-new"]
    assert body["degraded"] is True
    assert body["error"] == "SnapshotPending"
    assert handler.response_headers["X-WebUI-List-Degraded"] == "1"
    assert "Retry-After" not in handler.response_headers
    assert wsbound.RESPONSES.snapshot()["entries"] == 0
    assert any("serving degraded index payload" in record.message for record in caplog.records)


def test_ordinary_builder_failure_keeps_http_200_contract(list_route_state, monkeypatch, caplog):
    routes, _wsbound, _directory = list_route_state

    def bounded_failure(*_args, **_kwargs):
        raise RuntimeError("edge record")

    monkeypatch.setattr(routes, "_get_bounded_session_list_payload", bounded_failure)
    handler = _Handler("/api/sessions?sidebar_source=webui&exclude_hidden=1")

    with caplog.at_level("WARNING", logger="api.routes"):
        routes.handle_get(handler, urlparse(handler.path))

    body = handler.json_body()
    assert handler.status == 200
    assert body["sessions"] == []
    assert body["degraded"] is True
    assert body["error"] == "RuntimeError"
    assert handler.response_headers["X-WebUI-List-Degraded"] == "1"
    assert any(
        record.exc_info is not None and "serving degraded index payload" in record.message
        for record in caplog.records
    )


def test_shared_list_boundary_never_hits_unbound_flight_json(list_route_state, monkeypatch):
    routes, wsbound, _directory = list_route_state

    def impl_failure(*_args, **_kwargs):
        raise wsbound.SnapshotPending()

    monkeypatch.setattr(routes, "_handle_get_impl", impl_failure)
    handler = _Handler("/api/sessions?sidebar_source=webui&exclude_hidden=1")

    routes.handle_get(handler, urlparse(handler.path))

    body = handler.json_body()
    assert handler.status == 503
    assert body == {"error": "metadata_refresh_pending"}
    assert "Retry-After" in handler.response_headers


def test_one_bad_response_row_cannot_fail_the_whole_list(list_route_state, monkeypatch):
    routes, _wsbound, _directory = list_route_state
    original = routes._sidebar_session_response_item

    def failing_projection(row, **kwargs):
        if row.get("session_id") == "bad-row":
            raise ValueError("edge row projection")
        return original(row, **kwargs)

    monkeypatch.setattr(routes, "_sidebar_session_response_item", failing_projection)
    response = routes._session_list_payload_to_response(
        {
            "sessions": [
                {"session_id": "good-row", "title": "Good", "message_count": 1, "last_message_at": 20.0},
                {"session_id": "bad-row", "title": "Bad", "message_count": 1, "last_message_at": 10.0},
            ],
            "cli_count": 0,
            "active_profile": "default",
        }
    )

    by_id = {row["session_id"]: row for row in response["sessions"]}
    assert set(by_id) == {"good-row", "bad-row"}
    assert by_id["bad-row"]["title"] == "Session unavailable"
    assert by_id["bad-row"]["_sidebar_response_degraded"] is True
    assert response["degraded"] is True


def test_metadata_open_cache_miss_reads_oversized_sidecar_once(list_route_state, monkeypatch):
    routes, wsbound, directory = list_route_state
    from api import models

    sid = "metadata-open-once"
    record = {
        "session_id": sid,
        "title": "Cached metadata",
        "created_at": 100.0,
        "updated_at": 200.0,
        "profile": "default",
        "message_count": 2,
        "context_length": 128000,
        "threshold_tokens": 1000,
        # This oversized pre-messages field defeats the 64-KiB prefix scanner and
        # reproduces the legacy full-read fallback without shipping a real transcript.
        "tool_calls": [{"blob": "x" * 70000}],
        "messages": [
            {"role": "user", "content": "hello", "timestamp": 150.0},
            {"role": "assistant", "content": "hi", "timestamp": 200.0},
        ],
    }
    (directory / f"{sid}.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", directory / "_index.json")

    calls = []
    original_loader = models.Session.load_metadata_only

    def counted_loader(session_id, *args, **kwargs):
        calls.append(session_id)
        return original_loader(session_id, *args, **kwargs)

    monkeypatch.setattr(models.Session, "load_metadata_only", staticmethod(counted_loader))
    refusing_gate = wsbound.AdmissionGate(512 * 1024 * 1024)
    monkeypatch.setattr(
        refusing_gate,
        "admit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("metadata admitted")),
    )
    monkeypatch.setattr(wsbound, "COMPILES", refusing_gate)
    with wsbound.SESSION_LOAD_FLIGHT_LOCK:
        wsbound.SESSION_LOAD_FLIGHTS.clear()

    handler = _Handler(f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    body = handler.json_body()
    assert handler.status == 200
    assert calls == [sid]
    assert body["session"]["session_id"] == sid
    assert body["session"]["message_count"] == 2
    assert body["session"]["messages"] == []
    assert wsbound.SESSION_LOAD_FLIGHTS == {}


def test_metadata_open_uses_fresh_list_cache_without_sidecar_read(list_route_state, monkeypatch):
    routes, wsbound, _directory = list_route_state
    from api import models

    sid = "metadata-open-cache"
    row = {
        "session_id": sid,
        "title": "List-cache metadata",
        "profile": "default",
        "message_count": 7,
        "created_at": 100.0,
        "updated_at": 200.0,
        "last_message_at": 200.0,
        "is_streaming": False,
        "cron_running": False,
    }
    key = routes._metadata_open_list_cache_key("default")
    assert key is not None
    routes._session_list_cache_set(
        key,
        {
            "sessions": [row],
            "sidebar_reference_sessions": [],
            "cli_count": 0,
            "webui_session_count": 1,
            "active_profile": "default",
        },
    )

    calls = []

    def refusing_loader(session_id, *_args, **_kwargs):
        calls.append(session_id)
        raise AssertionError("fresh list cache must not read the sidecar")

    monkeypatch.setattr(models.Session, "load_metadata_only", staticmethod(refusing_loader))
    refusing_gate = wsbound.AdmissionGate(512 * 1024 * 1024)
    monkeypatch.setattr(
        refusing_gate,
        "admit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("metadata admitted")),
    )
    monkeypatch.setattr(wsbound, "COMPILES", refusing_gate)
    with wsbound.SESSION_LOAD_FLIGHT_LOCK:
        wsbound.SESSION_LOAD_FLIGHTS.clear()

    handler = _Handler(f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    body = handler.json_body()
    assert handler.status == 200
    assert calls == []
    assert body["session"]["session_id"] == sid
    assert body["session"]["title"] == "List-cache metadata"
    assert body["session"]["message_count"] == 7
    assert body["session"]["messages"] == []
    assert handler.response_headers["X-WebUI-Metadata-Cache"] == "session-list"
    assert wsbound.SESSION_LOAD_FLIGHTS == {}
