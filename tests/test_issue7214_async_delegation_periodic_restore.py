"""Regression coverage for durable async-delegation restore retries (#7214)."""

from __future__ import annotations

import sys
import types

import pytest

from api import process_event_utils as peu


class _FakeTimer:
    def __init__(self, interval, function):
        self.interval = float(interval)
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        assert self.started
        assert not self.cancelled
        self.function()


@pytest.fixture(autouse=True)
def _reset_retry_state():
    peu._reset_legacy_async_delivery_dedupe_for_tests()
    yield
    peu._reset_legacy_async_delivery_dedupe_for_tests()


def _install_fake_restore(monkeypatch, restore, durable=None):
    tools = types.ModuleType("tools")
    async_delegation = types.ModuleType("tools.async_delegation")
    async_delegation.restore_undelivered_completions = restore
    if durable is not None:
        async_delegation.get_durable_delegation = durable
    tools.async_delegation = async_delegation
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", async_delegation)


def _capture_timers(monkeypatch):
    timers = []

    def build_timer(interval, function):
        timer = _FakeTimer(interval, function)
        timers.append(timer)
        return timer

    monkeypatch.setattr(peu.threading, "Timer", build_timer)
    return timers


def test_public_retry_scheduler_keeps_idle_pending_backlog_alive(monkeypatch):
    """The existing event-facing scheduler must enter the periodic retry chain."""
    restored_counts = iter([1, 0])
    restore_calls = []
    durable_reads = []

    def restore(queue):
        restore_calls.append(queue)
        return next(restored_counts)

    def get_durable(delegation_id):
        durable_reads.append(delegation_id)
        return {"delivery_state": "pending"}

    _install_fake_restore(monkeypatch, restore, get_durable)
    timers = _capture_timers(monkeypatch)
    completion_queue = object()
    evt = {"type": "async_delegation", "delegation_id": "deleg_public"}

    assert peu.schedule_async_delegation_claim_retry(evt, completion_queue, delay=0.0)
    assert durable_reads == ["deleg_public"]
    assert len(timers) == 1

    timers[0].fire()

    assert restore_calls == [completion_queue]
    assert len(timers) == 2, "restored durable work must retry without another turn"
    assert timers[1].interval == peu.ASYNC_DELIVERY_CLAIM_RETRY_SECONDS

    timers[1].fire()

    assert restore_calls == [completion_queue, completion_queue]
    assert len(timers) == 2
    assert peu.async_delivery_retry_timer_count() == 0


def test_successful_restore_rearms_until_durable_backlog_clears(monkeypatch):
    """A positive restore count must keep one independent retry sweep alive."""
    restored_counts = iter([1, 0])
    restore_calls = []

    def restore(queue):
        restore_calls.append(queue)
        return next(restored_counts)

    _install_fake_restore(monkeypatch, restore)
    timers = _capture_timers(monkeypatch)
    completion_queue = object()

    assert peu._arm_async_delegation_restore_sweep(completion_queue, 0.0)
    assert len(timers) == 1

    timers[0].fire()

    assert restore_calls == [completion_queue]
    assert len(timers) == 2, "pending durable work must arm a follow-up sweep"
    assert timers[1].interval == peu.ASYNC_DELIVERY_CLAIM_RETRY_SECONDS
    assert peu.async_delivery_retry_timer_count() == 1

    timers[1].fire()

    assert restore_calls == [completion_queue, completion_queue]
    assert len(timers) == 2, "zero restored rows must stop the retry chain"
    assert peu.async_delivery_retry_timer_count() == 0


def test_restore_failure_keeps_short_routing_retry(monkeypatch):
    """Restore failures retain the existing short retry instead of waiting 301s."""
    def restore(_queue):
        raise RuntimeError("synthetic restore failure")

    _install_fake_restore(monkeypatch, restore)
    timers = _capture_timers(monkeypatch)

    assert peu._arm_async_delegation_restore_sweep(object(), 0.0)
    timers[0].fire()

    assert len(timers) == 2
    assert timers[1].interval == peu.ASYNC_DELIVERY_ROUTING_RETRY_SECONDS
    assert peu.async_delivery_retry_timer_count() == 1


def test_reset_cancels_periodic_restore_timer(monkeypatch):
    """Test/process teardown must still retire the single shared timer."""
    _install_fake_restore(monkeypatch, lambda _queue: 1)
    timers = _capture_timers(monkeypatch)

    assert peu._arm_async_delegation_restore_sweep(object(), 30.0)
    assert peu.async_delivery_retry_timer_count() == 1

    peu._reset_legacy_async_delivery_dedupe_for_tests()

    assert timers[0].cancelled is True
    assert peu.async_delivery_retry_timer_count() == 0
