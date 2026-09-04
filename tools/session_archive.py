"""Offline, non-migrating session archival policy (Linux, standard library only)."""

import argparse
from collections import deque
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid


ENQUEUE_BYTES = 8 * 1024**2
SESSION_ALARM = 32 * 1024**2
STORE_ALARM = 3_200_000_000
HEAD_BYTES = 1_500_000
BLOB_BYTES = 256 * 1024
SEGMENT_BYTES = 1_000_000
MAX_SOURCE_BYTES = 64 * 1024**2
COLD_SECONDS = 15 * 60


class PolicyError(ValueError):
    """Unsafe or unsupported staging input; canonical state is never changed."""


def _encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"),
                      sort_keys=True, allow_nan=False).encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise PolicyError(f"non-finite JSON constant: {value}")


def _decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise PolicyError(f"unsupported JSON: {error}") from error


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _path(path):
    path = Path(os.path.abspath(path))
    if path.resolve(strict=True) != path:
        raise PolicyError("symlink paths are not allowed")
    return path


def _cgroup_text():
    return Path("/proc/self/cgroup").read_text()


def _outside_webui():
    text = _cgroup_text()
    lines = text.strip().splitlines()
    if not lines or any(len(line.split(":", 2)) != 3 for line in lines):
        raise PolicyError("cannot establish cgroup isolation")
    if "hermes-webui" in text:
        raise PolicyError("staging/restore forbidden in hermes-webui cgroup")


@contextmanager
def _locked_source(path):
    # WHY: 09-01/02 OOM and unbounded-store remediation must not touch canonical
    # state, even atime. Fail closed if Linux no-atime reads are unavailable.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PolicyError("source must be regular")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


@contextmanager
def _output_lock(output, forbidden):
    output = _path(output)
    if output == forbidden or output.is_relative_to(forbidden):
        raise PolicyError("output must be outside the source tree")
    descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PolicyError("output must be an owned, private 0700 directory")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield output
        if _identity(os.stat(output))[:2] != _identity(info)[:2]:
            raise PolicyError("output directory changed")
    finally:
        os.close(descriptor)


def scan(store):
    """Read-only recursive size inventory; enqueue/alarm are report fields only."""
    store = _path(store)
    if not store.is_dir():
        raise PolicyError("scan requires a directory")
    total = 0
    sessions = []
    for directory, folders, files in os.walk(store, followlinks=False, onerror=_scan_error):
        for name in folders + files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise PolicyError(f"non-regular scan entry: {path}")
            total += info.st_size
            if path.suffix == ".json":
                sessions.append({"path": str(path.relative_to(store)), "bytes": info.st_size,
                                 "enqueue": info.st_size > ENQUEUE_BYTES,
                                 "alarm": info.st_size > SESSION_ALARM})
    return {"dry_run": True, "total_bytes": total, "store_alarm": total > STORE_ALARM,
            "sessions": sorted(sessions, key=lambda item: item["path"])}


def _scan_error(error):
    raise error


def _write(root, name, raw, artifacts):
    path = root / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    artifacts[name] = {"sha256": _sha(raw), "bytes": len(raw)}


def _fsync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(pending, output, prefix):
    for directory, _, _ in os.walk(pending, topdown=False):
        _fsync_dir(directory)
    destination = output / (prefix + pending.name.removeprefix(".pending-"))
    if destination.exists():
        raise PolicyError("publication target exists")
    os.rename(pending, destination)
    _fsync_dir(output)
    return destination


def _document(raw):
    document = _decode(raw)
    if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
        raise PolicyError("expected object with messages array")
    if not all(isinstance(message, dict) for message in document["messages"]):
        raise PolicyError("messages must be objects")
    return document


def _unchanged(source, stream, before, digest):
    stream.seek(0)
    checksum = hashlib.sha256()
    remaining = before[2] + 1
    while remaining and (chunk := stream.read(min(1024 * 1024, remaining))):
        checksum.update(chunk)
        remaining -= len(chunk)
    if (checksum.hexdigest() != digest or _identity(os.fstat(stream.fileno())) != before
            or _identity(source.lstat()) != before):
        raise PolicyError("source changed during staging")


