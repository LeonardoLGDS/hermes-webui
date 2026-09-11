"""R116 — the shutdown drain must fit inside the unit's real stop budget.

The defect (live acceptance 2026-09-11 17:33:52): the systemd unit deliberately
set ``TimeoutStopSec=15`` while the R114/R115 shutdown drain waits up to 45s
(clamped <=55s) for in-flight turns and their result persistence. Every
non-idle stop therefore ended in ``stop-sigterm`` timeout -> SIGKILL: the
``[shutdown-drain] outcome=...`` line never appeared, turns longer than ~13s
were unprotected, and a SIGKILL could land mid-persist. The two numbers were
never compared, so the mismatch stayed silent for two rounds.

R116 aligns the unit (15s -> 60s) and adds the missing comparison: a startup
drift check that warns when the unit's effective ``TimeoutStopUSec`` is below
the drain bound plus a margin. These tests exercise the check with an injected
resolver (mechanism: keyword ``resolver`` on
``streaming.warn_if_drain_exceeds_stop_budget``), so no real systemctl is
needed; the parser/resolver that read the unit are stubbed at the subprocess
layer too. The check is fail-open: a missing/raising/unparsable budget only
DEBUGs and never blocks startup.
"""
from __future__ import annotations

import logging
import math
import subprocess
import types

import pytest

from api import streaming

DRAIN_BOUND = streaming._SHUTDOWN_DRAIN_DEFAULT_SECONDS
MARGIN = streaming._SHUTDOWN_DRAIN_BUDGET_MARGIN_SECONDS
LOGGER_NAME = "api.streaming"


def _warnings(caplog):
    return [
        record
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno >= logging.WARNING
    ]


def test_budget_below_drain_bound_warns(caplog):
    """The old unit value (15s) must warn and name both numbers + the file."""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        warned = streaming.warn_if_drain_exceeds_stop_budget(
            resolver=lambda: 15.0,
            drain_bound_seconds=DRAIN_BOUND,
        )
    assert warned is True
    records = _warnings(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "15" in message
    assert "45" in message
    assert streaming._SHUTDOWN_UNIT_PATH in message


@pytest.mark.parametrize("budget", [55.0, 60.0, 90.0])
def test_budget_at_or_above_margin_is_silent(caplog, budget):
    """bound + margin (45 + 10 = 55) and up — including the aligned 60s — are silent."""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        warned = streaming.warn_if_drain_exceeds_stop_budget(
            resolver=lambda: budget,
            drain_bound_seconds=DRAIN_BOUND,
        )
    assert warned is False
    assert _warnings(caplog) == []


def test_budget_one_second_below_margin_warns(caplog):
    """The comparison is strict: bound + margin - 1s still warns."""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        warned = streaming.warn_if_drain_exceeds_stop_budget(
            resolver=lambda: DRAIN_BOUND + MARGIN - 1.0,
            drain_bound_seconds=DRAIN_BOUND,
        )
    assert warned is True
    assert _warnings(caplog)


def test_infinity_budget_is_silent(caplog):
    """An unbounded (disabled) stop timeout cannot be outrun by the drain."""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        warned = streaming.warn_if_drain_exceeds_stop_budget(
            resolver=lambda: math.inf,
            drain_bound_seconds=DRAIN_BOUND,
        )
    assert warned is False
    assert _warnings(caplog) == []


def test_resolver_raising_is_fail_open(caplog):
    """A raising resolver must not propagate: DEBUG only, startup continues."""

    def boom():
        raise RuntimeError("systemd unavailable")

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        warned = streaming.warn_if_drain_exceeds_stop_budget(
            resolver=boom,
            drain_bound_seconds=DRAIN_BOUND,
        )
    assert warned is False
    assert _warnings(caplog) == []
    assert any(record.levelno == logging.DEBUG for record in caplog.records)


def test_resolver_unavailable_is_debug_only(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        warned = streaming.warn_if_drain_exceeds_stop_budget(
            resolver=lambda: None,
            drain_bound_seconds=DRAIN_BOUND,
        )
    assert warned is False
    assert _warnings(caplog) == []
    assert any(record.levelno == logging.DEBUG for record in caplog.records)


def test_default_resolver_is_used_when_not_injected(monkeypatch, caplog):
    monkeypatch.setattr(streaming, "systemd_stop_budget_seconds", lambda: 15.0)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        warned = streaming.warn_if_drain_exceeds_stop_budget(
            drain_bound_seconds=DRAIN_BOUND,
        )
    assert warned is True
    assert _warnings(caplog)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("15s", 15.0),
        ("1min", 60.0),
        ("1min 30s", 90.0),
        ("2h 30min", 9000.0),
        ("500ms", 0.5),
        ("15000000", 15.0),
        ("TimeoutStopUSec=15s", 15.0),
    ],
)
def test_parse_systemd_time_seconds(raw, expected):
    assert streaming._parse_systemd_time_seconds(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["infinity", "infinite", "0", "0us"])
def test_parse_systemd_time_seconds_unbounded(raw):
    """0 disables the timeout in systemd, so it is treated as unbounded."""
    assert streaming._parse_systemd_time_seconds(raw) == math.inf


@pytest.mark.parametrize("raw", ["", "   ", "garbage", "unknown"])
def test_parse_systemd_time_seconds_unparsable(raw):
    assert streaming._parse_systemd_time_seconds(raw) is None


def test_resolver_reads_systemctl_value(monkeypatch):
    """The resolver runs systemctl bounded and parses its bare value form."""
    seen = {}

    class _Proc:
        returncode = 0
        stdout = "1min\n"

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(streaming, "subprocess", types.SimpleNamespace(run=fake_run))
    assert streaming.systemd_stop_budget_seconds() == 60.0
    assert seen["cmd"] == [
        "systemctl", "--user", "show", "-p", "TimeoutStopUSec", "--value",
        streaming._SHUTDOWN_UNIT_NAME,
    ]
    assert 0 < seen["kwargs"]["timeout"] <= 1.0


def test_resolver_timeout_is_fail_open(monkeypatch, caplog):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0.5))

    monkeypatch.setattr(streaming, "subprocess", types.SimpleNamespace(run=fake_run))
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert streaming.systemd_stop_budget_seconds() is None
    assert _warnings(caplog) == []


def test_resolver_nonzero_exit_is_fail_open(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        streaming, "subprocess", types.SimpleNamespace(run=lambda *a, **k: _Proc())
    )
    assert streaming.systemd_stop_budget_seconds() is None
