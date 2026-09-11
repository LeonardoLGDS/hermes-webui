"""Bound derived HTTP working sets; never cache mutable session graphs."""

import hashlib
import os
import json
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from contextvars import ContextVar


class MemoryBudgetExceeded(BaseException):
    # WHY: legacy readers catch Exception and return empty history. Budget
    # refusal must reach the HTTP boundary, never masquerade as an empty session.
    pass


class SnapshotPending(Exception):
    pass


class WindowTooLarge(Exception):
    pass


DERIVED_READ = ContextVar("webui_derived_read", default=False)
RESPONSE_CAPTURE = ContextVar("webui_response_capture", default=None)
READ_BUDGET = ContextVar("webui_read_budget", default=None)
METADATA_WORKER = threading.BoundedSemaphore(1)


class ReadBudget:
    def __init__(self, capacity):
        self.remaining = capacity

    def consume(self, amount):
        if amount > self.remaining:
            raise MemoryBudgetExceeded()
        self.remaining -= amount


def read_source_text(path):
    budget = READ_BUDGET.get()
    if budget is None:
        return path.read_text(encoding="utf-8")
    # WHY: a stat BEFORE queuing can race an atomic replacement (review P1).
    # Bind the bound to the opened descriptor and reject growth before parsing.
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if before.st_size > budget.remaining:
            raise MemoryBudgetExceeded()
        raw = source.read(budget.remaining + 1)
        after = os.fstat(source.fileno())
        budget.consume(len(raw))
        descriptor_identity = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        if descriptor_identity != (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        ):
            raise MemoryBudgetExceeded()
        # An atomic replacement can race the path lookup/open or happen after a
        # stable descriptor read. Comparing the live name to the descriptor in
        # both directions prevents stamping content from one inode with another
        # inode's source identity.
        try:
            current = path.stat()
        except OSError:
            raise MemoryBudgetExceeded() from None
        if descriptor_identity != (
            current.st_dev, current.st_ino, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns,
        ):
            raise MemoryBudgetExceeded()
    return raw.decode("utf-8")


def reserve_sql_rows(connection, columns, where, parameters):
    budget = READ_BUDGET.get()
    if budget is None:
        return
    # WHY: missing-sidecar/foreign history has no useful file size. Count bytes
    # inside the same read transaction BEFORE fetchall, including row overhead.
    terms = [f'COALESCE(length(CAST("{column}" AS BLOB)),0)' for column in columns]
    size = connection.execute(
        f"SELECT COALESCE(SUM({' + '.join(terms)} + 256),0) FROM messages WHERE {where}",
        parameters,
    ).fetchone()[0]
    budget.consume(size)


def bounded_window(messages, offset, budget=1572864):
    """Keep a contiguous tail so existing msg_before cursors remain valid."""
    used = 2
    start = len(messages)
    for row in reversed(messages):
        # WHY: 200 rows can still contain multi-MB tool results (09-02 OOM).
        # iterencode counts without allocating another full serialized row.
        cost = 1
        for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(row):
            cost += len(chunk.encode("utf-8"))
            if used + cost > budget:
                break
        if used + cost > budget:
            if start == len(messages):
                raise WindowTooLarge()
            break
        used += cost
        start -= 1
    return messages[start:], offset + start


class ByteLRU:
    def __init__(self, capacity):
        self.capacity = capacity
        self.used = 0
        self.generation = 0
        self.entries = OrderedDict()
        self.lock = threading.RLock()

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            blob, deadline, charge = entry
            if time.monotonic() >= deadline:
                self.used -= charge
                del self.entries[key]
                return None
            self.entries.move_to_end(key)
            return blob

    def put(self, key, blob, ttl=5):
        if not isinstance(blob, bytes):
            raise TypeError("response cache accepts only serialized bytes")
        # WHY: count keys/entry overhead too, not only payloads. The 09-01/02
        # OOMs proved that entry-count limits do not bound a growing store.
        charge = len(blob) + len(repr(key).encode()) + 512
        with self.lock:
            old = self.entries.pop(key, None)
            if old:
                self.used -= old[2]
            if charge > self.capacity // 4:
                return
            while self.used + charge > self.capacity and self.entries:
                self.used -= self.entries.popitem(last=False)[1][2]
            self.entries[key] = (blob, time.monotonic() + ttl, charge)
            self.used += charge

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.used = 0
            self.generation += 1

    def resize(self, capacity):
        with self.lock:
            self.capacity = capacity
            while self.used > capacity and self.entries:
                self.used -= self.entries.popitem(last=False)[1][2]

    def snapshot(self):
        with self.lock:
            return {"bytes": self.used, "cap": self.capacity, "entries": len(self.entries)}


class AdmissionGate:
    def __init__(self, budget, slots=2, queue_limit=64):
        self.budget = budget
        self.slots = slots
        self.queue_limit = queue_limit
        self.used = 0
        self.active = 0
        self.waiters = deque()
        self.condition = threading.Condition()

    @contextmanager
    def admit(self, cost, timeout=2):
        token = object()
        deadline = time.monotonic() + timeout
        with self.condition:
            if cost > self.budget or cost < 0 or len(self.waiters) >= self.queue_limit:
                raise MemoryBudgetExceeded()
            self.waiters.append(token)
            try:
                # WHY: FIFO stops a stream of tiny polling requests starving a
                # human's larger compile (09-03 sustained throttle/retry loop).
                while (self.waiters[0] is not token or self.active >= self.slots
                       or self.used + cost > self.budget):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MemoryBudgetExceeded()
                    self.condition.wait(remaining)
                self.waiters.popleft()
                self.used += cost
                self.active += 1
            except BaseException:
                self.waiters.remove(token)
                self.condition.notify_all()
                raise
            self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.used -= cost
                self.active -= 1
                self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"bytes": self.used, "budget": self.budget, "active": self.active,
                    "slots": self.slots, "queued": len(self.waiters)}


