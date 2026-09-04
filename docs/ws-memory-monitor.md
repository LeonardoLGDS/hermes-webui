# Bounded WebUI memory telemetry

Status: implementation candidate awaiting independent refs review. No runtime
integration or deployment is performed by these files. Applicable procedures:
PROC_ESCALATE and PROC_ECDP. The converged plan already approves this telemetry
design; no external outage hypothesis, cron, restart, limit raise, or independent
watchdog is introduced into the running system.

## Why

The September 1–2, 2026 OOM evidence and the plan's 2.61 million unnoticed
`memory.events:high` crossings show why both pressure and sampler freshness
matter. Lifetime counters alone are not alarms. This monitor reads only bounded
kernel telemetry; it never reads session bodies, environment, credentials,
command lines, agent trees, or application routes.

## Parent integration (preferred)

From the source root, import `MemoryMonitor`, `start_memory_monitor`, and
`read_health` from `tools.ws_memory_monitor`. After the serving process has forked,
start exactly one sampler for the target cgroup:

```python
import threading
from tools.ws_memory_monitor import MemoryMonitor, read_health, start_memory_monitor

memory_stop = threading.Event()
memory_monitor = MemoryMonitor(cgroup_path=resolved_webui_cgroup, session_dir=SESSION_DIR)
memory_thread = start_memory_monitor(memory_monitor, memory_stop)
```

`resolved_webui_cgroup` must be the actual cgroup-v2 directory for this serving
process, not its parent's or the sampler service's cgroup. `pid` defaults to the
calling process; an external caller must supply the target PID explicitly.
Optional `proc_root`, `state_path`, and `log_path` arguments support isolated
fixtures and operator-selected storage. Defaults are `/home/ops/webui-mem.log`
and `/home/ops/webui-mem-state.json`; their directories must already exist and
be writable by the server user. No imports start threads or write files.

`SESSION_DIR` is supplied by the parent. `session_dir=None` (the default) disables
G4 completely: no default session paths, discovery, or queue writes. The optional
CLI equivalent is `--session-dir PATH`.

At each admission decision, the parent calls
`read_health(memory_monitor.state_path)` and treats `health["shed"]` as the
authoritative fail-closed flag. Missing, malformed, future-dated, or older-than-
120-second successful telemetry returns CRIT and `shed=True`. The parent owns
wsbound, route status codes, budget/cache reduction, and sidecar/full-view
enforcement. This module does not modify those components or invoke callbacks.
Do not cache the flag without rechecking heartbeat freshness.

On normal shutdown call `memory_stop.set()` and `memory_thread.join(timeout=5)`;
check `memory_thread.is_alive()` rather than claiming a join always succeeded.
The daemon thread samples immediately, then at approximately one-minute cadence;
there is no busy polling, catch-up sample backlog, or publication retry loop.
I/O publication failure logs a sanitized CRIT and terminates the thread. Freshness
then expires, so the parent must continue checking it. Rollback means removing
the parent's startup hook and explicitly adjudicating admission behavior; do not
delete the retained telemetry evidence.

## Metrics, ownership, and thresholds

- Reads `memory.current`, `memory.high` (including `max`), allowlisted memory
  event counters, PSI some/full avg10/avg60/avg300/total, and process VmRSS/VmHWM.
  RSS/High uses process VmRSS, not cgroup current; both are recorded separately.
- G1: high delta divided by elapsed monotonic time, expressed per minute.
  Strictly >60/min for 300 seconds warns; >600/min for 120 seconds is critical.
  Counter rollback, process/cgroup identity change, or sampling gaps reset rate
  accumulation. Startup establishes a baseline, never alarms on a lifetime count.
- G2: RSS/High >0.75 warns, >0.90 immediately sheds. Unlimited High warns because
  ratio protection is unavailable. After ten minutes' warmup, fixed hourly
  baseline comparisons warn on >128 MiB/hour growth. This is an hourly trend,
  not an instantaneous leak detector. PSI is recorded, without invented thresholds.
- New OOM/oom_kill counter increments between continuous samples are critical;
  they never cause an automatic restart. Lifetime OOM values are not new incidents.
- Every critical observation latches shed. Recovery requires ten continuous
  minutes without warning/critical conditions and high rate <=60/min. Identity
  changes and gaps reset recovery. Sample errors preserve the previous successful
  heartbeat and shed; repaired input does not immediately clear that latch.
- G5: successful state age >2 periods is CRIT. A returning sampler reports a
  stale gap; an admission reader fails closed even if the sampler is dead. This
  is **not independent G5 paging**: a dead WebUI cannot run its own reader.
- T3 quarantine, repeated-T2 accounting, promotion/migration blocks, daily
  synthetic alerts, and downstream paging remain parent/operator work. Critical
  memory pressure continues to request shed; no quarantine actuator is claimed.

Each telemetry read is capped at 64 KiB; only one previous observation, two sustained
durations, one recovery start, and one hourly baseline are retained. Kernel
identities prevent joining rates across PID/cgroup replacement. Monotonic time
handles interval timing; wall time supports external freshness checks and clock
rollback fails closed. One process/thread must own each state/log pair; never
run the parent sampler and timer sampler together. No interprocess writer
arbitration is provided.

