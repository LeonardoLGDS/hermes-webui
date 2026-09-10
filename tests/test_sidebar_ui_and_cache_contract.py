"""Hermetic frontend and session-list cache contract checks for R61."""

import json
import os
import subprocess
import time
from pathlib import Path

import api.route_session_list_cache as cache


def _extract_function(source, name, next_name):
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end].strip()


def test_actual_frontend_predicate_renders_repaired_and_hides_empty(tmp_path):
    sessions_js = Path(__file__).resolve().parents[1] / "static" / "sessions.js"
    source = sessions_js.read_text(encoding="utf-8")
    function_source = _extract_function(
        source,
        "_sidebarRowHasVisibleMessages",
        "_partitionSidebarSessionRows",
    )
    script = r"""
const source = process.env.FUNCTION_SOURCE;
const factory = new Function(
  "t",
  "S",
  "_sessionAttentionState",
  "_isSessionEffectivelyStreaming",
  "_isChildSession",
  source + "\nreturn _sidebarRowHasVisibleMessages;"
);
const visible = factory(
  (key) => key,
  {session: null},
  () => null,
  () => false,
  () => false
);
const repaired = {session_id: "repaired", message_count: 3};
const empty = {session_id: "empty", message_count: 0};
if (!visible(repaired, "another-session")) {
  throw new Error("repaired sidebar row is hidden");
}
if (visible(empty, "another-session")) {
  throw new Error("genuine empty sidebar row is visible");
}
console.log(JSON.stringify({repaired: true, empty: false}));
"""
    env = os.environ.copy()
    env["FUNCTION_SOURCE"] = function_source

    result = subprocess.run(
        ["node", "-e", script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    assert json.loads(result.stdout.strip()) == {"repaired": True, "empty": False}


def test_expired_or_source_changed_cache_cannot_mask_repaired_count(monkeypatch):
    key = ("default", False, False, False, False, False, False, True)
    stamp = ("hermetic-r62-source",)
    monkeypatch.setattr(cache, "_session_list_cache_resolved_source_stamp", lambda _key: stamp)
    monkeypatch.setattr(cache, "_session_list_cache_streaming_freeze_marker", lambda: None)
    cache._SESSIONS_CACHE.clear()
    try:
        # Age an old zero payload beyond the normal TTL.
        cache._SESSIONS_CACHE[key] = (
            time.monotonic() - cache._SESSIONS_CACHE_TTL_SECONDS - 1.0,
            stamp,
            {"sessions": [{"session_id": "repaired", "message_count": 0}]},
        )
        payload, fresh = cache._session_list_cache_get(key, allow_stale=False)
        assert payload is None
        assert fresh is False

        cache._session_list_cache_set(
            key,
            {"sessions": [{"session_id": "repaired", "message_count": 4}]},
        )
        payload, fresh = cache._session_list_cache_get(key, allow_stale=False)
        assert fresh is True
        assert payload["sessions"][0]["message_count"] == 4

        # A source change invalidates even a young repaired payload, so normal
        # freshness cannot pin a stale snapshot after a sidecar/index repair.
        cache._SESSIONS_CACHE[key] = (
            time.monotonic(),
            ("older-stamp",),
            payload,
        )
        payload, fresh = cache._session_list_cache_get(key, allow_stale=False)
        assert payload is None
        assert fresh is False
    finally:
        cache._SESSIONS_CACHE.clear()
