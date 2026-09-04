# ROUND: wui-routes-failsafe

STATUS|done|routes/tests committed; promotion and live soak intentionally not performed

## Outcome

Fixed both incident paths in `api/routes.py`:

- The shared GET wrapper now defines a safe `flight_json` responder before the `/api/session`-only flight branch. It conditionally records only when a session-load flight exists, so `/api/sessions` can use the same boundary handlers without taking the `UnboundLocalError` detour through the app-wide 500 branch.
- Ordinary bounded session-list builder failures are logged with tracebacks and converted to a valid HTTP 200 degraded response. The response retains whatever safe rows are available (compact index rows on a cold build), includes `degraded: true` and the sanitized exception class in `error`, and is excluded from the fresh 5-second response cache. `MemoryBudgetExceeded` remains a deliberate pressure boundary rather than being disguised as an ordinary degraded list.
- List sorting, runtime overlay, row projection, and pre-header serialization have row/response-level guards so one malformed row cannot fail the entire sidebar response.

No files outside `api/routes.py`, `tests/test_session_list_failsafe.py`, and this report were changed.

## Real-environment reproduction

The checkout base was `c23c47794eb53f2f5f1076faf6a0ddac28b7eefb` (with incident revision `496e4fc` as its ancestor). A detached clean worktree at `c23c477` was used for fail-before evidence. The real environment was loaded from the service environment file without displaying it, and the app venv ran the actual `api.routes.handle_get` path with bytecode disabled. The cold bounded-list boundary was forced in-process only (snapshot miss plus denied rebuild ownership); no live HTTP request or service mutation was made.

Baseline result reproduced the supplied 19:20 chain exactly:

```text
_handle_get_impl -> _get_bounded_session_list_payload -> SnapshotPending
handle_get -> flight_json({"error": "metadata_refresh_pending"}, status=503)
UnboundLocalError: cannot access local variable 'flight_json'
```

After the fix, the same real-handler call returned:

```text
status=200
sessions=61
degraded=true
degraded_reason="metadata_refresh_pending"
error="SnapshotPending"
```

The traceback was retained in the log, and the response came from the real compact session index.

### Real edge-record finding

No session record was proven to cause the nine incident 500s. The deterministic cold `SnapshotPending` requires no session ID and fully explains the `UnboundLocalError` chain. A separate read-only scan of the current real session directory found the JSON records parseable and no missing indexed sidecar; it identified two valid but oversized records (`6aa24bce183b`, 14,572,300 bytes, and `ccc050b98bfe`, 14,648,805 bytes) that exceed the metadata worker budget and are skipped/logged. Those are separate resource-boundary inputs, not evidence of the incident's list-builder exception. No transcript content is reproduced here.

## Verification

Focused regression and join tests:

```text
.venv/bin/python -m pytest tests/test_session_list_failsafe.py tests/test_wsbound.py -q
20 passed in 2.96s
```

Exact requested broad subset on the fixed tree:

```text
.venv/bin/python -m pytest tests/ -k "session or wsbound or routes or list" -q
43 failed, 3291 passed, 30 skipped, 11878 deselected in 145.07s
```

The same exact broad subset was run in the clean `c23c477` baseline worktree:

```text
54 failed, 3275 passed, 30 skipped, 11878 deselected in 150.01s
```

Every failing test on the fixed tree also failed at baseline (zero new failures); the patch fixed 11 of the baseline failures and added five selected regression tests. The remaining failures are pre-existing environment/source expectations unrelated to this round (for example static-route assertions, profile-worker routing, and legacy session-index behavior). The full suite was not run; the bounded subset above is what was executed.

Static checks:

```text
git diff --check
.venv/bin/python -m py_compile api/routes.py tests/test_session_list_failsafe.py
```

Both exited successfully.

## Operational state

- No restart, promotion, rollback, or live HTTP mutation was performed.
- No gateway/service owner was changed.
- Deployment, runtime source verification, and live soak remain with the operator.
