import json
import threading

import pytest

from tools.ws_memory_monitor import MemoryMonitor, read_health, start_memory_monitor


@pytest.fixture
def rig(tmp_path):
    cgroup = tmp_path / "cgroup"
    process = tmp_path / "proc" / "42"
    cgroup.mkdir()
    process.mkdir(parents=True)
    (process / "stat").write_text("42 (fixture process) S " + "0 " * 18 + "123 0\n")
    (process / "status").write_text("VmRSS:\t100 kB\nVmHWM:\t200 kB\n")
    (cgroup / "memory.current").write_text("102400")
    (cgroup / "memory.high").write_text("1048576")
    (cgroup / "memory.events").write_text("high 2611748\nmax 0\noom 0\noom_kill 0\n")
    (cgroup / "memory.pressure").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=10\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    monitor = MemoryMonitor(cgroup, pid=42, proc_root=tmp_path / "proc",
                            log_path=tmp_path / "memory.log", state_path=tmp_path / "state.json")
    return monitor, cgroup, process


def sample(monitor, minute):
    return monitor.sample(now=1000 + minute * 60, monotonic=1000 + minute * 60)


def high(cgroup, value):
    (cgroup / "memory.events").write_text(f"high {value}\nmax 0\noom 0\noom_kill 0\n")


def test_lifetime_counter_is_not_rate_and_log_is_durable(rig):
    monitor, _, _ = rig
    result = sample(monitor, 0)
    assert result["severity"] == "OK"
    assert result["metrics"]["high_per_minute"] is None
    assert result["metrics"]["vmhwm_bytes"] == 204800
    assert json.loads(monitor.log_path.read_text())["metrics"]["events"]["high"] == 2611748
    assert read_health(monitor.state_path, now=1000)["shed"] is False


@pytest.mark.parametrize("increment,minutes,severity", [(61, 5, "WARN"), (601, 2, "CRIT")])
def test_sustained_rates(rig, increment, minutes, severity):
    monitor, cgroup, _ = rig
    sample(monitor, 0)
    for minute in range(1, minutes + 1):
        high(cgroup, 2611748 + increment * minute)
        result = sample(monitor, minute)
        if minute < minutes:
            assert result["severity"] != severity
    assert result["severity"] == severity
    assert result["shed"] is (severity == "CRIT")


def test_counter_reset_and_gap_do_not_accumulate(rig):
    monitor, cgroup, _ = rig
    sample(monitor, 0)
    high(cgroup, 2612748)
    sample(monitor, 1)
    high(cgroup, 0)
    assert sample(monitor, 2)["severity"] == "OK"
    high(cgroup, 999999)
    result = sample(monitor, 5)
    assert result["severity"] == "CRIT"
    assert "G5_gap" in result["reasons"]
    assert result["metrics"]["high_per_minute"] is None


def test_rss_ratio_and_ten_minute_recovery(rig):
    monitor, _, process = rig
    (process / "status").write_text("VmRSS: 950 kB\nVmHWM: 1000 kB\n")
    assert sample(monitor, 0)["shed"]
    (process / "status").write_text("VmRSS: 100 kB\nVmHWM: 1000 kB\n")
    for minute in range(1, 11):
        assert sample(monitor, minute)["shed"]
    assert not sample(monitor, 11)["shed"]


def test_state_survives_oneshot_instances(rig):
    monitor, cgroup, _ = rig
    sample(monitor, 0)
    high(cgroup, 2612748)
    sample(monitor, 1)
    replacement = MemoryMonitor(cgroup, pid=42, proc_root=monitor.proc_root,
                                log_path=monitor.log_path, state_path=monitor.state_path)
    high(cgroup, 2613748)
    assert sample(replacement, 2)["shed"]


def test_fail_closed_missing_stale_future_and_bad_state(rig):
    monitor, _, _ = rig
    assert read_health(monitor.state_path, now=1000)["shed"]
    sample(monitor, 0)
    assert not read_health(monitor.state_path, now=1120)["shed"]
    assert read_health(monitor.state_path, now=1121)["shed"]
    assert read_health(monitor.state_path, now=999)["shed"]
    monitor.state_path.write_text('{"last_success":1000}')
    assert read_health(monitor.state_path, now=1000)["shed"]


def test_read_error_does_not_refresh_success_or_leak_input(rig):
    monitor, cgroup, _ = rig
    sample(monitor, 0)
    (cgroup / "memory.current").write_text("PRIVATE_FIXTURE_DO_NOT_LOG")
    result = sample(monitor, 1)
    assert result["shed"]
    assert result["last_success"] == 1000
    assert "PRIVATE_FIXTURE" not in monitor.log_path.read_text()


def test_unlimited_high_and_bounded_input(rig):
    monitor, cgroup, _ = rig
    (cgroup / "memory.high").write_text("max")
    assert "G2_high_unbounded" in sample(monitor, 0)["reasons"]
    (cgroup / "memory.events").write_text("x" * 65537)
    assert sample(monitor, 1)["severity"] == "CRIT"


