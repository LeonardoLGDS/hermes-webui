# Background completion card contract

`api/background_process.py::_build_payload` retains `session_id`, `task_id`,
`completed_at`, `event_id`, and optional `summary`. Both `bg_task_complete` and
the legacy `process_complete` alias carry the same additive metadata:

- `task_type`: `delegation` for async delegations, otherwise `process`.
- Optional `title`: first line of the delegation goal or shell command, capped
  at 200 characters. Omitted when unavailable so known client titles survive.
- `status`: original agent outcome for delegations; delivery acceptance does
  not imply task success. No synthetic process exit code is invented.
- `exit_code`: process exit code, or null when unavailable, for shell jobs.

The browser reducer uses a numeric exit code when available: zero means `done`,
nonzero means `failed`. Without an exit code it accepts terminal success statuses
`done`, `completed`, and `success`; all other/missing statuses fail closed as
`failed`, including a completion erroneously marked `running`. This fallback is
not proof of a real execution failure; it deliberately never invents success.
Old frames with no outcome metadata therefore remain terminal `failed`.

Registry `bg_status` snapshots report exited processes as `done` only when the
exit code equals zero; an unknown exit code is `failed`. Live rows are `running`.
The reducer's internal `done` maps to card `status=completed`.

Cards use `delegate_task` only for delegations and `terminal` for shell jobs.
Older metadata-free frames infer delegation kind from `deleg_`; other IDs fall
back to process kind. Explicit kind wins. Known titles survive completion, and
missing titles use generic labels rather than repeating the raw task ID.
Both kinds retain task-ID-based DOM identity, including active-stream cards.

These changes affect live browser state and future frames, not durable task
outcomes, delivery/claim state, old saved card markup, or SSE replay policy.

Run the browser regression table with
`node tests/background_card_outcomes.cjs .` and server/wire regressions with
`./scripts/test.sh -q tests/test_background_card_payload.py`.
