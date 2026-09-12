import ast
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import types
import uuid

import pytest


ROOT = Path(os.environ.get('BG_CARD_SOURCE', Path(__file__).resolve().parents[1]))


@pytest.fixture
def background_functions(monkeypatch):
    source = ROOT / 'api/background_process.py'
    tree = ast.parse(source.read_text())
    names = {'_build_payload', 'bg_status_for_session'}
    functions = ast.Module(body=[node for node in tree.body
                                if isinstance(node, ast.FunctionDef) and node.name in names],
                           type_ignores=[])
    namespace = {
        'time': time, 'uuid': uuid, 'Any': object,
        'logger': logging.getLogger(__name__),
        'completion_delivery_id': lambda event: event.get('delegation_id') or event.get('session_id'),
        '_truncate': lambda value, limit: value[:limit],
        'format_wakeup_prompt': lambda event: 'Delegation finished',
    }
    exec(compile(functions, str(source), 'exec'), namespace)
    registry = types.SimpleNamespace(rows=[])
    registry.list_sessions = lambda **kwargs: registry.rows
    module = types.ModuleType('tools.process_registry')
    module.process_registry = registry
    monkeypatch.setitem(sys.modules, 'tools.process_registry', module)
    return namespace, registry


@pytest.mark.parametrize('status', ['completed', 'success', 'error', 'interrupted', 'stalled'])
def test_delegation_payload(background_functions, status):
    functions, _ = background_functions
    result = functions['_build_payload']({
        'type': 'async_delegation', 'delegation_id': 'deleg_0b076c17',
        'goal': 'Review three services', 'status': status, 'is_batch': True,
    }, 'test')
    assert result['task_id'] == 'deleg_0b076c17'
    assert result['status'] == status
    assert result['task_type'] == 'delegation'
    assert result['title'] == 'Review three services'
    assert 'exit_code' not in result
    assert result['event_id'] and result['completed_at']


@pytest.mark.parametrize('code', [0, 1, 2, None])
def test_shell_payload(background_functions, code):
    functions, _ = background_functions
    result = functions['_build_payload']({
        'session_id': 'proc_7d96aaf242b9', 'command': 'bash check.sh\nsecond line',
        'exit_code': code,
    }, 'test')
    assert result['exit_code'] == code
    assert result['task_type'] == 'process'
    assert result['title'] == 'bash check.sh'


@pytest.mark.parametrize('status,code,state', [
    ('exited', 0, 'done'), ('exited', 1, 'failed'), ('exited', 2, 'failed'),
    ('exited', None, 'failed'), ('running', None, 'running'),
])
def test_registry_snapshot(background_functions, status, code, state):
    functions, registry = background_functions
    registry.rows = [{'session_id': 'proc_status', 'status': status,
                      'exit_code': code, 'command': 'bash check.sh'}]
    result = functions['bg_status_for_session']('test')
    assert result['processes'][0]['state'] == state
    assert result['processes'][0]['exit_code'] == code
    assert result['active'] == (state == 'running')


def test_metadata_free_payload(background_functions):
    functions, _ = background_functions
    result = functions['_build_payload']({'session_id': 'proc_legacy'}, 'test')
    assert 'title' not in result
    assert result.get('exit_code') is None
    assert result.get('status') not in ('completed', 'success')


@pytest.mark.parametrize('channel,kind,outcome,state', [
    ('bg_task_complete', 'delegation', 'completed', 'done'),
    ('bg_task_complete', 'delegation', 'error', 'failed'),
    ('bg_task_complete', 'process', 0, 'done'),
    ('bg_task_complete', 'process', 2, 'failed'),
    ('bg_task_complete', 'process', None, 'failed'),
    ('bg_status', 'process', 0, 'done'),
    ('bg_status', 'process', 2, 'failed'),
    ('bg_status', 'process', None, 'failed'),
])
def test_wire_to_card(background_functions, channel, kind, outcome, state):
    functions, registry = background_functions
    title = 'Review services' if kind == 'delegation' else 'bash check.sh'
    if channel == 'bg_status':
        registry.rows = [{'session_id': 'proc_wire', 'command': title,
                          'status': 'exited', 'exit_code': outcome}]
        frame = functions['bg_status_for_session']('test')
    else:
        event = {'session_id': 'proc_wire', 'command': title, 'exit_code': outcome}
        if kind == 'delegation':
            event = {'type': 'async_delegation', 'delegation_id': 'deleg_wire',
                     'goal': title, 'status': outcome}
        frame = functions['_build_payload'](event, 'test')
    wire = {'channel': channel, 'frame': frame, 'state': state, 'title': title,
            'tool': 'delegate_task' if kind == 'delegation' else 'terminal'}
    harness = Path(__file__).with_name('background_card_outcomes.cjs')
    result = subprocess.run(['node', str(harness), str(ROOT), json.dumps(wire)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