def test_log_failure_never_publishes_success(rig):
    monitor, _, _ = rig
    monitor.log_path = monitor.log_path.parent
    with pytest.raises(OSError):
        sample(monitor, 0)
    assert read_health(monitor.state_path, now=1000)["shed"]


def test_parent_stop_event_prevents_sampling(rig):
    monitor, _, _ = rig
    stop = threading.Event()
    stop.set()
    thread = start_memory_monitor(monitor, stop)
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert not monitor.state_path.exists()


def test_elapsed_time_not_number_of_samples_sets_rate(rig):
    monitor, cgroup, _ = rig
    sample(monitor, 0)
    high(cgroup, 2611809)
    result = sample(monitor, 0.5)
    assert result["metrics"]["high_per_minute"] == 122
    assert result["durations"]["G1_warn"] == 30


def test_process_change_resets_sustained_rate(rig):
    monitor, cgroup, process = rig
    sample(monitor, 0)
    high(cgroup, 2612748)
    sample(monitor, 1)
    (process / "stat").write_text("42 (replacement) S " + "0 " * 18 + "456 0\n")
    high(cgroup, 2613748)
    result = sample(monitor, 2)
    assert result["metrics"]["high_per_minute"] is None
    assert result["severity"] == "OK"


def test_warning_ratio_and_exact_rate_threshold(rig):
    monitor, cgroup, process = rig
    sample(monitor, 0)
    for minute in range(1, 7):
        high(cgroup, 2611748 + 60 * minute)
        assert sample(monitor, minute)["severity"] == "OK"
    (process / "status").write_text("VmRSS: 800 kB\nVmHWM: 1000 kB\n")
    result = sample(monitor, 7)
    assert result["severity"] == "WARN"
    assert not result["shed"]


def test_oom_delta_is_immediately_critical(rig):
    monitor, cgroup, _ = rig
    sample(monitor, 0)
    (cgroup / "memory.events").write_text("high 2611748\nmax 0\noom 1\noom_kill 1\n")
    assert "oom_event" in sample(monitor, 1)["reasons"]


def test_corrupt_state_is_not_permissive(rig):
    monitor, _, _ = rig
    sample(monitor, 0)
    state = json.loads(monitor.state_path.read_text())
    state["severity"] = "CRIT"
    state["shed"] = False
    monitor.state_path.write_text(json.dumps(state))
    assert read_health(monitor.state_path, now=1000)["shed"]
    assert sample(monitor, 1)["shed"]


def test_hourly_growth_after_warmup(rig):
    monitor, cgroup, process = rig
    (cgroup / "memory.high").write_text(str(1024 ** 3))
    for minute in range(71):
        if minute == 70:
            (process / "status").write_text("VmRSS: 140000 kB\nVmHWM: 140000 kB\n")
        result = sample(monitor, minute)
    assert "G2_growth" in result["reasons"]


def test_atomic_failure_keeps_previous_heartbeat(rig, monkeypatch):
    monitor, _, _ = rig
    sample(monitor, 0)
    previous = monitor.state_path.read_bytes()

    def fail_replace(*args):
        raise OSError("fixture failure")

    monkeypatch.setattr("tools.ws_memory_monitor.os.replace", fail_replace)
    with pytest.raises(OSError):
        sample(monitor, 1)
    assert monitor.state_path.read_bytes() == previous
    assert read_health(monitor.state_path, now=1121)["shed"]


@pytest.fixture
def g4_rig(rig):
    monitor, cgroup, process = rig
    sessions = monitor.state_path.parent / "sessions"
    sessions.mkdir()
    monitor = MemoryMonitor(cgroup, pid=42, proc_root=monitor.proc_root,
                            log_path=monitor.log_path, state_path=monitor.state_path,
                            session_dir=sessions)
    return monitor, sessions


def sparse_session(path, size):
    with path.open("wb") as stream:
        stream.truncate(size)


def test_g4_is_opt_in(rig, monkeypatch):
    monitor, _, _ = rig

    def forbidden(*args):
        raise AssertionError("disabled G4 must not scan")

    monkeypatch.setattr("tools.ws_memory_monitor.os.scandir", forbidden)
    assert "g4" not in sample(monitor, 0)
    assert not (monitor.state_path.parent / "webui-compaction-queue.json").exists()


def test_g4_stat_only_direct_regular_files(g4_rig, monkeypatch):
    from pathlib import Path

    monitor, sessions = g4_rig
    sparse_session(sessions / "large.json", 9 * 1024 ** 2)
    sparse_session(sessions / "small.json", 100)
    sparse_session(sessions / "_index.json", 1000)
    sparse_session(sessions / "ignored.txt", 1000)
    (sessions / "nested.json").mkdir()
    sparse_session(sessions / "nested.json" / "hidden.json", 1000)
    (sessions / "linked.json").symlink_to(sessions / "large.json")
    original = Path.open

    def checked_open(path, *args, **kwargs):
        assert sessions not in path.parents, "session body was opened"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    result = sample(monitor, 0)
    assert result["g4"]["total_bytes"] == 9 * 1024 ** 2 + 100
    assert result["g4"]["file_count"] == 2
    assert result["g4"]["severity"] == "WARN"
    assert not result["shed"]
    queue = json.loads((monitor.state_path.parent / "webui-compaction-queue.json").read_text())
    assert queue["entries"] == [{"name": "large.json", "size_bytes": 9 * 1024 ** 2}]
    assert "large.json" not in monitor.log_path.read_text()


