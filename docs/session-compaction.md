# Archive-first display compaction

`tools/drain_webui_compaction.py` consumes the memory monitor's sibling
`webui-compaction-queue.json`. It processes one largest candidate per invocation
by default. The queue is a stat-based inventory, not a transactional work queue:
do not delete or rewrite it. Already-compacted and below-threshold entries are
idempotently refused/skipped until the monitor publishes another inventory.

Run only in a **quiescent maintenance window**, with every sidecar writer stopped
and prevented from restarting. `--writer-service` checks the named user service
before reading and before committing; `--quiescent-maintenance` asserts the
stronger all-writer exclusion that a service status check alone cannot prove.
An advisory drainer lock serializes drainers, NOT existing WebUI writers.
Fingerprint revalidation catches accidental touch/append/replace, but is not a
substitute for writer exclusion across the final check-and-rename interval.
Never use this command concurrently with a live WebUI or external JSON writer.

Prefer a node maintenance job over an in-process daemon: parsing a very large
sidecar temporarily allocates several copies. Doing that inside a memory-stressed
WebUI adds to its cgroup/RSS and can block live turns. An ordinary always-on cron
must skip while the service is active; it must not stop the service itself.
This tool never stops/starts services, changes limits, edits SQLite, or deletes
session/archive data. Offline startup also avoids stale resident graphs saving
the untrimmed display back over the compacted sidecar.

The complete original byte stream is gzip-archived, fsynced, atomically published
outside the served session directory, and directory-fsynced **before** trimming.
Archives are private (0600). Every message row, identifier, timestamp, user
message, last 100 messages, and `context_messages` remain. Only older assistant
and tool display text is shortened to a labelled 512-byte preview pointing to
the full archive. This is display archival, not LLM summarization. An explicit
model context is required; there is no fallback to using shortened display as
context. All other fields and `updated_at` are retained. Subsequent DB display
reconciliation can rehydrate some archived text; this does not compact state.db
or claim permanent database size relief. Very large user/context/tool metadata
can leave a sidecar over threshold; inspect the receipt, never discard it.

The result is written to a unique 0600 staging file and fsynced. The original
device/inode/size/mtime/ctime and SHA-256 are checked again, then the staged result
is atomically replaced and its directory fsynced. On refusal, archives and
staging evidence remain for inspection. No automatic cleanup deletes them.

Recovery: during the same all-writer exclusion, decompress the receipt's archive
to a NEW staged file, verify its SHA-256 against the receipt, fsync it, archive
the current sidecar too, then atomically replace the sidecar. Never overwrite
an intervening/newer turn with an older archive. Archives contain private full
transcripts: do not serve them as static assets or ship them with a patch kit.

Example (substitute an operator-controlled maintenance window and paths):

```sh
python tools/drain_webui_compaction.py --quiescent-maintenance \
  --writer-service hermes-webui-home.service \
  --queue /path/to/tools/webui-compaction-queue.json \
  --sessions /path/to/sessions --archives /private/session-archives --limit 1
```
