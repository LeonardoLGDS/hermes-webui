"""Regression coverage for half-open SSE peer reaping (#7105)."""

import errno
import io
from pathlib import Path
import queue
import socket

import pytest

import api.routes as routes
import api.streaming as streaming


ROOT = Path(__file__).resolve().parents[1]


class FakeConnection:
    def __init__(self, *, peek=b"x", socket_error=0):
        self.peek = peek
        self.socket_error = socket_error
        self.timeout = None
        self.options = []
        self.recv_flags = None

    def settimeout(self, seconds):
        self.timeout = seconds

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))

    def getsockopt(self, level, option):
        assert (level, option) == (socket.SOL_SOCKET, socket.SO_ERROR)
        return self.socket_error

    def fileno(self):
        return 42

    def recv(self, size, flags=0):
        self.recv_flags = flags
        return self.peek[:size]


class FakeHandler:
    def __init__(self, connection=None):
        self.connection = connection or FakeConnection()
        self.wfile = io.BytesIO()
        self.headers_sent = []

    def send_response(self, status):
        self.headers_sent.append(("status", status))

    def send_header(self, name, value):
        self.headers_sent.append((name, value))


def test_sse_guard_arms_keepalive_and_unacked_data_timeout():
    connection = FakeConnection()
    handler = FakeHandler(connection)

    streaming._sse_set_write_deadline(handler, seconds=12)

    assert connection.timeout == 12
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in connection.options
    if hasattr(socket, "TCP_USER_TIMEOUT"):
        assert (
            socket.IPPROTO_TCP,
            socket.TCP_USER_TIMEOUT,
            12_000,
        ) in connection.options


def test_peer_probe_detects_fin_and_pending_socket_error(monkeypatch):
    fin = FakeConnection(peek=b"")
    monkeypatch.setattr(streaming._select, "select", lambda *_args: ([fin], [], []))
    assert streaming._sse_peer_is_dead(FakeHandler(fin)) is True
    assert fin.recv_flags & socket.MSG_PEEK

    alive = FakeConnection(peek=b"x")
    monkeypatch.setattr(streaming._select, "select", lambda *_args: ([alive], [], []))
    assert streaming._sse_peer_is_dead(FakeHandler(alive)) is False

    errored = FakeConnection(socket_error=errno.ETIMEDOUT)
    monkeypatch.setattr(streaming._select, "select", lambda *_args: ([], [], []))
    assert streaming._sse_peer_is_dead(FakeHandler(errored)) is True


def test_dead_session_events_peer_unsubscribes(monkeypatch):
    subscriber = object()
    unsubscribed = []
    handler = FakeHandler()

    class EmptyQueue:
        def get(self, timeout):
            raise queue.Empty

    subscriber = EmptyQueue()
    monkeypatch.setattr(routes, "end_sse_headers", lambda _handler: None)
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: None)
    monkeypatch.setattr(routes, "subscribe_session_events", lambda: subscriber)
    monkeypatch.setattr(routes, "unsubscribe_session_events", unsubscribed.append)
    monkeypatch.setattr(
        routes,
        "_sse_write_heartbeat",
        lambda _handler, _payload=b"": (_ for _ in ()).throw(ConnectionResetError()),
    )

    assert routes._handle_session_events_stream(handler) is True
    assert unsubscribed == [subscriber]


def test_all_long_lived_sse_heartbeats_use_peer_guard():
    source = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")

    assert "handler.wfile.write(b\": heartbeat\\n\\n\")" not in source
    assert "handler.wfile.write(b': keepalive\\n\\n')" not in source
    assert "handler.wfile.write(b\": terminal heartbeat\\n\\n\")" not in source
    assert source.count("_sse_write_heartbeat(handler") >= 9

    terminal_start = source.index("def _handle_terminal_output")
    terminal_end = source.index("def _gateway_sse_probe_payload", terminal_start)
    assert "except _CLIENT_DISCONNECT_ERRORS:" in source[terminal_start:terminal_end]
