"""Detached policy tests: no server, agent imports, or real session data."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time

import pytest


spec = importlib.util.spec_from_file_location(
    "session_archive", Path(__file__).parents[1] / "tools/session_archive.py"
)
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


@pytest.fixture
def fixture_store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    output = tmp_path / "detached"
    output.mkdir(mode=0o700)
    source = store / "fixture.json"
    messages = [{"role": "user", "content": "repeat"}] * 205
    messages += [{"role": "tool", "content": "界" * 100000}]
    raw = json.dumps({"messages": messages, "other": {"keep": True}}, indent=2).encode()
    source.write_bytes(raw)
    cold = time.time() - 1000
    os.utime(source, (cold, cold))
    monkeypatch.setattr(archive, "_cgroup_text", lambda: "0::/test-fixture\n")
    return store, source, output, raw


def test_scan_strict_boundaries_and_no_writes(tmp_path):
    sizes = [8 * 1024**2, 8 * 1024**2 + 1, 32 * 1024**2, 32 * 1024**2 + 1]
    for index, size in enumerate(sizes):
        with (tmp_path / f"{index}.json").open("wb") as stream:
            stream.truncate(size)
    before = sorted(tmp_path.iterdir())
    report = archive.scan(tmp_path)
    assert [item["enqueue"] for item in report["sessions"]] == [False, True, True, True]
    assert [item["alarm"] for item in report["sessions"]] == [False, False, False, True]
    assert report["total_bytes"] == sum(sizes)
    assert not report["store_alarm"]
    assert sorted(tmp_path.iterdir()) == before
    with (tmp_path / "retained.dat").open("wb") as stream:
        stream.truncate(archive.STORE_ALARM - sum(sizes))
    assert not archive.scan(tmp_path)["store_alarm"]
    with (tmp_path / "retained.dat").open("ab") as stream:
        stream.write(b"x")
    assert archive.scan(tmp_path)["store_alarm"]


def test_lossless_stage_and_restore(fixture_store):
    store, source, output, raw = fixture_store
    before = source.stat()
    snapshot = archive.stage(source, output)
    after = source.stat()
    assert archive._identity(before) == archive._identity(after)
    assert before.st_atime_ns == after.st_atime_ns
    assert source.read_bytes() == raw
    assert list(store.iterdir()) == [source]
    manifest = archive.verify(snapshot)
    assert manifest["message_count"] == 206
    assert (snapshot / "originals/original.json").read_bytes() == raw
    head = json.loads((snapshot / "head.json").read_bytes())
    assert len(head["messages"]) <= 200
    assert (snapshot / "head.json").stat().st_size <= archive.HEAD_BYTES
    assert list((snapshot / "segments").glob("*.jsonl"))
    assert list((snapshot / "blobs").glob("*.json"))
    restored = archive.restore(snapshot, output)
    assert restored.read_bytes() == raw
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == manifest["original_sha256"]


def test_head_byte_cap(fixture_store):
    _, source, output, _ = fixture_store
    source.write_text(json.dumps({"messages": [{"content": "x" * 200000}] * 12}))
    os.utime(source, (time.time() - 1000,) * 2)
    snapshot = archive.stage(source, output)
    head = json.loads((snapshot / "head.json").read_bytes())
    assert 0 < len(head["messages"]) < 12
    assert (snapshot / "head.json").stat().st_size <= archive.HEAD_BYTES
    archive.verify(snapshot)


@pytest.mark.parametrize("cgroup", ["0::/system.slice/hermes-webui-home.service", "1:memory:/hermes-webui.service", ""])
def test_cgroup_refused(fixture_store, monkeypatch, cgroup):
    _, source, output, _ = fixture_store
    monkeypatch.setattr(archive, "_cgroup_text", lambda: cgroup)
    with pytest.raises(archive.PolicyError):
        archive.stage(source, output)
    assert not list(output.iterdir())


def test_hot_and_oversized_refused(fixture_store):
    _, source, output, _ = fixture_store
    os.utime(source, None)
    with pytest.raises(archive.PolicyError, match="cold"):
        archive.stage(source, output)
    with source.open("wb") as stream:
        stream.truncate(archive.MAX_SOURCE_BYTES + 1)
    os.utime(source, (time.time() - 1000,) * 2)
    with pytest.raises(archive.PolicyError, match="limit"):
        archive.stage(source, output)
    assert not list(output.iterdir())


def test_source_lock_refused(fixture_store):
    import fcntl

    _, source, output, _ = fixture_store
    with source.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            archive.stage(source, output)
    assert not list(output.iterdir())
    archive.stage(source, output)


def test_changed_source_never_published(fixture_store, monkeypatch):
    _, source, output, _ = fixture_store
    real_verify = archive.verify

    def mutate_after_verification(snapshot):
        result = real_verify(snapshot)
        source.write_bytes(b'{"messages": []}')
        return result

    monkeypatch.setattr(archive, "verify", mutate_after_verification)
    with pytest.raises(archive.PolicyError, match="changed"):
        archive.stage(source, output)
    assert not list(output.glob("snapshot-*"))
    assert list(output.glob(".pending-*"))


def test_tampering_refuses_restore(fixture_store):
    _, source, output, _ = fixture_store
    snapshot = archive.stage(source, output)
    (snapshot / "head.json").write_text("{}")
    with pytest.raises(archive.PolicyError):
        archive.restore(snapshot, output)
    assert not list(output.glob("restore-*"))


def test_unsafe_paths_refused(fixture_store, tmp_path):
    store, source, output, _ = fixture_store
    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(archive.PolicyError):
        archive.stage(link, output)
    with pytest.raises(archive.PolicyError):
        archive.stage(source, store)
    output.chmod(0o755)
    with pytest.raises(archive.PolicyError):
        archive.stage(source, output)
    (store / "alias.json").symlink_to(source)
    with pytest.raises(archive.PolicyError):
        archive.scan(store)


@pytest.mark.parametrize("raw", [b'{"messages":[],"messages":[]}', b'{"messages":NaN}', b'{"other":1}'])
def test_invalid_json_fails_without_publication(fixture_store, raw):
    _, source, output, _ = fixture_store
    source.write_bytes(raw)
    os.utime(source, (time.time() - 1000,) * 2)
    with pytest.raises(archive.PolicyError):
        archive.stage(source, output)
    assert not list(output.glob("snapshot-*"))


def test_empty_session(fixture_store):
    _, source, output, _ = fixture_store
    source.write_text('{"messages":[], "metadata": "retained"}')
    os.utime(source, (time.time() - 1000,) * 2)
    assert archive.verify(archive.stage(source, output))["message_count"] == 0


def test_order_tampering_detected_even_with_resealed_artifact(fixture_store):
    _, source, output, _ = fixture_store
    source.write_text('{"messages":[{"content":"first"},{"content":"second"}]}')
    os.utime(source, (time.time() - 1000,) * 2)
    snapshot = archive.stage(source, output)
    head = json.loads((snapshot / "head.json").read_bytes())
    head["messages"].reverse()
    for index, record in enumerate(head["messages"]):
        record["index"] = index
    raw = archive._encode(head)
    (snapshot / "head.json").write_bytes(raw)
    manifest = json.loads((snapshot / "MANIFEST.json").read_bytes())
    manifest["artifacts"]["head.json"] = {"sha256": archive._sha(raw), "bytes": len(raw)}
    (snapshot / "MANIFEST.json").write_bytes(archive._encode(manifest))
    with pytest.raises(archive.PolicyError, match="message checksum"):
        archive.verify(snapshot)


def test_output_lock_refused_and_released(fixture_store):
    import fcntl

    _, source, output, _ = fixture_store
    descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            archive.stage(source, output)
    finally:
        os.close(descriptor)
    assert not list(output.iterdir())
    archive.stage(source, output)


def test_failed_publication_retains_pending_and_releases_locks(fixture_store, monkeypatch):
    _, source, output, raw = fixture_store
    before = archive._identity(source.stat())

    def refuse_rename(*args):
        raise OSError("injected rename failure")

    with monkeypatch.context() as patch:
        patch.setattr(archive.os, "rename", refuse_rename)
        with pytest.raises(OSError, match="injected"):
            archive.stage(source, output)
    assert not list(output.glob("snapshot-*"))
    pending = list(output.glob(".pending-*"))
    assert len(pending) == 1
    archive.verify(pending[0])
    assert archive._identity(source.stat()) == before
    assert source.read_bytes() == raw
    archive.stage(source, output)
    assert pending[0].exists()


def test_cold_exact_boundary_refused(fixture_store, monkeypatch):
    _, source, output, _ = fixture_store
    monkeypatch.setattr(archive.time, "time_ns", lambda: source.stat().st_mtime_ns + 900_000_000_000)
    with pytest.raises(archive.PolicyError, match="cold"):
        archive.stage(source, output)
    assert not list(output.iterdir())


def test_body_blob_strict_boundary(fixture_store):
    _, source, output, _ = fixture_store
    overhead = len(archive._encode({"content": ""}))
    source.write_bytes(archive._encode({"messages": [
        {"content": "x" * (archive.BLOB_BYTES - overhead)},
        {"content": "x" * (archive.BLOB_BYTES - overhead + 1)},
    ]}))
    os.utime(source, (time.time() - 1000,) * 2)
    snapshot = archive.stage(source, output)
    records = json.loads((snapshot / "head.json").read_bytes())["messages"]
    assert "message" in records[0]
    assert "blob" in records[1]
    assert len(records[1]["preview"].encode()) <= 4096


def test_cli_explicit_modes_and_readonly_scan(tmp_path, capsys):
    with pytest.raises(SystemExit):
        archive.main([])
    with pytest.raises(SystemExit):
        archive.main(["--stage", str(tmp_path / "absent.json")])
    archive.main(["--dry-run", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)
    assert report == {"dry_run": True, "total_bytes": 0, "store_alarm": False, "sessions": []}
    assert not list(tmp_path.iterdir())


def test_unknown_cgroup_refused_without_artifacts(fixture_store, monkeypatch):
    _, source, output, _ = fixture_store

    def unreadable():
        raise OSError("cannot read cgroup")

    monkeypatch.setattr(archive, "_cgroup_text", unreadable)
    with pytest.raises(OSError):
        archive.stage(source, output)
    assert not list(output.iterdir())


def test_manifest_path_escape_refused(fixture_store):
    _, source, output, _ = fixture_store
    snapshot = archive.stage(source, output)
    manifest = json.loads((snapshot / "MANIFEST.json").read_bytes())
    manifest["artifacts"]["../escape.json"] = {"sha256": "", "bytes": 0}
    (snapshot / "MANIFEST.json").write_bytes(archive._encode(manifest))
    with pytest.raises(archive.PolicyError, match="unsafe artifact"):
        archive.verify(snapshot)
