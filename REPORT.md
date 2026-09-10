# r106 resident transcript loading

## Scope and result

The defect was an admission-policy cliff, not an unreadable transcript. A
resident session was charged as though `/api/session?messages=1` had to compile
the complete sidecar, even when its authoritative graph was already in memory.
The final patch keeps `AdmissionGate` and its 512 MiB default, but changes what
a resident read and legacy fallback reserve.

No production service was restarted and no request was sent to port 8787.

## Before and after probes

### Live incident (from the incident capture, 2026-09-09)

```text
GET /api/session?session_id=15371642067b&messages=1&resolve_model=0&msg_limit=30
-> 429 {"error":"memory_budget"} in 10 ms

GET /api/session?session_id=15371642067b&messages=1&resolve_model=0&msg_limit=5
-> 429 {"error":"memory_budget"} in 10 ms

GET /api/session?session_id=15371642067b&messages=1&resolve_model=0
-> 429 {"error":"memory_budget"} in 10 ms

GET /api/session?session_id=15371642067b&messages=0
-> 200

GET /api/session?session_id=<4KB-session>&messages=1
-> 200
```

The 22.6 MB sidecar was charged approximately 542 MB by
`max(8 MiB, file_size * 2) * 12`, exceeding the 512 MB default.

### Synthetic baseline at f1c07de

The new 23-test file was copied into an isolated detached worktree at that
commit and run there:

```text
19 failed, 4 passed in 3.81s
```

Representative failures:

```text
PROBE large&msg_limit=30: HTTP 429, messages=0, error=memory_budget
CONCURRENT resident=False, clean=False, default=536870912: [429, 429]
CONCURRENT resident=False, clean=True, default=536870912: [429, 429]
CONCURRENT resident=True, clean=True, default=536870912: [429, 429]
```

### Patched probes

The focused test run prints the request result, elapsed time, returned row
count, and error. Each large fixture is at least 24 MB:

```text
PROBE large&msg_limit=30: HTTP 200, 219.3 ms, messages=30, error=None
PROBE large&msg_limit=30: HTTP 200, 198.4 ms, messages=30, error=None
PROBE large&msg_limit=30: HTTP 200, 35.2 ms, messages=30, error=None
PROBE resident&msg_limit=30: HTTP 200, 33.9 ms, messages=30, error=None
PROBE resident&msg_limit=5: HTTP 200, 5.6 ms, messages=5, error=None
PROBE resident: HTTP 200, 70.9 ms, messages=64, error=None
PROBE oversized&msg_limit=30: HTTP 413, 165.1 ms, messages=0, error=message_window_too_large
PROBE oversized&msg_limit=30: HTTP 413, 3.4 ms, messages=0, error=message_window_too_large
PROBE legacy: HTTP 200, 106.3 ms, messages=63, error=None
PROBE other-profile&msg_limit=30: HTTP 409, 0.6 ms, messages=0, error=Session belongs to a different profile
```

With the default 536,870,912-byte gate, two large loads overlapped at two active
compiles in all required states and both succeeded:

```text
CONCURRENT resident=False, clean=False, default=536870912: [200, 200]
CONCURRENT resident=False, clean=True, default=536870912: [200, 200]
CONCURRENT resident=True, clean=True, default=536870912: [200, 200]
```

The test also asserts that `COMPILES.snapshot()` returns zero active, queued,
and reserved bytes after the loads.

## Exact hunks and rationale

### `api/routes.py`

- `@@ -70,6 +70,10 @@`: adds `_RESIDENT_SESSION_READ` and the fixed 8 KiB
  legacy JSON preflight allowance.
  **Why:** a pinned graph must remain the display source across the admission
  and response phases, while a sparse-zero sidecar can still be rejected before
  its whole physical extent is read.

- `@@ -13572,17 +13576,57 @@`: changes
  `_bounded_writer_authority_exists` to consult only
  `config.session_writeback_owner`; adds `_resident_session_for_read`; adds
  `_legacy_sidecar_prefix_has_raw_nul`.
  **Why:** registry membership alone is not writeback ownership. A metadata-only
  stub therefore remains a bounded disk read. A full resident graph is pinned
  under `LOCK` and selected instead. Raw NUL is invalid JSON, so a stable 8 KiB
  prefix containing NUL proves the legacy file cannot parse without using file
  size as a valid-transcript admission charge.

- `@@ -13626,6 +13670,13 @@`: pins a resident graph before constructing or
  consuming a disk-stamped response-cache key.
  **Why:** inode/mtime/size cannot identify unsaved rows in the graph.

- `@@ -13634,7 +13685,7 @@`: suppresses the response-cache lookup while a
  resident graph is selected.
  **Why:** otherwise an old disk projection could hide an unsaved live answer.

- `@@ -13699,7 +13750,12 @@`: excludes resident reads from disk-keyed session
  load single-flight ownership.
  **Why:** joining a disk load could replay stale saved state.

- `@@ -13749,22 +13805,15 @@`: skips the bounded disk proof when a full
  resident graph was already pinned, and rechecks both writeback ownership and
  residency before and after the proof. Resident publication during the proof
  discards the disk body and falls through to the graph.
  **Why:** the resident graph is authoritative; a proof based on unchanged disk
  bytes is not allowed to overwrite it.

- `@@ -13778,13 +13827,15 @@`: bounded cache capture also requires no resident
  graph and no writeback owner at response time.
  **Why:** prevents a stale bounded projection from entering the disk cache.