@pytest.mark.parametrize("size,severity", [(8 * 1024 ** 2, "OK"),
                                           (32 * 1024 ** 2, "WARN"),
                                           (32 * 1024 ** 2 + 1, "CRIT")])
def test_g4_file_thresholds(g4_rig, size, severity):
    monitor, sessions = g4_rig
    sparse_session(sessions / "session.json", size)
    assert sample(monitor, 0)["g4"]["severity"] == severity


def test_g4_cadence_survives_monitor_recreation(g4_rig):
    monitor, sessions = g4_rig
    sample(monitor, 0)
    sparse_session(sessions / "new.json", 123)
    replacement = MemoryMonitor(monitor.cgroup_path, pid=42, proc_root=monitor.proc_root,
                                log_path=monitor.log_path, state_path=monitor.state_path,
                                session_dir=sessions)
    assert sample(replacement, 1439)["g4"]["total_bytes"] == 0
    assert sample(replacement, 1440)["g4"]["total_bytes"] == 123


def test_g4_failed_scan_preserves_queue_and_stale_alerts(g4_rig, monkeypatch):
    monitor, sessions = g4_rig
    sparse_session(sessions / "session.json", 9 * 1024 ** 2)
    sample(monitor, 0)
    queue_path = monitor.state_path.parent / "webui-compaction-queue.json"
    previous_queue = queue_path.read_bytes()

    def fail(*args):
        raise OSError("PRIVATE_ERROR_NOT_FOR_LOGS")

    monkeypatch.setattr("tools.ws_memory_monitor.os.scandir", fail)
    result = sample(monitor, 1440)
    assert "G4_scan_failed" in result["g4"]["reasons"]
    assert result["g4"]["last_success"] == 1000
    assert "G4_stale" not in sample(monitor, 1560)["g4"]["reasons"]
    assert "G4_stale" in sample(monitor, 1561)["g4"]["reasons"]
    assert queue_path.read_bytes() == previous_queue
    assert "PRIVATE_ERROR" not in monitor.log_path.read_text()


def test_g4_queue_bounded_without_truncating_total(g4_rig):
    monitor, sessions = g4_rig
    for index in range(4097):
        sparse_session(sessions / f"{index}.json", 9 * 1024 ** 2)
    result = sample(monitor, 0)
    queue = json.loads((monitor.state_path.parent / "webui-compaction-queue.json").read_text())
    assert len(queue["entries"]) == 4096
    assert queue["omitted_count"] == 1
    assert result["g4"]["total_bytes"] == 4097 * 9 * 1024 ** 2
    assert "G4_store_critical" in result["g4"]["reasons"]
    assert "G4_queue_overflow" in result["g4"]["reasons"]
    assert len(monitor.state_path.read_bytes()) < 65536


def test_g4_queue_publish_failure_is_scan_failure(g4_rig, monkeypatch):
    from tools import ws_memory_monitor

    monitor, _ = g4_rig
    original = ws_memory_monitor._atomic

    def fail_queue(path, record):
        if path.name == "webui-compaction-queue.json":
            raise OSError("fixture publication failure")
        original(path, record)

    monkeypatch.setattr(ws_memory_monitor, "_atomic", fail_queue)
    result = sample(monitor, 0)
    assert result["g4"]["last_success"] is None
    assert result["g4"]["severity"] == "CRIT"
    assert result["last_success"] == 1000


def test_g4_store_limit_is_strict_and_logs_critical(g4_rig):
    monitor, sessions = g4_rig
    sparse_session(sessions / "session.json", 3_200_000_000)
    assert "G4_store_critical" not in sample(monitor, 0)["g4"]["reasons"]
    sparse_session(sessions / "extra.json", 1)
    assert "G4_store_critical" in sample(monitor, 1440)["g4"]["reasons"]
    alarms = [json.loads(line) for line in monitor.log_path.read_text().splitlines()]
    assert any(alarm.get("kind") == "G4" and alarm["severity"] == "CRIT"
               and "G4_store_critical" in alarm["reasons"] for alarm in alarms)


def test_g4_new_source_forces_scan(g4_rig):
    monitor, sessions = g4_rig
    sparse_session(sessions / "old.json", 9 * 1024 ** 2)
    sample(monitor, 0)
    replacement_dir = sessions.parent / "replacement"
    replacement_dir.mkdir()
    replacement = MemoryMonitor(monitor.cgroup_path, pid=42, proc_root=monitor.proc_root,
                                log_path=monitor.log_path, state_path=monitor.state_path,
                                session_dir=replacement_dir)
    result = sample(replacement, 1)
    assert result["g4"]["last_success"] == 1060
    assert result["g4"]["total_bytes"] == 0
    queue = json.loads((monitor.state_path.parent / "webui-compaction-queue.json").read_text())
    assert queue["entries"] == []
    assert (sessions / "old.json").stat().st_size == 9 * 1024 ** 2