Each sample appends one allowlisted JSON receipt, flushes and fsyncs the log,
then writes/fsyncs a fixed `.tmp` state file, atomically replaces state, and
fsyncs its directory. The state file itself is the heartbeat, avoiding a
separate heartbeat/shed publication race. Failure leaves readers with either
the previous state or the complete new state, never partial JSON. A crash after
log append but before state publication may leave an extra log receipt; it
cannot certify fresh state without a durable sample. Files are created mode
0600 with no-follow opens; use trusted storage directories, not shared writable
directories. Existing operator-managed file modes are not changed.

Memory/state size is bounded; the durable append-only log intentionally grows
on disk (one receipt per minute, plus explicit checks). Retention/archival and
disk-space monitoring are operator responsibilities. No rotation, truncation of
the log, deletion, uploading, or log-content ingestion is implemented. Alarms
also go to Python logging using fixed reason codes, never raw input/errors.

## Optional G4 store scan

With `MemoryMonitor(cgroup, session_dir=SESSION_DIR)`, the initial sample and each
24-hour interval attempt a streaming `os.scandir` scan of direct `*.json` regular
files, excluding `_index.json`. No recursion, archive-module calls, session-body
reads, or symlink following occur. Subdirectories and non-regular files are
ignored. The total is the sum of matching files' stat sizes, not allocated disk
blocks. Directory changes during a scan are not a transactional snapshot; a
stat failure invalidates the scan instead of reporting a partial total as fresh.

Strictly >8 MiB files enter an advisory queue and produce WARN. Strictly >32 MiB
individual files or >3,200,000,000 bytes total (decimal 3.2 GB) produce CRIT.
`webui-compaction-queue.json` is atomically published next to `state_path`; it
contains a timestamp, at most 4096 `{name, size_bytes}` entries, and an omitted
count. Every matching regular file contributes to the total even after the queue
fills; overflow produces `G4_queue_overflow`. Queue selection uses directory
enumeration order. Memory is bounded by that cap, but scan time scales with the
number of direct directory entries. Only the separate queue contains basenames;
logs/state contain aggregate counts and a digest identifying the configured
directory, never filenames or session content. Each directory must have its own
state directory/queue owner. Keep output state outside the session directory.

The summary and last-attempt/last-success times persist in monitor state, so
one-shot invocations do not scan every minute. Changing the configured directory
forces a new scan. Failed attempts log `G4_scan_failed` CRIT, retain the prior
successful aggregate/queue, and do not advance G4 success freshness. Publication
failure also prevents advancing success (a failure after atomic replacement can
leave a complete newer queue, without a certified successful summary). There is
no tight retry: the next scan attempt is 24 hours later. A last success older than
26 hours logs `G4_stale` CRIT; if no scan has succeeded, age is measured from first
attempt. A returning scanner reports a stale gap even if that scan succeeds.

`sample()["g4"]` exposes G4 severity, reasons, counts, and freshness independently
of memory health. Active G4 alerts append explicit `kind="G4"` WARN/CRIT receipts
and sanitized logging alarms; memory samples also carry the bounded summary.
G4 alone does not alter memory shed state, split/archive/delete files, call an
actuator, or restart anything. CLI sampling exits 2 on G4 CRIT. Independent
notification delivery remains deferred with the existing G5 work.

## Deferred timer templates and independent G5

`tools/ws-memory-*.service.in` and `.timer.in` are inert templates only. No units
are installed, enabled, started, or restarted, and no cron is used. A separately
authorized deployment must replace every `@TOKEN@`, validate target identity and
permissions, and connect `OnFailure` to the real notification channel. The
provided alarm template is only a journal sink, **not paging delivery**.

The sample template resolves the target unit's current MainPID each invocation;
it must not sample its own service PID/cgroup. CLI sampling persists bounded
sustained-rate/recovery state across invocations. Exit 2 indicates critical.
The independent heartbeat template runs `--check` without a target PID/cgroup,
appends a G5 receipt, and exits 2 on critical health. Its state reader never
refreshes the heartbeat. A separate heartbeat timer is required whether sampling
uses the parent or a timer. Use exactly one sampler mode.

Independent G5 remains **DEFERRED / UNVERIFIED** until separately deployed and
tested: observe at least three successful minute receipts, stop only the
sampler in an authorized test, prove independent CRIT within five minutes,
restore sampling, and verify latch recovery. No such live test occurred here.
In-process tests cannot prove independent liveness or notification delivery.

## Test receipts and review handoff

Run from the source root:

```sh
PYTHONDONTWRITEBYTECODE=1 timeout 60 ./scripts/test.sh tests/test_ws_memory_monitor.py
```

Fixtures use temporary cgroup/proc/log/state paths only; no real telemetry files,
services, sessions, or limits are touched. Captured runner outputs are
`tools/ws-memory-monitor-test-before.txt` (expected missing-module failure before
implementation) and `tools/ws-memory-monitor-test-after.txt` (initial 18-test run).
The G4 extension's preimplementation failure receipt is
`tools/ws-memory-monitor-g4-test-before.txt`; its successful runner receipt is
`tools/ws-memory-monitor-g4-test-after.txt`. G4 fixtures include stat-only access,
direct-only filtering, strict size boundaries, 24-hour persistence, failed/stale
scans, queue publication failure, and 4097 sparse files proving the 4096-entry
cap does not truncate the aggregate total.
Independent refs must review rate duration semantics, stale-state admission,
atomic publication/failure ordering, lifecycle ownership, and deferred G5 before
the parent declares the work complete. Local fixture success is not live soak,
UI verification, independent refs approval, or deployment evidence.
