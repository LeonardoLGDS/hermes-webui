"""FIFO-only tests must skip unavailable setup, not hide supported coverage."""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path
import time

import pytest


TESTS = Path(__file__).resolve().parent
TARGETS = [
    ("test_agent_runtime_revision_guard.py",
     "test_read_live_agent_update_rejects_fifo_marker_without_hanging"),
    ("test_rollback_diff_symlink_disclosure.py",
     "test_checkpoint_diff_skips_workspace_fifo_without_hanging"),
]


class SetupReached(RuntimeError):
    pass


def _setup_reached(*_args, **_kwargs):
    raise SetupReached("FIFO setup was reached")


def _load_test(filename, name):
    # Execute the complete checked-in test function, not a copied guard. Avoid
    # unrelated module-level app imports when testing this setup-only boundary.
    path = TESTS / filename
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    node = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == name
    )
    namespace = {"os": os, "pytest": pytest, "Path": Path, "time": time,
                 "_init_checkpoint": _setup_reached}
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


def _call_test(test, tmp_path, monkeypatch):
    fixtures = {"tmp_path": tmp_path, "monkeypatch": monkeypatch}
    test(**{name: fixtures[name] for name in inspect.signature(test).parameters})


@pytest.mark.parametrize(("filename", "name"), TARGETS)
def test_missing_fifo_skips_before_setup(monkeypatch, tmp_path, filename, name):
    monkeypatch.delattr(os, "mkfifo", raising=False)
    test = _load_test(filename, name)
    with pytest.raises(pytest.skip.Exception, match="FIFO creation is unavailable"):
        _call_test(test, tmp_path, monkeypatch)
    assert not list(tmp_path.iterdir()), "unavailable FIFO test created fixture state"


@pytest.mark.parametrize(("filename", "name"), TARGETS)
def test_available_fifo_is_not_blanket_skipped(monkeypatch, tmp_path, filename, name):
    monkeypatch.setattr(os, "mkfifo", _setup_reached, raising=False)
    test = _load_test(filename, name)
    with pytest.raises(SetupReached, match="FIFO setup was reached"):
        _call_test(test, tmp_path, monkeypatch)


def test_portable_runtime_fallback_still_runs_without_fifo(monkeypatch, tmp_path):
    monkeypatch.delattr(os, "mkfifo", raising=False)
    test = _load_test(
        "test_agent_runtime_revision_guard.py",
        "test_read_live_agent_update_windows_fallback_never_opens_marker",
    )
    _call_test(test, tmp_path, monkeypatch)
