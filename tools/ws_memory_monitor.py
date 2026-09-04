"""Bounded cgroup-v2 telemetry; never changes limits or restarts processes."""

import argparse
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import stat
import threading
import time


PERIOD = 60
MAX_BYTES = 65536
G4_PERIOD = 24 * 60 * 60
G4_STALE = 26 * 60 * 60
G4_QUEUE_LIMIT = 4096
G4_QUEUE_BYTES = 8 * 1024 ** 2
G4_CRITICAL_BYTES = 32 * 1024 ** 2
G4_STORE_BYTES = 3_200_000_000
DEFAULT_LOG = Path("/home/ops/webui-mem.log")
DEFAULT_STATE = Path("/home/ops/webui-mem-state.json")
LOGGER = logging.getLogger(__name__)


def _read(path):
    with Path(path).open() as stream:
        text = stream.read(MAX_BYTES + 1)
    if len(text) > MAX_BYTES:
        raise ValueError("oversized telemetry")
    return text


def _number(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid telemetry number")
    return result


def _load(path):
    state = json.loads(_read(path))
    if state["version"] != 1 or type(state["shed"]) is not bool:
        raise ValueError("invalid telemetry state")
    if (state["severity"] not in {"OK", "WARN", "CRIT"}
            or not isinstance(state["reasons"], list)
            or len(state["reasons"]) > 16
            or any(not isinstance(reason, str) or len(reason) > 64 for reason in state["reasons"])
            or (state["severity"] == "CRIT" and not state["shed"])):
        raise ValueError("invalid telemetry health")
    _number(state["last_success"])
    return state


def read_health(state_path=DEFAULT_STATE, *, now=None):
    """Parent admission hook: missing, malformed or stale state always sheds."""
    now = time.time() if now is None else now
    try:
        state = _load(state_path)
        age = now - state["last_success"]
        if not 0 <= age <= 2 * PERIOD:
            raise ValueError("stale heartbeat")
        return {"severity": state["severity"], "shed": state["shed"],
                "age_seconds": age, "reasons": state["reasons"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {"severity": "CRIT", "shed": True, "reasons": ["G5_stale_or_invalid"]}


def _append(path, record):
    payload = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic(path, record):
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(record, stream, separators=(",", ":"), allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class MemoryMonitor:
    """One owner per state/log pair; fixed-size state, no sample history list."""

    def __init__(self, cgroup_path, *, pid=None, proc_root="/proc",
                 log_path=DEFAULT_LOG, state_path=DEFAULT_STATE, session_dir=None):
        self.cgroup_path = Path(cgroup_path)
        self.pid = os.getpid() if pid is None else int(pid)
        self.proc_root = Path(proc_root)
        self.log_path = Path(log_path)
        self.state_path = Path(state_path)
        self.session_dir = Path(session_dir) if session_dir is not None else None
        self._lock = threading.Lock()

    def _scan_sessions(self, now):
        entries = []
        summary = {"total_bytes": 0, "file_count": 0, "oversized_count": 0,
                   "critical_file_count": 0, "omitted_count": 0}
        # WHY: growth feeds OOM recurrence; stat-only discovery never loads sessions.
        with os.scandir(self.session_dir) as directory:
            for entry in directory:
                if entry.name == "_index.json" or not entry.name.endswith(".json"):
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                summary["total_bytes"] += metadata.st_size
                summary["file_count"] += 1
                if metadata.st_size > G4_CRITICAL_BYTES:
                    summary["critical_file_count"] += 1
                if metadata.st_size > G4_QUEUE_BYTES:
                    summary["oversized_count"] += 1
                    if len(entries) < G4_QUEUE_LIMIT:
                        entries.append({"name": entry.name, "size_bytes": metadata.st_size})
                    else:
                        summary["omitted_count"] += 1
        _atomic(self.state_path.with_name("webui-compaction-queue.json"),
                {"version": 1, "timestamp": now, "entries": entries,
                 "omitted_count": summary["omitted_count"]})
        return summary

    def _g4(self, previous, now):
        source = hashlib.sha256(os.fsencode(self.session_dir.absolute())).hexdigest()
        defaults = {"source": source, "started_at": now, "last_attempt": None,
                    "last_success": None, "scan_failed": False, "total_bytes": 0,
                    "file_count": 0, "oversized_count": 0, "critical_file_count": 0,
                    "omitted_count": 0}
        saved = previous.get("g4") if previous else None
        summary = defaults.copy()
        if isinstance(saved, dict) and saved.get("source") == source:
            try:
                for key in defaults:
                    value = saved[key]
                    if key not in {"source", "scan_failed"}:
                        if value is None and key in {"last_attempt", "last_success"}:
                            continue
                        _number(value)
                        if type(value) not in {int, float}:
                            raise ValueError("invalid G4 number")
                if type(saved["scan_failed"]) is not bool or saved["started_at"] is None:
                    raise ValueError("invalid G4 state")
                summary = {key: saved[key] for key in defaults}
            except (KeyError, TypeError, ValueError):
                summary["scan_failed"] = True
        anchor = summary["last_success"]
        if anchor is None:
            anchor = summary["started_at"]
        stale = now < anchor or now - anchor > G4_STALE
        attempt = summary["last_attempt"]
        if attempt is None or now < attempt or now - attempt >= G4_PERIOD:
            summary["last_attempt"] = now
            try:
                scanned = self._scan_sessions(now)
            except (OSError, ValueError, OverflowError):
                summary["scan_failed"] = True
            else:
                summary.update(scanned)
                summary["scan_failed"] = False
                summary["last_success"] = now
        reasons = []
        critical = False
        for condition, reason, is_critical in (
                (summary["scan_failed"], "G4_scan_failed", True),
                (stale, "G4_stale", True),
                (summary["oversized_count"] > 0, "G4_compaction_needed", False),
                (summary["critical_file_count"] > 0, "G4_session_critical", True),
                (summary["total_bytes"] > G4_STORE_BYTES, "G4_store_critical", True),
                (summary["omitted_count"] > 0, "G4_queue_overflow", False)):
            if condition:
                reasons.append(reason)
                critical |= is_critical
        summary["reasons"] = reasons
        summary["severity"] = "CRIT" if critical else "WARN" if reasons else "OK"
        return summary

    def _metrics(self):
        current = int(_read(self.cgroup_path / "memory.current"))
        high_text = _read(self.cgroup_path / "memory.high").strip()
        high = None if high_text == "max" else int(high_text)
        events = {}
        for line in _read(self.cgroup_path / "memory.events").splitlines():
            name, value = line.split()
            if name in {"high", "max", "oom", "oom_kill", "oom_group_kill"}:
                events[name] = int(value)
        for required in ("high", "max", "oom", "oom_kill"):
            _number(events[required])
        pressure = {}
        for line in _read(self.cgroup_path / "memory.pressure").splitlines():
            kind, *fields = line.split()
            if kind in {"some", "full"}:
                parsed = dict(field.split("=", 1) for field in fields)
                pressure[kind] = {name: _number(parsed[name])
                                  for name in ("avg10", "avg60", "avg300", "total")}
        if set(pressure) != {"some", "full"}:
            raise ValueError("missing pressure")
        process = self.proc_root / str(self.pid)
        before = _read(process / "stat").rsplit(")", 1)[1].split()[19]
        status = {}
        for line in _read(process / "status").splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                name, value, unit = line.split()
                if unit != "kB":
                    raise ValueError("invalid status unit")
                status[name.rstrip(":")] = int(value) * 1024
        after = _read(process / "stat").rsplit(")", 1)[1].split()[19]
        if before != after:
            raise ValueError("process changed")
        for value in (current, status["VmRSS"], status["VmHWM"]):
            _number(value)
        if high is not None and high <= 0:
            raise ValueError("invalid memory.high")
        identity = self.cgroup_path.stat()
        return {"current_bytes": current, "high_bytes": high, "events": events,
                "pressure": pressure, "rss_bytes": status["VmRSS"],
                "vmhwm_bytes": status["VmHWM"],
                "identity": [identity.st_dev, identity.st_ino, self.pid, before]}

    def sample(self, *, now=None, monotonic=None):
        """Sample once, append+fsync receipt, then atomically publish heartbeat."""
        now = time.time() if now is None else now
        monotonic = time.monotonic() if monotonic is None else monotonic
        with self._lock:
            previous = None
            invalid = False
            try:
                previous = _load(self.state_path)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, KeyError, TypeError):
                invalid = True
            try:
                metrics = self._metrics()
                record = self._evaluate(metrics, previous, now, monotonic, invalid)
            except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError):
                # WHY: 09-01/02 OOMs and a dead sampler made silence unsafe.
                record = {"version": 1, "timestamp": now, "last_success":
                          previous["last_success"] if previous else 0,
                          "severity": "CRIT", "shed": True,
                          "reasons": ["G5_sample_error"]}
            if self.session_dir is not None:
                record["g4"] = self._g4(previous, now)
                if record["g4"]["severity"] != "OK":
                    _append(self.log_path, {"timestamp": now, "kind": "G4", **record["g4"]})
                    LOGGER.warning("webui_memory G4 %s %s", record["g4"]["severity"],
                                   ",".join(record["g4"]["reasons"]))
            _append(self.log_path, record)
            _atomic(self.state_path, record)
            if record["severity"] != "OK":
                LOGGER.warning("webui_memory %s %s", record["severity"],
                               ",".join(record["reasons"]))
            return record

    def _evaluate(self, metrics, previous, now, monotonic, invalid):
        reasons = []
        critical = invalid
        if invalid:
            reasons.append("G5_invalid_state")
        elapsed = 0
        continuous = False
        if previous and "metrics" in previous:
            elapsed = monotonic - previous["monotonic"]
            continuous = (0 < elapsed <= 2 * PERIOD
                          and metrics["identity"] == previous["metrics"]["identity"]
                          and 0 <= now - previous["last_success"] <= 2 * PERIOD)
        if previous and (now - previous["last_success"] > 2 * PERIOD
                         or now < previous["last_success"]):
            reasons.append("G5_gap")
            critical = True
        rate = None
        if continuous:
            delta = metrics["events"]["high"] - previous["metrics"]["events"]["high"]
            if delta >= 0:
                rate = delta * PERIOD / elapsed
            if any(metrics["events"][name] > previous["metrics"]["events"][name]
                   for name in ("oom", "oom_kill")):
                reasons.append("oom_event")
                critical = True
        metrics["high_per_minute"] = rate
        # WHY: 2.61M unnoticed high crossings require rates, not lifetime alarms.
        durations = {}
        for label, threshold, seconds in (("G1_warn", 60, 300), ("G1_crit", 600, 120)):
            durations[label] = (min(seconds, previous.get("durations", {}).get(label, 0) + elapsed)
                                if rate is not None and rate > threshold else 0)
            if durations[label] >= seconds:
                reasons.append(label)
                critical |= label == "G1_crit"
        ratio = metrics["rss_bytes"] / metrics["high_bytes"] if metrics["high_bytes"] else None
        metrics["rss_high_ratio"] = ratio
        if ratio is None:
            reasons.append("G2_high_unbounded")
        elif ratio > 0.90:
            reasons.append("G2_crit")
            critical = True
        elif ratio > 0.75:
            reasons.append("G2_warn")
        warmup = previous.get("warmup", monotonic) if continuous else monotonic
        trend = previous.get("trend") if continuous else None
        if monotonic - warmup >= 600:
            if trend is None:
                trend = [monotonic, metrics["rss_bytes"]]
            elif monotonic - trend[0] >= 3600:
                growth = (metrics["rss_bytes"] - trend[1]) * 3600 / (monotonic - trend[0])
                metrics["rss_growth_bytes_per_hour"] = growth
                if growth > 128 * 1024 * 1024:
                    reasons.append("G2_growth")
                trend = [monotonic, metrics["rss_bytes"]]
        shed = critical or bool(previous and previous["shed"])
        healthy_since = None
        if not reasons and (rate is None or rate <= 60):
            healthy_since = previous.get("healthy_since") if continuous else None
            if healthy_since is None:
                healthy_since = monotonic
            if monotonic - healthy_since >= 600:
                shed = False
        return {"version": 1, "timestamp": now, "last_success": now,
                "monotonic": monotonic, "severity": "CRIT" if critical else "WARN" if reasons else "OK",
                "shed": shed, "reasons": reasons, "metrics": metrics,
                "durations": durations, "healthy_since": healthy_since,
                "warmup": warmup, "trend": trend}


def start_memory_monitor(monitor, stop_event):
    """Parent calls once after fork; shutdown via stop_event.set(), thread.join()."""
    def run():
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                monitor.sample()
            except Exception:
                # Never expose exception strings (paths or input) in alarm logs.
                LOGGER.error("webui_memory CRIT G5_publish_error")
                return
            stop_event.wait(max(0, PERIOD - (time.monotonic() - started)))

    thread = threading.Thread(target=run, name="webui-memory-monitor", daemon=True)
    thread.start()
    return thread


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cgroup", type=Path)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.check:
        health = read_health(args.state)
        _append(args.log, {"timestamp": time.time(), "kind": "G5_check", **health})
        if health["severity"] == "CRIT":
            LOGGER.error("webui_memory CRIT G5_check")
        return 2 if health["severity"] == "CRIT" else 0
    if args.cgroup is None or args.pid is None:
        parser.error("sampling requires the target --cgroup and --pid")
    result = MemoryMonitor(args.cgroup, pid=args.pid, log_path=args.log,
                           state_path=args.state, session_dir=args.session_dir).sample()
    return 2 if (result["severity"] == "CRIT"
                 or result.get("g4", {}).get("severity") == "CRIT") else 0


if __name__ == "__main__":
    raise SystemExit(main())
