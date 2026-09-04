"""Regression coverage for issue #7352 one-shot cron editing."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function


REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


class _JSONHandler:
    def __init__(self):
        self.headers = {}
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cron_schedule_for_edit_round_trips_one_shot_timestamp():
    helper = extract_function(PANELS_JS, "_cronScheduleForEdit")
    edit = extract_function(PANELS_JS, "openCronEdit")
    duplicate = extract_function(PANELS_JS, "duplicateCurrentCron")
    script = helper + """
const once = _cronScheduleForEdit({
  schedule_display: 'once at 2026-08-28 16:00',
  schedule: {kind: 'once', run_at: '2026-08-28T16:00:00-05:00'},
});
const recurring = _cronScheduleForEdit({
  schedule_display: 'every 30m',
  schedule: {kind: 'interval', expression: 'every 30m'},
});
console.log(JSON.stringify({once, recurring}));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    values = json.loads(result.stdout)

    assert values == {
        "once": "2026-08-28T16:00:00-05:00",
        "recurring": "every 30m",
    }
    assert "schedule: _cronScheduleForEdit(job)" in edit
    assert "schedule: _cronScheduleForEdit(job)" in duplicate


def test_cron_update_returns_400_for_invalid_schedule(monkeypatch):
    import api.routes as routes

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")

    def update_job(_job_id, _updates):
        raise ValueError("Invalid schedule 'once at 2026-08-28 16:05'")

    cron_jobs.update_job = update_job
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {"job_id": "job-once", "schedule": "once at 2026-08-28 16:05"},
    )

    assert handler.status == 400
    assert _payload(handler) == {"error": "Invalid schedule 'once at 2026-08-28 16:05'"}
