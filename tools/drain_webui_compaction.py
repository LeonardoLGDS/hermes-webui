"""Archive-first compaction of quiescent WebUI display sidecars, not model context."""

import argparse
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid


MAX_SESSION_BYTES = 128 * 1024 * 1024
MAX_QUEUE_BYTES = 1024 * 1024
MIN_SESSION_BYTES = 8 * 1024 * 1024
KEEP_RECENT = 100
DISPLAY_TEXT_BYTES = 512


class Refused(RuntimeError):
    pass


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_regular(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise Refused("not a bounded regular file")
        payload = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(payload) > limit or signature(before) != signature(after):
        raise Refused("file changed while reading")
    return payload, before


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new(path, payload):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def parse_json(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Refused("duplicate JSON member")
            result[key] = value
        return result

    def invalid_constant(value):
        raise Refused("non-finite JSON number")

    return json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid_constant)


def compact_display(document, archive_name, original_digest):
    messages = document.get("messages")
    if not isinstance(messages, list) or any(not isinstance(row, dict) for row in messages):
        raise Refused("unsupported messages schema")
    if not isinstance(document.get("context_messages"), list):
        raise Refused("explicit model context required; cannot compact a context fallback")
    if any(document.get(key) for key in (
        "active_stream_id", "pending_user_message", "pending_started_at", "pending_attachments",
    )):
        raise Refused("active or pending session")
    if document.get("display_compaction"):
        raise Refused("already compacted; inspect before another compaction")
    changed = 0

    def shorten(value):
        nonlocal changed
        if isinstance(value, str) and len(value.encode("utf-8")) > DISPLAY_TEXT_BYTES:
            changed += 1
            prefix = value.encode("utf-8")[:DISPLAY_TEXT_BYTES].decode("utf-8", errors="ignore")
            return prefix + f"\n[Archived display text: {archive_name}; full original retained]"
        if isinstance(value, list):
            return [shorten(item) for item in value]
        if isinstance(value, dict):
            return {key: shorten(item) for key, item in value.items()}
        return value

    for row in messages[:-KEEP_RECENT]:
        if row.get("role") not in {"assistant", "tool"}:
            continue
        for key in ("content", "reasoning", "reasoning_content"):
            if key in row:
                row[key] = shorten(row[key])
    document["message_count"] = len(messages)
    document["display_compaction"] = {
        "version": 1, "archive": archive_name, "sha256": original_digest,
        "original_message_count": len(messages), "preserved_message_count": len(messages),
        "shortened_display_fields": changed, "recent_messages_untouched": KEEP_RECENT,
    }
    return document


def compact_one(session_dir, archive_dir, name, *, assert_quiescent, before_commit=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}\.json", name) or name == "_index.json":
        raise Refused("invalid session name")
    assert_quiescent()
    path = Path(session_dir) / name
    original, before = read_regular(path, MAX_SESSION_BYTES)
    if before.st_size <= MIN_SESSION_BYTES:
        return {"name": name, "status": "below_threshold", "before_bytes": before.st_size}
    document = parse_json(original)
    if not isinstance(document, dict) or document.get("session_id") != name[:-5]:
        raise Refused("session identity mismatch")
    if document.get("display_compaction"):
        return {"name": name, "status": "already_compacted", "before_bytes": before.st_size}
    digest = hashlib.sha256(original).hexdigest()
    archive_dir = Path(archive_dir)
    if archive_dir.is_symlink() or archive_dir.resolve() == Path(session_dir).resolve():
        raise Refused("archive must be a separate private directory")
    archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive_name = f"{name}.{digest}.{uuid.uuid4().hex}.gz"
    archive = archive_dir / archive_name
    archive_stage = archive.with_name(archive.name + ".part")
    write_new(archive_stage, gzip.compress(original, compresslevel=1, mtime=0))
    os.rename(archive_stage, archive)
    sync_directory(archive_dir)
    document = compact_display(document, archive_name, digest)
    replacement = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(replacement) >= len(original):
        return {"name": name, "status": "no_savings", "archive": str(archive)}
    stage = path.with_name(path.name + f".compact-{uuid.uuid4().hex}.part")
    write_new(stage, replacement)
    if before_commit is not None:
        before_commit(path)
    assert_quiescent()
    current, current_stat = read_regular(path, MAX_SESSION_BYTES)
    if signature(before) != signature(current_stat) or hashlib.sha256(current).hexdigest() != digest:
        raise Refused(f"session changed mid-run; archive retained: {archive}")
    if signature(path.lstat()) != signature(before):
        raise Refused(f"session replaced mid-run; archive retained: {archive}")
    os.replace(stage, path)
    sync_directory(path.parent)
    return {"name": name, "status": "compacted", "before_bytes": len(original),
            "after_bytes": len(replacement), "preserved_message_count": len(document["messages"]),
            "archive": str(archive), "sha256": digest}


def drain(queue_path, session_dir, archive_dir, *, assert_quiescent, limit=1):
    if not 1 <= limit <= 13:
        raise Refused("limit must be between 1 and 13")
    queue, _ = read_regular(queue_path, MAX_QUEUE_BYTES)
    queue = parse_json(queue)
    if queue.get("version") != 1 or not isinstance(queue.get("entries"), list):
        raise Refused("unsupported queue")
    if len(queue["entries"]) > 4096:
        raise Refused("queue entry limit exceeded")
    entries = queue["entries"]
    if any(not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
           or type(entry.get("size_bytes")) is not int for entry in entries):
        raise Refused("malformed queue entry")
    results = []
    attempted = 0
    for entry in sorted(entries, key=lambda item: item["size_bytes"], reverse=True):
        try:
            receipt = compact_one(session_dir, archive_dir, entry["name"], assert_quiescent=assert_quiescent)
        except (Refused, OSError, ValueError) as error:
            receipt = {"name": entry["name"], "status": "refused", "reason": str(error)}
        results.append(receipt)
        print(json.dumps(receipt), flush=True)
        if receipt["status"] not in {"below_threshold", "already_compacted"}:
            attempted += 1
        if attempted >= limit:
            break
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--archives", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--writer-service", required=True)
    parser.add_argument("--quiescent-maintenance", action="store_true", required=True,
                        help="assert all sidecar writers are stopped and cannot restart during this run")
    args = parser.parse_args()

    def quiescent():
        result = subprocess.run(
            ["systemctl", "--user", "show", args.writer_service, "--property=ActiveState", "--value"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        if result.stdout.strip() != "inactive":
            raise Refused("writer service is not inactive")

    args.archives.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.archives / ".drain.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        quiescent()
        results = drain(args.queue, args.sessions, args.archives, assert_quiescent=quiescent, limit=args.limit)
    return 2 if any(item["status"] == "refused" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
