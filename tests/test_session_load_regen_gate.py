"""The load-path regeneration revision must skip unchanged recomputation.

GET /api/session recomputed the regeneration revision on every load. The
revision is a full state.db reconcile + append-only merge plus two
whole-transcript SHA-256 passes; on the 2026-09-11 burst it dominated the
metadata-only (``messages=0``) poll that the sidebar/session-switch fires.

The gate is fail-open: it may only reuse a revision while BOTH the sidecar
stat signature and the state.db per-session watermark are provably unchanged.
These tests pin both halves:
  * an unchanged second load does NOT re-run the merge and still returns the
    exact revision the uncached path would produce;
  * a changed sidecar, a moved state.db watermark, an unavailable signature,
    and an active session all fall back to a fresh recompute.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.config as config  # noqa: E402
import api.models as models  # noqa: E402
import api.routes as routes  # noqa: E402
import api.session_ops as session_ops  # noqa: E402

# Unique per test: the process-level SESSIONS cache is keyed by session id and
# is shared across tests in one pytest process.
SID = "20260401_120000_regen1"
OTHER_SID = "20260401_120000_regen2"


class _FakeHandler:
    headers = {}

    def _safe_webui_print(self, msg):
        pass


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = tmp_path / "state"
    session_dir = state / "sessions"
    session_dir.mkdir(parents=True)
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(state))
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)

    # The state.db watermark is an input signal whose real movement is covered
    # by tests/test_state_db_session_signature.py; stub it so this file can
    # exercise the gate without provisioning a profile state.db.
    db_sig = {"value": ("db", 1, 2, 3)}
    monkeypatch.setattr(
        routes, "_state_db_session_signature", lambda sid, profile=None: db_sig["value"]
    )

    def fake_j(handler, data, status=200, extra_headers=None):
        handler.last = data
        handler.status = status
        return True

    def fake_bad(handler, msg, status=400, extra_headers=None):
        handler.last = {"error": msg}
        handler.status = status
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", fake_bad)
    _clear_regen_cache()
    yield {"db_sig": db_sig}
    _clear_regen_cache()


def _clear_regen_cache():
    cache = getattr(routes, "_regeneration_revision_cache", None)
    lock = getattr(routes, "_regeneration_revision_cache_lock", None)
    if cache is None or lock is None:
        return
    with lock:
        cache.clear()


def _make_session(sid=SID, *, n_pairs=1):
    s = models.Session(session_id=sid, profile=None)
    s.session_source = "webui"
    now = time.time()
    for i in range(n_pairs):
        s.messages.append(
            {"role": "user", "content": f"question {i}", "timestamp": now + i * 10}
        )
        s.messages.append(
            {"role": "assistant", "content": f"answer {i}", "timestamp": now + i * 10 + 1}
        )
    s.save()
    return s


def _load(sid=SID, *, messages=1):
    handler = _FakeHandler()
    parsed = urlparse(
        f"/api/session?session_id={sid}&messages={messages}&resolve_model=0"
    )
    assert routes._handle_get_impl(handler, parsed) is True
    return handler


def _revision(handler):
    return ((handler.last or {}).get("session") or {}).get("regeneration_revision")


def _fresh_revision(sid):
    s = models.get_session(sid)
    rows, context = session_ops.regeneration_state(s)
    return session_ops.regeneration_revision_for(rows, session=s, context=context)


@pytest.fixture()
def regen_spy(monkeypatch):
    calls = {"n": 0}
    original = session_ops.regeneration_state

    def counting(session):
        calls["n"] += 1
        return original(session)

    monkeypatch.setattr(session_ops, "regeneration_state", counting)
    return calls


def test_second_unchanged_load_skips_the_regen_merge_and_keeps_revision(env, regen_spy):
    _make_session()
    expected = _fresh_revision(SID)
    assert expected, "fixture must produce a real regeneration revision"
    base = regen_spy["n"]

    first = _load()
    assert regen_spy["n"] == base + 1
    assert _revision(first) == expected

    before = regen_spy["n"]
    second = _load()
    assert regen_spy["n"] == before, "unchanged load must not re-run the merge"
    assert _revision(second) == expected


def test_regen_runs_again_after_the_sidecar_changes(env, regen_spy):
    sid = "20260401_120000_regen_sidecar"
    s = _make_session(sid)
    first = _load(sid)
    assert regen_spy["n"] == 1

    s.messages.append({"role": "user", "content": "follow up", "timestamp": time.time()})
    s.messages.append({"role": "assistant", "content": "reply", "timestamp": time.time() + 1})
    s.save()

    before = regen_spy["n"]
    second = _load(sid)
    assert regen_spy["n"] == before + 1, "a changed sidecar must recompute"
    assert _revision(second) != _revision(first)


def test_regen_runs_again_when_the_state_db_watermark_moves(env, regen_spy):
    sid = "20260401_120000_regen_db"
    _make_session(sid)
    _load(sid)
    assert regen_spy["n"] == 1

    _load(sid)
    assert regen_spy["n"] == 1, "unchanged load should have been skipped"

    env["db_sig"]["value"] = ("db", 9, 9, 9)
    _load(sid)
    assert regen_spy["n"] == 2, "moved state.db watermark must recompute"


def test_gate_fails_open_when_state_db_signature_is_unavailable(env, regen_spy, monkeypatch):
    sid = "20260401_120000_regen_nosig"
    _make_session(sid)
    monkeypatch.setattr(
        routes, "_state_db_session_signature", lambda sid, profile=None: None
    )
    _load(sid)
    _load(sid)
    assert regen_spy["n"] == 2, "an unavailable watermark must not be cached"
    assert _revision(_load(sid)) is not None


def test_gate_fails_open_for_an_active_session(env, regen_spy):
    sid = "20260401_120000_regen_pending"
    s = _make_session(sid)
    s.pending_user_message = "queued while busy"
    s.save()
    _load(sid)
    _load(sid)
    assert regen_spy["n"] == 2, "a session with a pending turn must always recompute"


def test_gate_is_scoped_per_session(env, regen_spy):
    _make_session()
    _make_session(OTHER_SID)
    _load(SID)
    _load(OTHER_SID)
    assert regen_spy["n"] == 2, "each session's first load computes once"
    _load(SID)
    _load(OTHER_SID)
    assert regen_spy["n"] == 2, "both unchanged second loads should be skipped"


def test_declined_verdict_is_also_memoized(env, regen_spy):
    """A None verdict (no regenerable turn) is a real, reusable result.

    This is the shape the 2026-09-11 burst sessions took: regeneration_authority
    declines a long/imported transcript, and re-deriving that decline was the
    per-load merge cost.
    """
    sid = "20260401_120000_regen_declined"
    s = models.Session(session_id=sid, profile=None)
    s.session_source = "webui"
    s.messages.append({"role": "user", "content": "never answered", "timestamp": time.time()})
    s.save()

    first = _load(sid)
    assert _revision(first) is None
    assert regen_spy["n"] == 1

    before = regen_spy["n"]
    second = _load(sid)
    assert regen_spy["n"] == before, "a declined verdict must still be memoized"
    assert _revision(second) is None


def test_gate_recomputes_when_ownership_flags_change_without_a_sidecar_write(env, regen_spy):
    """The authority's writability flags are part of the key, not just the file.

    ``raw_source`` is read by _selected_regeneration_turn_owned() to decide the
    session is not WebUI-owned, but it does not change the handler's gate
    condition, so the block still runs and must recompute.
    """
    sid = "20260401_120000_regen_flags"
    _make_session(sid)
    _load(sid)
    assert regen_spy["n"] == 1

    models.get_session(sid).raw_source = "cli"
    _load(sid)
    assert regen_spy["n"] == 2, "an in-memory ownership change must recompute"


def test_missing_sidecar_does_not_serve_a_cached_revision(env, regen_spy):
    sid = "20260401_120000_regen_missing"
    _make_session(sid)
    _load(sid)
    assert regen_spy["n"] == 1

    # Remove the sidecar: the stat signature can no longer be resolved, so the
    # gate must fail open rather than reusing the stale revision.
    (routes.SESSION_DIR / f"{sid}.json").unlink()
    _load(sid)
    assert regen_spy["n"] == 2
