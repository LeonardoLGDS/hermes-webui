# Detached session archive policy — wave 1

Status: implemented for isolated review; **not a production migration**. Design
authority: Q3 of `final_plan-20260903.md` (2026-09-03), with the wave-1 staging
restriction. Independent postimplementation review is required before acceptance.

The 09-01/02 OOM incidents and unbounded store motivate bounded hot snapshots,
not higher memory ceilings. This tool never imports the WebUI or agent, starts a
service, schedules work, updates sessions, hides files, deletes files, or evicts
caches. The only mutable state is a caller-specified detached output directory.
Canonical bytes, inode, timestamps (including atime), and pathname are retained.

## Policy and dry-run

`python3 tools/session_archive.py --dry-run /path/to/fixture-store`

The required explicit root is walked without opening session contents. Each
regular `.json` file is treated as a legacy session candidate, including nested
files. Point this at the legacy session store, not an archive/output tree.
All regular files count toward total logical bytes (not allocated disk blocks).
Symlinks, special files, unreadable entries and a non-directory root fail closed.
This is a point-in-time best-effort stat inventory, not a transactional store
snapshot; concurrent store changes require a fresh owner-reviewed report.

| Strictly greater than | Report behavior |
| --- | --- |
| 8 MiB (8,388,608 bytes) per session | `enqueue: true` |
| 32 MiB (33,554,432 bytes) per session | `alarm: true` and enqueue |
| 3.2 GB (3,200,000,000 bytes) total | `store_alarm: true` |

Equality does not cross a threshold. Reports go to stdout; there is **no durable
queue, paging integration, background worker, timer, or auto-compaction**. A
future consumer must translate those fields into actions. Scan freshness (>26h)
monitoring is an integration requirement, not implemented here.

## Explicit fixture staging

Create a separate owner-controlled directory with mode 0700, outside the source
session tree. Then, on fixtures only during this wave:

```sh
python3 tools/session_archive.py --stage /path/to/fixture-store/session.json \
  --output /path/to/detached-output
python3 tools/session_archive.py --restore /path/to/detached-output/snapshot-ID \
  --output /path/to/detached-output
```

There are no implicit state paths or environment-derived agent homes. `--stage`
processes exactly one explicitly named file, regardless of enqueue threshold.
It requires mtime age **strictly greater than 900 seconds** and a readable
`/proc/self/cgroup` showing no `hermes-webui` component. Restore also enforces the
cgroup restriction. Unknown isolation, active locks, changed files and unsafe
paths are errors, with no automatic retry. Linux `O_NOATIME` is required: source
ownership or permission to use it is necessary; there is no atime-writing fallback.

Input schema is a JSON object with an array of object-valued `messages`.
Other top-level fields are retained in `metadata.json`. Duplicate JSON keys,
nonfinite numbers and unsupported structures are refused, never normalized away.
This is a proposed derived schema, not a verified runtime schema contract.

The standard-library JSON parser is not streaming. Files above **64 MiB** are
refused before parsing; the dry-run still reports/enqueues/alarms on them. Only
one file is parsed at a time. This byte cap is not an RSS guarantee: JSON object
expansion can be substantial, particularly with many tiny objects. A streaming
parser and measured external resource budget remain prerequisites for large-file
production work. This tool must not be run inside the WebUI cgroup.

## Snapshot format and integrity

Each successful operation publishes a new immutable-by-convention directory;
there is no in-place append or update API:

- `originals/original.json`: complete original bytes, uncompressed, retained
  indefinitely. This intentionally avoids a zstd dependency in wave 1.
- `metadata.json`: every top-level field except `messages`.
- `head.json`: a consecutive suffix of at most 200 message records, serialized
  size at most **1,500,000 bytes**. Each record includes its absolute zero-based
  index and original-message canonical-JSON SHA-256. Fewer than 200 messages may
  fit the byte budget. The full historical index is in segment records, not an
  unbounded index in the head.
- `segments/000000.jsonl`, etc.: older records in original order, targeted at
  1,000,000 bytes per segment. A record is never split across lines or files.
