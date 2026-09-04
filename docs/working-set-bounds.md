# Session-read working-set bounds

This node-side wave addresses the September 1–2 OOMs and September 3 sustained
memory.high throttling without changing service memory ceilings or agent code.

## Request ownership

Authentication still runs in `server.py` before the GET dispatcher. The response
cache retains only already-projected, redacted JSON bytes; it cannot bypass
authentication. Keys include profile/home, query/window, session inode/mtime/size,
DB/WAL and index/settings stamps, projection versions, and mutation generation.
Committed session-list events invalidate the generation. The five-second TTL is
a safety net for in-memory runtime changes and external writers. Cookie/security
headers are emitted fresh and gzip is negotiated per request.

The LRU has a 256 MiB default cap, counts key/entry overhead, and rejects entries
larger than one quarter of its cap. `HERMES_WEBUI_CACHE_BYTES` overrides the cap.
`HERMES_WEBUI_INFLIGHT_BYTES` overrides the 512 MiB compile budget. The gate admits
at most two compiles and 64 queued requests, FIFO, with a two-second deadline.
Failures return `429 memory_budget` and `Retry-After: 2`; no unbounded fallback.
Each request reserves twelve times a raw-input allowance of at least 8 MiB or
twice its sidecar size (sidecar plus DB merge). This multiplier is an estimate,
not a proof of a universal Python-object expansion ratio or a process RSS cap.

Opened file reads enforce the allowance before JSON parsing, and recheck the
descriptor after reading. DB reads measure BLOB byte lengths plus row overhead
in the same SQLite transaction as the subsequent fetch. Budget refusal crosses
legacy catch-and-empty-history handlers and is caught at the HTTP boundary.
Clean historical reads do not populate the mutable writer session cache;
existing live/dirty objects remain authoritative and are never discarded here.

## Projection and metadata

Default transcript reads use the existing offset-based paging contract with a
200-row default and a 1.5 MiB contiguous message-tail ceiling. Offset and
`_messages_truncated` remain authoritative; no canonical messages are removed.
A single oversized message returns `413 message_window_too_large` instead of
silently changing its content. `full=1` returns an explicit 409 directing clients
to existing export/offset paging; a truly streaming full-history reader is deferred.

The sessions endpoint reuses serialized responses/ETags. Expired inner metadata
snapshots refresh on one background worker under the shared admission gate,
with captured profile context and guaranteed ownership cleanup. Cold snapshots
return `503 metadata_refresh_pending` until ready; request threads never rebuild
the full index as a fallback. Refresh is demand-triggered, not an independent
five-second watcher. Existing inner metadata caches retain their compatibility
shapes and count limits; they are not all converted to byte-LRUs in this wave.

Automatic readers can send `X-WebUI-Poll: hidden` for a 60-second server-side
floor or another non-foreground automatic marker for a five-second floor. The
active foreground reader sends `X-WebUI-Poll: visible` and is exempt from that
floor: its 650ms–2s cadence supports the visible conversation, and refusing it
turns a first click into “Failed to load session.” Native external-session
polling keeps its existing cadence and hidden-tab skip. Pressure still refuses
marked polls before compile admission, while direct human reads use FIFO
admission. Unmarked legacy sidecars benefit from byte-cache reuse; their
producer-side cadence/jitter/backoff must be updated separately.

## Telemetry and rollout

Only the named production service cgroup starts the one-minute sampler. Test and
manually launched servers must not overwrite production telemetry. The service
owns sampler startup/shutdown; stale or invalid telemetry halves cache/admission
budgets and refuses marked polls. No ceiling is raised and no restart is automatic.
`/internal/memstats` is behind the existing authentication gate. See
`ws-memory-monitor.md` for sustained rate thresholds and independent G5 limits,
and `session-archive-policy.md` for detached no-delete staging/restore.

Still gated: independent timer/paging and automation freeze, full streamed
history and canonical archive dual-read migration, bounded general HTTP/SSE pool,
allocator tuning, complete retained-graph audit, and 7/14-day soak acceptance.
Do not lower MemoryHigh/MemoryMax during this deployment. Roll back only the
source patch with a separately authorized service restart; canonical state has
not been migrated, so no session restore is needed to roll back this wave.