- `@@ -13805,22 +13856,38 @@`: replaces whole-sidecar admission with
  `(8 MiB + normalized window rows * 4 KiB) * 12 + 1.5 MiB`, keeps the existing
  physical `ReadBudget`, and adds the fixed structural NUL preflight.
  **Why:** the fallback still parses a complete legacy file, but admission now
  describes the requested display window plus a fixed working allowance. For
  `msg_limit=30`, the reservation is 103,710,720 bytes. Two such reads total
  207,421,440 bytes under the unchanged 536,870,912-byte gate. Genuine output
  overflow continues through `WindowTooLarge` to HTTP 413.

- `@@ -13828,6 +13895,8 @@`: re-pins residency after waiting for admission
  and before choosing a response source; cache capture refuses resident graphs.
  **Why:** closes the queue/proof-to-use gap without re-resolving the path.

- `@@ -13837,9 +13906,11 @@`: publishes the pinned graph through
  `_RESIDENT_SESSION_READ` and resets it in `finally` beside every other
  response context.
  **Why:** success, 413, disconnect, cancellation, and ordinary exceptions all
  restore the request-local state and leave `AdmissionGate` via its `with`
  block.

- `@@ -14511,6 +14582,13 @@`: rechecks residency immediately before body
  selection.
  **Why:** a graph published after the bounded proof must win over stale disk
  data.

- `@@ -14519,7 +14597,11 @@`: chooses the pinned graph before `get_session`,
  bounded reader, or freshness/recovery logic.
  **Why:** the resident transcript is already authoritative and must not read
  the sidecar body.

- `@@ -14551,7 +14633,9 @@`: suppresses `_clear_stale_stream_state` for a
  display-only resident read.
  **Why:** serving a transcript cannot mutate stream state or cause repair/save
  I/O.

- `@@ -14571,9 +14655,9 @@`: skips messaging/state.db transcript
  rehydration for resident reads.
  **Why:** disk rows must not replace the graph's unsaved transcript.

- `@@ -14639,7 +14723,7 @@`: preserves the existing metadata-only summary
  branch only when messages were not requested.
  **Why:** resident and metadata-only requests remain distinct state paths.

- `@@ -14665,7 +14749,11 @@`: copies `list(s.messages)` into the response
  builder.
  **Why:** supplies the graph-owned transcript while retaining the one shared
  pagination, redaction, and `WindowTooLarge` implementation.

### `tests/test_r106_resident.py`

- `@@ -0,0 +1,328 @@`: adds isolated synthetic HTTP probes. They cover the
  required 24 MB load, resident no-sidecar-body assertion, 413 overflow,
  concurrent default-gate loads, clean and metadata-stub bounded reads, cache
  invalidation, residency races, writeback ownership, 0/1 rows, profile
  isolation, and context/gate cleanup after disconnect.
  **Why:** every assertion observes the dispatched HTTP result or a monkeypatch
  at an I/O boundary rather than merely checking source text.

### `docs/working-set-bounds.md`

- `@@ -17,10 +17,14 @@`: updates the contract paragraph to distinguish gate
  contention, fixed bounded reads, resident graph reads, and window-based
  legacy reservations.
  **Why:** the old documented whole-sidecar formula is no longer the behavior.

## State ownership and invariants

- The mutable `SESSIONS` graph owns unsaved transcript state. A read pins the
  exact object under `LOCK`, then uses that handle outside the lock; the read
  never replaces, discards, saves, or enqueues it for repair.
- The bounded tail result remains response-only and does not populate the
  mutable writer cache.
- `RESPONSES` remains disk-keyed and is bypassed for resident graphs. Resident
  publication also prevents bounded and legacy captures.
- Profile visibility is checked after selecting either resident or disk state.
- `COMPILES`, `READ_BUDGET`, `DERIVED_READ`, `RESPONSE_CAPTURE`,
  `_BOUNDED_SESSION_READ`, and `_RESIDENT_SESSION_READ` have scope-local reset
  paths. The disconnect test exercises the exception path and asserts zero gate
  residue.
- `AdmissionGate` defaults and its implementation are unchanged. Physical file
  read budgets and SQLite byte accounting are also unchanged.
- `list(s.messages)` protects against replacement of the message container, but
  it is intentionally shallow. Concurrent in-place mutation of one nested row is
  not serialized by a display read; that is the pre-existing graph ownership
  model and remains outside this patch.

## Verification

All commands ran in this isolated worktree:

```text
./scripts/test.sh -s -q tests/test_r106_resident.py
23 passed in 3.89s

./scripts/test.sh -q \
  tests/test_r106_resident.py \
  tests/test_reasoning_tail_display.py \
  tests/test_session_message_window_renderable_tail.py \
  tests/test_display_r2_regressions.py \
  tests/test_full_session_resolve_gate.py \
  tests/test_session_sidecar_repair.py \
  tests/test_session_tail_payload.py \
  tests/test_session_msg_limit_ceiling.py \
  tests/test_display_merge_cache.py \
  tests/test_display_merge_cache_shortcut.py \
  tests/test_session_switch_performance.py \
  tests/test_wsbound.py
208 passed, 1 skipped in 10.09s
```

`git diff --check` passed. `api/wsbound.py`, `api/models.py`, `static/*`, and
`tests/test_wsbound.py` have no diff from the baseline.
