"""Regression coverage for issue #7234 rejected write request framing."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

import api.auth as auth
import api.routes as routes


class _Handler:
    def __init__(self, *, command="POST", path="/api/session/new", headers=None, body=b'{"probe":1}'):
        self.command = command
        self.path = path
        self.headers = dict(headers or {})
        self.client_address = ("127.0.0.1", 12345)
        self.request = SimpleNamespace()
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _force_password_auth(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: None)
    monkeypatch.setattr(auth, "ensure_trusted_auth_session", lambda _handler: None)


@pytest.mark.parametrize("path", ["/api/auth/logout", "/api/session/new"])
def test_auth_rejected_post_closes_connection_without_draining_body(monkeypatch, path):
    _force_password_auth(monkeypatch)
    handler = _Handler(path=path)

    assert auth.check_auth(handler, SimpleNamespace(path=path, query="")) is False

    assert handler.status == 401
    assert handler.close_connection is True
    assert handler.rfile.tell() == 0


def test_profile_forbidden_post_closes_connection_without_draining_body(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: "session-cookie")
    monkeypatch.setattr(auth, "verify_session", lambda _cookie: True)
    monkeypatch.setattr(
        auth,
        "ensure_trusted_auth_session",
        lambda _handler: {"auth_type": "trusted", "bound_profile": "other"},
    )
    monkeypatch.setattr(auth, "trusted_session_allows_active_profile", lambda _info: False)
    handler = _Handler()

    assert auth.check_auth(handler, SimpleNamespace(path=handler.path, query="")) is False

    assert handler.status == 403
    assert handler.close_connection is True
    assert handler.rfile.tell() == 0


def test_auth_rejected_get_keeps_connection_reusable(monkeypatch):
    _force_password_auth(monkeypatch)
    handler = _Handler(command="GET")

    assert auth.check_auth(handler, SimpleNamespace(path=handler.path, query="")) is False

    assert handler.status == 401
    assert handler.close_connection is False


@pytest.mark.parametrize(
    ("command", "route"),
    [
        ("POST", routes.handle_post),
        ("PATCH", routes.handle_patch),
        ("DELETE", routes.handle_delete),
        ("PUT", routes.handle_put),
    ],
)
def test_csrf_rejected_write_closes_connection_without_draining_body(
    monkeypatch,
    command,
    route,
):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    handler = _Handler(
        command=command,
        headers={
            "Origin": "https://evil.example",
            "Host": "127.0.0.1:8787",
            "Content-Length": "11",
        },
    )

    route(handler, SimpleNamespace(path="/api/providers/delete", query=""))

    assert handler.status == 403
    assert handler.close_connection is True
    assert handler.rfile.tell() == 0


def test_pre_csrf_deprecation_response_closes_unread_post_body(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    handler = _Handler(path="/api/process-complete-ack")

    routes.handle_post(handler, SimpleNamespace(path=handler.path, query=""))

    assert handler.status == 410
    assert handler.close_connection is True
    assert handler.rfile.tell() == 0