RESPONSES = ByteLRU(int(os.getenv("HERMES_WEBUI_CACHE_BYTES", "268435456")))
POLL_COOLDOWNS = ByteLRU(1024 * 1024)
COMPILES = AdmissionGate(int(os.getenv("HERMES_WEBUI_INFLIGHT_BYTES", "536870912")))
SESSION_LOAD_JOIN_TIMEOUT = 20.0
SESSION_LOAD_FLIGHT_LIMIT = 256
SESSION_LOAD_PENDING_BODY = b'{"session_load":"in_progress"}'
SESSION_LOAD_FLIGHTS = {}
SESSION_LOAD_FLIGHT_LOCK = threading.Lock()
NORMAL_CACHE_BYTES = RESPONSES.capacity
NORMAL_INFLIGHT_BYTES = COMPILES.budget
PRESSURE_STATE = None
REDACT_RULESET_VERSION = 1
VIEW_SCHEMA_VERSION = 1


class SessionLoadFlight:
    def __init__(self):
        self.event = threading.Event()
        self.result = None

    def record(self, body, status):
        self.result = (body, status)


def claim_session_load(key):
    """Return ``(flight, is_leader)`` for an exact response-key session read.

    ``None, False`` means the process-wide unique-key cap is full. That is an
    abuse/resource boundary, deliberately distinct from an identical-reader join.
    """
    with SESSION_LOAD_FLIGHT_LOCK:
        flight = SESSION_LOAD_FLIGHTS.get(key)
        if flight is not None:
            return flight, False
        if len(SESSION_LOAD_FLIGHTS) >= SESSION_LOAD_FLIGHT_LIMIT:
            return None, False
        flight = SessionLoadFlight()
        # WHY: exact keys make this a response replay, not generic session
        # coalescing; differing query/profile/source stamps still get isolation.
        SESSION_LOAD_FLIGHTS[key] = flight
        return flight, True


def join_session_load(flight):
    """Bounded-wait for the leader, then expose pending rather than 429."""
    completed = flight.event.wait(SESSION_LOAD_JOIN_TIMEOUT)
    with SESSION_LOAD_FLIGHT_LOCK:
        if completed and flight.result is not None:
            return flight.result, True
    return (SESSION_LOAD_PENDING_BODY, 202), False


def finish_session_load(key, flight):
    with SESSION_LOAD_FLIGHT_LOCK:
        if flight.result is None:
            # Unexpected/no-response leader paths must never strand followers
            # until socket timeout; pending is retryable and client-safe.
            flight.result = (SESSION_LOAD_PENDING_BODY, 202)
        flight.event.set()
        if SESSION_LOAD_FLIGHTS.get(key) is flight:
            del SESSION_LOAD_FLIGHTS[key]


def admit_poll(key, visibility):
    # WHY: `visible` is the active foreground reader. It must not consume the
    # automatic-poll floor: the desktop app probes that session every 650ms–2s,
    # so throttling it turns a first click into "Failed to load session".
    if not visibility or visibility == "visible":
        return 0
    delay = 60 if visibility == "hidden" else 5
    # WHY: the 650ms sidecar loop amplified 09-03 throttling. Only marked
    # automatic polls are limited; direct human reads still queue normally.
    with POLL_COOLDOWNS.lock:
        entry = POLL_COOLDOWNS.entries.get(key)
        if entry and entry[1] > time.monotonic():
            return max(1, int(entry[1] - time.monotonic()) + 1)
        POLL_COOLDOWNS.put(key, b"poll", ttl=delay)
    return 0


def pressure_health():
    if PRESSURE_STATE is None:
        return {"shed": False, "severity": "not_configured"}
    # WHY (2026-09-04 17:39 /api/sessions 500): HERMES_WEBUI_AGENT_DIR is prepended to
    # sys.path, so bare "from tools.ws_memory_monitor" resolved to the AGENT tree's tools
    # package (regular, has __init__) and shadowed this WebUI module — same bug family as
    # the server.py crash (16:54). Load by absolute file path: unambiguous identity.
    from pathlib import Path as _Path
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "tools.ws_memory_monitor",
        str(_Path(__file__).resolve().parent.parent / "tools" / "ws_memory_monitor.py"))
    _wsm = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_wsm)
    read_health = _wsm.read_health
    health = read_health(PRESSURE_STATE)
    # WHY: 2.61M high crossings went unnoticed. On sustained pressure or stale
    # telemetry reduce internal work, never raise ceilings or restart sessions.
    divisor = 2 if health["shed"] else 1
    RESPONSES.resize(NORMAL_CACHE_BYTES // divisor)
    with COMPILES.condition:
        COMPILES.budget = NORMAL_INFLIGHT_BYTES // divisor
        COMPILES.condition.notify_all()
    return health


def path_stamp(path):
    try:
        info = path.stat()
        return (info.st_ino, info.st_mtime_ns, info.st_size)
    except FileNotFoundError:
        return (0, 0, 0)


def etag(blob):
    return '"' + hashlib.sha256(blob).hexdigest() + '"'