- `blobs/<sha256>.json`: canonical serialized **whole message bodies** larger
  than 256 KiB, referenced by records with a 4 KiB preview. Canonical JSON uses
  ASCII escaping, so this threshold concerns serialized bytes, not character
  count. Whole-message externalization preserves nested tool/multimodal content
  without guessing which fields constitute a body. Duplicate blobs are shared
  within a snapshot; duplicate messages retain distinct ordered indexes.
- `MANIFEST.json`: version, derived-only marker, original digest, source stat
  identity, message count, ordered segment names, and SHA-256/byte length of
  every payload artifact. The manifest does not hash itself and is not signed.

`verify()` checks every listed artifact, metadata equality, count, and ordered
per-message byte/hash equality against the retained original. This is stronger
than set equality (which loses duplicates and ordering). These are corruption
checks, not authenticity protection against someone who can rewrite the entire
snapshot, including its manifest. Snapshots contain unredacted private data;
keep them private, not served or publicly uploaded.

`--restore` first verifies the snapshot, writes a **new detached**
`restore-ID/original.json`, and verifies its hash before publication. It never
restores onto a canonical session path, overwrites an existing file, or performs
rollback of a live store. The returned original copy is byte-for-byte lossless,
including whitespace and key order; derived JSON need not have the original
formatting.

## Locking and failure ownership

The source is opened read-only/no-follow/no-atime and held under a nonblocking
exclusive advisory flock. Its device/inode/size/mtime/ctime are captured. Before
publication, the tool rehashes the held descriptor and compares both descriptor
and pathname stat identities. Output operations hold a nonblocking flock on the
existing private output directory; no lockfiles are placed in the source store.
Descriptors and locks are released on every exit.

Artifacts use exclusive creation, 0600 files and 0700 directories under a unique
`.pending-ID`. Files and directories are fsynced; only after verification and the
source recheck is the whole directory atomically renamed to `snapshot-ID` on the
same filesystem, then the output directory is fsynced. The same protocol applies
to restore. Existing snapshots are not reused or overwritten. A publication
fsync error may leave a complete visible snapshot but returns failure; inspect it
instead of blindly retrying.

On failure, partial `.pending-ID` data is deliberately retained for investigation,
never deleted. It is not a published snapshot and no reader should enumerate it.
This includes cancellation/crash leftovers; repeated attempts consume space.
No cleanup command is supplied. Repeated successful stages also retain duplicate
originals, so staging grows storage rather than shrinking the canonical store.

Advisory locks do not constrain noncooperating writers. Source/path rechecks
detect observed changes but cannot prevent a write after the final check.
The tool only promises a verified snapshot of observed bytes, not a production
transaction. Source and output directories must remain controlled by the same
trusted operator for the operation; hostile same-UID namespace replacement is
outside this offline tool's contract. Do not run against mutable live state.

## Evidence and integration gates

Tests use generated temporary fixtures only, with cgroup readings replaced by
test values. No server or agent modules are loaded. Reproduce with:

```sh
PYTHONDONTWRITEBYTECODE=1 \
PYTEST_ADDOPTS='--noconftest -p no:cacheprovider' \
timeout 60 ./scripts/test.sh tests/test_session_archive_policy.py
```

`--noconftest` is essential here: repository-wide fixtures otherwise inspect
agent/runtime state. Test receipts belong in the external task directory, not the
source tree. Fixture evidence does not demonstrate production performance,
production cgroup placement, live writer locking, or actual store compatibility.

Before any production migration: independent review; owner approval of an actual
dry-run; a separately implemented dual-read shim (100% legacy initially); verified
runtime schema mapping; coordinated per-session writer locks; cold-first
selection; escaped-cgroup/resource limits; alert/queue/freshness wiring; retention
capacity planning; and restoration proof. Cache invalidation after a canonical
commit, hiding semantics, zstd originals, actual canonical splitting, live
rollback, and deployment are **explicitly deferred**. None is enabled by this
tool or by passing `--stage`.