def stage(source, output):
    """Create a verified detached snapshot; never rename/write the source."""
    _outside_webui()
    source = _path(source)
    with _locked_source(source) as stream, _output_lock(output, source.parent) as output:
        info = os.fstat(stream.fileno())
        before = _identity(info)
        if time.time_ns() - info.st_mtime_ns <= COLD_SECONDS * 1_000_000_000:
            raise PolicyError("source must be cold for more than 15 minutes")
        # WHY: 09-01/02 OOM exposed an unbounded store. A stdlib whole-JSON
        # parser gets a hard per-file cap, not a loop loading the entire store.
        if info.st_size > MAX_SOURCE_BYTES:
            raise PolicyError("source exceeds staging parser limit; streaming reader deferred")
        raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) != info.st_size:
            raise PolicyError("source changed during read")
        document = _document(raw)
        digest = _sha(raw)
        pending = output / (".pending-" + uuid.uuid4().hex)
        pending.mkdir(mode=0o700)
        artifacts = {}
        _write(pending, "originals/original.json", raw, artifacts)
        del raw
        metadata = {key: value for key, value in document.items() if key != "messages"}
        _write(pending, "metadata.json", _encode(metadata), artifacts)
        head = deque()
        head_size = len(b'{"messages":[]}')
        segment = bytearray()
        segments = []

        def flush_segment():
            if segment:
                name = f"segments/{len(segments):06d}.jsonl"
                _write(pending, name, bytes(segment), artifacts)
                segments.append(name)
                segment.clear()

        for index, message in enumerate(document["messages"]):
            body = _encode(message)
            record = {"index": index, "sha256": _sha(body)}
            if len(body) > BLOB_BYTES:
                name = f"blobs/{_sha(body)}.json"
                if name not in artifacts:
                    _write(pending, name, body, artifacts)
                record.update(blob=name, preview=body[:4096].decode("ascii"))
            else:
                record["message"] = message
            encoded = _encode(record)
            head.append(encoded)
            head_size += len(encoded) + 1
            while len(head) > 200 or head_size > HEAD_BYTES:
                older = head.popleft()
                head_size -= len(older) + 1
                if segment and len(segment) + len(older) + 1 > SEGMENT_BYTES:
                    flush_segment()
                segment.extend(older + b"\n")
        flush_segment()
        _write(pending, "head.json", b'{"messages":[' + b",".join(head) + b"]}", artifacts)
        manifest = {"version": 1, "derived_only": True, "original_sha256": digest,
                    "source_stat": list(before), "message_count": len(document["messages"]),
                    "segments": segments, "artifacts": artifacts}
        _write(pending, "MANIFEST.json", _encode(manifest), {})
        del document, metadata
        verify(pending)
        _unchanged(source, stream, before, digest)
        _outside_webui()
        return _publish(pending, output, "snapshot-")


def _artifact(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise PolicyError("unsafe artifact path")
    path = _path(root / name)
    if not path.is_relative_to(root) or not path.is_file():
        raise PolicyError("artifact outside snapshot")
    with _locked_source(path) as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise PolicyError("artifact exceeds verification limit")
    return raw


def verify(snapshot):
    """Check all hashes plus metadata and ordered message equality (duplicates kept)."""
    snapshot = _path(snapshot)
    manifest = _decode(_artifact(snapshot, "MANIFEST.json"))
    if manifest.get("version") != 1 or manifest.get("derived_only") is not True:
        raise PolicyError("unsupported snapshot")

    def checked(name):
        raw = _artifact(snapshot, name)
        expected = manifest["artifacts"].get(name)
        if expected != {"sha256": _sha(raw), "bytes": len(raw)}:
            raise PolicyError(f"artifact checksum mismatch: {name}")
        return raw

    for name in manifest["artifacts"]:
        checked(name)
    raw = checked("originals/original.json")
    if _sha(raw) != manifest["original_sha256"]:
        raise PolicyError("original checksum mismatch")
    document = _document(raw)
    del raw
    metadata = {key: value for key, value in document.items() if key != "messages"}
    if _encode(_decode(checked("metadata.json"))) != _encode(metadata):
        raise PolicyError("metadata mismatch")
    head_raw = checked("head.json")
    head = _decode(head_raw)["messages"]
    if len(head_raw) > HEAD_BYTES or len(head) > 200:
        raise PolicyError("head exceeds policy")
    count = 0

    def records():
        for name in manifest["segments"]:
            for line in checked(name).splitlines():
                yield _decode(line)
        yield from head

    for record in records():
        if count >= len(document["messages"]) or record["index"] != count:
            raise PolicyError("message order/count mismatch")
        body = checked(record["blob"]) if "blob" in record else _encode(record["message"])
        if _sha(body) != record["sha256"] or body != _encode(document["messages"][count]):
            raise PolicyError("message checksum mismatch")
        count += 1
    if count != manifest["message_count"] or count != len(document["messages"]):
        raise PolicyError("message count mismatch")
    return manifest


def restore(snapshot, output):
    """Export verified original bytes to a NEW detached restore directory only."""
    _outside_webui()
    snapshot = _path(snapshot)
    manifest = verify(snapshot)
    raw = _artifact(snapshot, "originals/original.json")
    if _sha(raw) != manifest["original_sha256"]:
        raise PolicyError("original changed before restore")
    with _output_lock(output, snapshot) as output:
        pending = output / (".pending-" + uuid.uuid4().hex)
        pending.mkdir(mode=0o700)
        _write(pending, "original.json", raw, {})
        if _sha(_artifact(pending, "original.json")) != manifest["original_sha256"]:
            raise PolicyError("restored checksum mismatch")
        _outside_webui()
        return _publish(pending, output, "restore-") / "original.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", metavar="STORE", type=Path)
    mode.add_argument("--stage", metavar="SOURCE", type=Path)
    mode.add_argument("--restore", metavar="SNAPSHOT", type=Path)
    parser.add_argument("--output", type=Path, help="existing private detached output directory")
    args = parser.parse_args(argv)
    if bool(args.output) != bool(args.stage or args.restore):
        parser.error("--output is required only for --stage/--restore")
    try:
        if args.dry_run:
            result = scan(args.dry_run)
        elif args.stage:
            result = {"snapshot": str(stage(args.stage, args.output)), "derived_only": True}
        else:
            result = {"restored_copy": str(restore(args.restore, args.output))}
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        parser.exit(2, f"session archive refused: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
