import io
from unittest.mock import MagicMock

import server
from server import Handler


def _handler(host):
    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/"
    handler._req_t0 = 0.0
    handler.headers = {"Host": host}
    handler.wfile = io.BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    return handler


def test_host_validation_accepts_configured_tailnet_host():
    assert _handler("100.81.211.55:8787")._host_allowed() is True
    assert _handler("127.0.0.1:49152")._host_allowed() is True


def test_host_validation_rejects_unknown_host_before_routing(monkeypatch):
    handler = _handler("evil-host-probe.invalid")
    reset_state = MagicMock()
    get_profile_cookie = MagicMock()
    check_auth = MagicMock()
    handle_get = MagicMock()
    monkeypatch.setattr(server, "reset_trusted_auth_request_state", reset_state)
    monkeypatch.setattr(server, "get_profile_cookie", get_profile_cookie)
    monkeypatch.setattr(server, "check_auth", check_auth)
    monkeypatch.setattr(server, "handle_get", handle_get)

    Handler.do_GET(handler)

    handler.send_response.assert_called_once_with(403)
    handler.send_header.assert_called_once_with("Content-Length", "0")
    handler.end_headers.assert_called_once()
    assert handler.wfile.getvalue() == b""
    reset_state.assert_called_once_with(handler)
    get_profile_cookie.assert_not_called()
    check_auth.assert_not_called()
    handle_get.assert_not_called()
