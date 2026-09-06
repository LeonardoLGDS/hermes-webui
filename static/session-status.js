/*
 * Session-status registry and reducers.
 *
 * WHY: Leo, 2026-09-02 — port the desktop client's per-session subagent and
 * background-process status semantics. The WebUI's literal "runs N" chip was
 * rejected because it exposed lineage volume, not live work. Plan:
 * /home/ops/vault-mirror/tmp/opus5-status-plan.md (S3; F11 covers idOf).
 *
 * This slice owns browser-memory state, the composer live-status DOM, and the
 * status poll. S5 owns sidebar rendering and S6 owns event-source wiring, so
 * this file deliberately performs no EventSource work.
 */
(function(){
'use strict';

const TERMINAL = new Set(['completed', 'failed', 'interrupted']);
const MAX_STREAM = 24;
const PREVIEW_MAX = 220;
const TOOL_PREVIEW_MAX = 96;
const SUCCESS_LINGER_MS = 4000;
const FAILURE_LINGER_MS = 12000;
const STALLED_DELTA_MS = 20000;

const _subagentsBySession = new Map();
const _bgProcsBySession = new Map();
const _dismissedBySession = new Map();
const _bgActiveBySession = new Map();
const _autoDismissTimers = new Map();
const _sessionRowsById = new Map();
// WHY: disclosure state belongs to its mounted DOM, not a session cache that outlives it.
let _runtimeState = {};
let _statusPollTimer = null;
let _statusPollContext = null;

function str(value) {
  return typeof value === 'string' ? value : '';
}

function num(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function compact(text, max) {
  const limit = typeof max === 'number' && Number.isFinite(max) ? max : PREVIEW_MAX;
  const line = String(text || '').replace(/\s+/g, ' ').trim();
  if (!line) return '';
  return line.length > limit ? line.slice(0, Math.max(0, limit - 1)) + '…' : line;
}

function asStatus(value, terminalEvent) {
  if (value === 'completed' || value === 'failed' || value === 'interrupted') {
    return value;
  }
  if (value === 'timeout' || value === 'error') return 'failed';
  if (value === 'cancelled' || value === 'canceled') return 'interrupted';

  // Desktop parity: a terminal frame with an unrecognized status must fail
  // closed rather than leave a row spinning forever.
  if (terminalEvent) return 'failed';
  return value === 'queued' ? value : 'running';
}

function toolLabel(name) {
  const text = str(name);
  return text.split('_').filter(Boolean).map(part => {
    return part ? part[0].toUpperCase() + part.slice(1) : part;
  }).join(' ') || text;
}

function formatTool(name, preview) {
  const label = toolLabel(name);
  const snippet = compact(preview, TOOL_PREVIEW_MAX);
  return snippet ? label + '("' + snippet + '")' : label;
}

function idOf(sub, parent) {
  const explicit = str(sub.subagent_id);
  if (explicit) return explicit;
  const parentId = str(sub.parent_id) || str(parent) || 'root';
  const index = num(sub.task_index);
  return parentId + ':' + (index === undefined ? 0 : index) + ':' + str(sub.goal);
}

function subagentMapFor(sid) {
  let items = _subagentsBySession.get(sid);
  if (!items) {
    items = new Map();
    _subagentsBySession.set(sid, items);
  }
  return items;
}

function delegateArgs(tc) {
  const args = tc && tc.args;
  return args && typeof args === 'object' && !Array.isArray(args) ? args : {};
}

function appendStream(stream, entry) {
  const next = stream.concat([entry]);
  return next.length > MAX_STREAM ? next.slice(next.length - MAX_STREAM) : next;
}

function dotRecord(state, claimedBy) {
  return {
    state,
    bucket: sessionStatusBucket(state),
    claimedBy,
  };
}

function showsRunningArc(state) {
  return state === 'stalled' || state === 'working';
}

function sessionStatusBucket(state) {
  if (state === 'stalled' || state === 'background') return 'working';
  if (state === 'draft' || state === 'idle' || state === 'needs-input'
    || state === 'unread' || state === 'working') {
    return state;
  }
  return 'idle';
}

function sessionStatusRank(state) {
  const ranks = {
    'needs-input': 0,
    working: 1,
    unread: 2,
    draft: 3,
    idle: 4,
  };
  return ranks[sessionStatusBucket(state)];
}

function lineageKeyForRow(row, explicitKey) {
  const explicit = str(explicitKey);
  if (explicit) return explicit;

  // sessions.js owns this identity (including its fork special case). It is a
  // function declaration in another classic script, so it is safely reachable
  // here without adding a DOM or sessions.js dependency.
  if (typeof _sidebarLineageKeyForRow === 'function') {
    return str(_sidebarLineageKeyForRow(row));
  }
  return str(row._lineage_key || row._lineage_root_id || row.lineage_root_id
    || row.parent_session_id || explicitKey || row.session_id);
}

function keysForRow(row) {
  const keys = [str(row.session_id)];
  const lineageKey = str(row.lineageKey);
  if (lineageKey && keys.indexOf(lineageKey) === -1) keys.push(lineageKey);
  return keys;
}

function runtimeState() {
  return _runtimeState;
}

function activeSessionId() {
  const runtime = runtimeState();
  const session = runtime && runtime.session;
  return str(session && session.session_id);
}

function hasUnread(row) {
  if (row.unread === true || row._completion_unread === true) return true;

  // sessions.js resolves unread against its persisted viewed-count cache. This
  // pure reducer accepts the resolved values as row fields instead of reading
  // browser storage or mutating that cache while merely computing a dot.
  const messageCount = num(row.message_count);
  const viewedCount = num(
    row.viewed_message_count !== undefined ? row.viewed_message_count : row.last_read_message_count
  );
  return messageCount !== undefined && viewedCount !== undefined && messageCount > viewedCount;
}

function hasBackgroundWork(row) {
  const sid = str(row.session_id);
  const bgState = window._sessionBgState && window._sessionBgState[sid];
  const subagents = _subagentsBySession.get(sid);
  const delegating = !!subagents && Array.from(subagents.values())
    .some(item => !TERMINAL.has(item.status));
  return Boolean(
    row.bg_active
    || _bgActiveBySession.get(sid) === true
    || (bgState && bgState.active)
    || delegating
  );
}

function isWorking(row, isActive) {
  return Boolean(
    row.is_streaming
    || row.active_stream_id
    || row.pending_user_message
    || row.has_pending_user_message
    || row.pending_started_at
    || row.cron_running
    || (isActive && Boolean(runtimeState().busy))
  );
}

function isStalled(row, isActive) {
  if (!isActive) return false;
  const lastDelta = num(
    row.last_delta_at !== undefined ? row.last_delta_at
      : (row.lastDeltaAt !== undefined ? row.lastDeltaAt
        : (row.updatedAt !== undefined ? row.updatedAt
          : (row.updated_at !== undefined ? row.updated_at
            : (row.last_seen_at !== undefined ? row.last_seen_at : row.lastSeenAt))))
  );
  return lastDelta !== undefined && Date.now() - lastDelta > STALLED_DELTA_MS;
}

function _recomputeSessionDotStates() {
  const next = Object.create(null);
  const allKeys = new Set();
  const claims = {
    draft: [],
    unread: [],
    background: [],
    working: [],
    stalled: [],
    'needs-input': [],
  };
  const activeSid = activeSessionId();

  for (const row of _sessionRowsById.values()) {
    const keys = keysForRow(row);
    keys.forEach(key => allKeys.add(key));

    // Draft is deliberately gated on field presence: absent message_count means
    // unknown, not zero, so this tier is skipped rather than inferred.
    if (Object.prototype.hasOwnProperty.call(row, 'message_count')
      && row.message_count === 0) {
      claims.draft.push(keys);
    }
    if (hasUnread(row)) claims.unread.push(keys);
    if (hasBackgroundWork(row)) claims.background.push(keys);
    if (isWorking(row, str(row.session_id) === activeSid)) claims.working.push(keys);
    if (isStalled(row, str(row.session_id) === activeSid)) claims.stalled.push(keys);
    if (row.attention) claims['needs-input'].push(keys);
  }

  for (const key of allKeys) next[key] = dotRecord('idle', null);
  for (const state of ['draft', 'unread', 'background', 'working']) {
    for (const keys of claims[state]) {
      keys.forEach(key => { next[key] = dotRecord(state, state); });
    }
  }

  // Stalled is a refinement, never an authority: it can only replace a key
  // already claimed as working, so a stale delta cannot invent live work.
  for (const keys of claims.stalled) {
    keys.forEach(key => {
      if (next[key].state === 'working') next[key] = dotRecord('stalled', 'stalled');
    });
  }
  for (const keys of claims['needs-input']) {
    keys.forEach(key => { next[key] = dotRecord('needs-input', 'needs-input'); });
  }

  window._sessionDotState = next;
  return next;
}

/**
 * Merge a session-list or live-frame field update into this module's compute
 * inputs, then refresh the public dot snapshot. S5 callers pass the row's
 * lineage tip explicitly when known; otherwise the sidebar lineage helper or
 * canonical lineage fields resolve it. Internal updates pass undefined so an
 * already-known tip survives. This hook only writes module-owned compute state
 * and the dot map — it never touches DOM or sidebar-owned state.
 */
function noteSessionRowUpdated(session_id, lineageKey, fields) {
  const sid = str(session_id);
  if (!sid) return false;
  if (arguments.length >= 3
    && (!fields || typeof fields !== 'object' || Array.isArray(fields))) {
    return false;
  }

  let row = _sessionRowsById.get(sid);
  if (!row) {
    row = Object.create(null);
    row.session_id = sid;
    _sessionRowsById.set(sid, row);
  }

  const source = fields || {};
  for (const key of Object.keys(source)) {
    if (key === 'session_id' || key === 'lineageKey') continue;
    row[key] = source[key];
  }
  row.lineageKey = arguments.length >= 2 && lineageKey !== undefined
    ? lineageKeyForRow(row, lineageKey)
    : str(row.lineageKey || lineageKeyForRow(row));

  _recomputeSessionDotStates();
  return true;
}

/**
 * Replace the sidebar row snapshot, then recompute the public dot map once.
 * Sidebar payloads are authoritative lists: pruning IDs here prevents a removed
 * row from retaining a live status forever. The optional runtime context lets
 * sessions.js supply the active pane's busy state without inventing another
 * window global.
 */
function noteSessionRowsUpdated(rows, runtime) {
  if (!Array.isArray(rows)) return false;
  if (runtime !== undefined
    && (!runtime || typeof runtime !== 'object' || Array.isArray(runtime))) {
    return false;
  }

  let nextRuntime = _runtimeState;
  if (runtime !== undefined) {
    const session = runtime.session && typeof runtime.session === 'object'
      ? runtime.session
      : null;
    nextRuntime = {session, busy: Boolean(runtime.busy)};
  }

  const nextRows = new Map();
  try {
    for (const source of rows) {
      if (!source || typeof source !== 'object' || Array.isArray(source)) continue;
      const sid = str(source.session_id);
      if (!sid) continue;

      const row = Object.create(null);
      for (const key of Object.keys(source)) row[key] = source[key];
      row.session_id = sid;
      row.lineageKey = lineageKeyForRow(row);
      nextRows.set(sid, row);
    }
  } catch (_) {
    // A malformed row or lineage helper must not leave a half-replaced snapshot.
    return false;
  }

  _runtimeState = nextRuntime;
  _sessionRowsById.clear();
  for (const [sid, row] of nextRows) _sessionRowsById.set(sid, row);
  _recomputeSessionDotStates();
  return true;
}

/**
 * Drop every browser-owned status entry for a deleted session.
 *
 * The sidebar row snapshot is normally replaced by the next list response, but
 * event-derived registries have no such authoritative list and must be pruned
 * at the deletion lifecycle boundary. Timers are cancelled first so a staged
 * terminal auto-dismiss cannot recreate a map entry after this cleanup.
 */
function pruneSession(session_id) {
  const sessionId = str(session_id);
  if (!sessionId) return false;

  const timers = _autoDismissTimers.get(sessionId);
  if (timers) {
    for (const itemId of Array.from(timers.keys())) cancelAutoDismiss(sessionId, itemId);
    _autoDismissTimers.delete(sessionId);
  }
  if (_statusPollContext && str(_statusPollContext.sessionId) === sessionId) {
    // WHY: pruning a session must release its mounted disclosure as well as its timers.
    const stack = statusStackFor(_statusPollContext.rootEl);
    if (stack && typeof stack.remove === 'function') stack.remove();
    _statusPollContext = null;
    stopStatusPoll();
  }

  _sessionRowsById.delete(sessionId);
  _subagentsBySession.delete(sessionId);
  _bgProcsBySession.delete(sessionId);
  _dismissedBySession.delete(sessionId);
  _bgActiveBySession.delete(sessionId);
  if (window._sessionDotState) delete window._sessionDotState[sessionId];
  _recomputeSessionDotStates();
  return true;
}

function ingestSubagentFrame(tc, sid) {
  const name = str(tc && tc.name).toLowerCase();
  if (name !== 'subagent_progress' && name !== 'delegate_task') return null;

  const sessionId = str(sid);
  if (!sessionId) return null;

  const args = delegateArgs(tc);
  const payload = {
    subagent_id: str(args.subagent_id) || str(args.delegation_id),
    parent_id: str(args.parent_id),
    goal: str(args.goal),
    status: str(args.status),
    task_index: num(args.task_index),
    task_count: num(args.task_count),
    current_tool: str(args.current_tool) || str(args.tool),
    tool_preview: str(args.tool_preview) || str(args.current_tool_preview),
    summary: str(args.summary),
    session_id: str(args.session_id) || str(args.child_session_id),
  };
  const id = idOf(payload, sessionId);
  const items = subagentMapFor(sessionId);
  const previous = items.get(id);
  const now = Date.now();
  const terminal = tc.done !== false;
  // A webui tool frame has no separate status field in the common case. Its
  // done/is_error progression is authoritative: live→running, done→completed.
  const statusSource = payload.status || (terminal ? 'completed' : 'running');
  const status = tc.is_error
    ? 'failed'
    : asStatus(statusSource, terminal);

  let stream = previous ? previous.stream.slice() : [];
  const preview = compact(tc.preview);
  if (preview) {
    stream = appendStream(stream, {
      at: now,
      isError: tc.is_error === true,
      kind: 'progress',
      text: preview,
    });
  }

  const item = {
    id,
    status,
    startedAt: previous ? previous.startedAt : now,
    updatedAt: now,
    stream,
  };
  if (payload.goal) item.goal = payload.goal;
  if (payload.session_id) item.sessionId = payload.session_id;
  if (payload.task_index !== undefined) item.taskIndex = payload.task_index;
  if (payload.task_count !== undefined) item.taskCount = payload.task_count;
  if (payload.summary) item.summary = payload.summary;

  // The active tool is meaningful only while the child is live; desktop clears
  // it on terminal status. Empty webui args leave the field undefined.
  if (!TERMINAL.has(status) && payload.current_tool) {
    item.currentTool = formatTool(payload.current_tool, payload.tool_preview);
  }

  items.set(id, item);
  noteSessionRowUpdated(sessionId, undefined, {});
  return item;
}

function bgProcessId(process) {
  return str(process && process.id) || str(process.task_id) || str(process.process_id);
}

function asBgState(value) {
  if (value === 'running' || value === 'done' || value === 'failed') return value;
  if (value === 'completed' || value === 'success') return 'done';
  if (value === 'error' || value === 'timeout') return 'failed';

  // bg_status v2 promises running|done|failed. An out-of-contract value is a
  // terminal failure, not a phantom running process.
  return 'failed';
}

function bgExitCode(value) {
  const code = num(value);
  return code === undefined ? undefined : Math.trunc(code);
}

function bgItemFromProcess(process, now) {
  const id = bgProcessId(process);
  if (!id) return null;

  const exitCode = bgExitCode(process && process.exit_code);
  let state = asBgState(process && process.state);
  if (exitCode !== undefined && exitCode !== 0 && state !== 'running') {
    state = 'failed';
  }

  const item = {
    id,
    type: 'background',
    title: str(process.title) || str(process.command) || id,
    state,
    updatedAt: num(process.updated_at) || now,
  };
  if (exitCode !== undefined) item.exitCode = exitCode;
  const startedAt = num(process && process.started_at);
  if (startedAt !== undefined) item.startedAt = startedAt;
  if (str(process && process.output)) item.output = str(process.output);
  return item;
}

function dismissedFor(sid) {
  let ids = _dismissedBySession.get(sid);
  if (!ids) {
    ids = new Set();
    _dismissedBySession.set(sid, ids);
  }
  return ids;
}

function cancelAutoDismiss(sid, id) {
  const timers = _autoDismissTimers.get(sid);
  const timer = timers && timers.get(id);
  if (timer === undefined) return;
  clearTimeout(timer);
  timers.delete(id);
  if (!timers.size) _autoDismissTimers.delete(sid);
}

function scheduleAutoDismiss(sid, id, delay) {
  const dismissed = _dismissedBySession.get(sid);
  if (dismissed && dismissed.has(id)) return;

  let timers = _autoDismissTimers.get(sid);
  if (timers && timers.has(id)) return;
  if (!timers) {
    timers = new Map();
    _autoDismissTimers.set(sid, timers);
  }

  timers.set(id, setTimeout(() => {
    const current = _autoDismissTimers.get(sid);
    if (current) {
      current.delete(id);
      if (!current.size) _autoDismissTimers.delete(sid);
    }
    dismiss(sid, id);
  }, delay));
}

function terminalAutoDismiss(sid, item) {
  if (item.state === 'running') {
    cancelAutoDismiss(sid, item.id);
    return;
  }
  scheduleAutoDismiss(
    sid,
    item.id,
    item.state === 'done' ? SUCCESS_LINGER_MS : FAILURE_LINGER_MS
  );
}

function setActiveFromItems(sid, frameActive) {
  const items = _bgProcsBySession.get(sid);
  const anyRunning = !!items && Array.from(items.values()).some(item => item.state === 'running');
  _bgActiveBySession.set(sid, Boolean(frameActive) || anyRunning);
}

function replaceBgItems(sid, items) {
  const previous = _bgProcsBySession.get(sid) || new Map();
  for (const id of previous.keys()) {
    if (!items.has(id)) cancelAutoDismiss(sid, id);
  }

  for (const item of items.values()) {
    const old = previous.get(item.id);
    if (item.state === 'running') {
      // A genuinely restarted id may return. Do not resurrect a row the user
      // dismissed while that same id was already running.
      if (!old || old.state !== 'running') {
        const dismissed = _dismissedBySession.get(sid);
        if (dismissed) dismissed.delete(item.id);
      }
      cancelAutoDismiss(sid, item.id);
    } else {
      const unchangedTerminal = old && old.state === item.state;
      if (!unchangedTerminal) terminalAutoDismiss(sid, item);
    }
  }

  if (items.size) _bgProcsBySession.set(sid, items);
  else _bgProcsBySession.delete(sid);
}

function ingestBgStatus(data, sid) {
  const sessionId = str(data && data.session_id) || str(sid);
  if (!sessionId) return null;

  const processes = data && data.processes;
  if (Array.isArray(processes)) {
    const now = Date.now();
    const items = new Map();
    for (const process of processes) {
      const item = bgItemFromProcess(process, now);
      if (item) items.set(item.id, item);
    }
    replaceBgItems(sessionId, items);
  }

  // Older servers send only active. Keep the flag separately instead of
  // inventing an unnamed process row.
  setActiveFromItems(sessionId, data && data.active);
  noteSessionRowUpdated(sessionId, undefined, {bg_active: _bgActiveBySession.get(sessionId)});
  return _bgActiveBySession.get(sessionId);
}

function ingestBgTaskComplete(data, sid, completion) {
  const sessionId = str(data && data.session_id) || str(sid);
  const id = str(data && data.task_id) || str(data && data.process_id);
  if (!sessionId || !id) return null;

  const known = completion && typeof completion === 'object' ? completion : {};
  // The trimmed bg_task_complete wire frame omits display metadata; S6 can
  // pass the parsed title/exitCode alongside it. Missing exit information also
  // fails closed as a failure rather than pretending the process succeeded.
  const rawExit = data.exit_code !== undefined
    ? data.exit_code
    : (data.exitCode !== undefined ? data.exitCode : known.exitCode);
  const exitCode = bgExitCode(rawExit);
  const state = exitCode === 0 ? 'done' : 'failed';
  const now = Date.now();

  let items = _bgProcsBySession.get(sessionId);
  if (!items) {
    items = new Map();
    _bgProcsBySession.set(sessionId, items);
  }
  const previous = items.get(id);
  const item = {
    id,
    type: 'background',
    title: str(data.title) || str(data.command) || str(known.title)
      || (previous ? previous.title : '') || id,
    state,
    updatedAt: num(data.completed_at) || now,
  };
  if (exitCode !== undefined) item.exitCode = exitCode;
  if (previous && previous.startedAt !== undefined) item.startedAt = previous.startedAt;
  const startedAt = num(data.started_at);
  if (startedAt !== undefined) item.startedAt = startedAt;
  const output = str(data.output) || str(known.output);
  if (output) item.output = output;

  items.set(id, item);
  terminalAutoDismiss(sessionId, item);
  setActiveFromItems(sessionId, false);
  noteSessionRowUpdated(sessionId, undefined, {bg_active: _bgActiveBySession.get(sessionId)});
  return item;
}

function copySubagent(item) {
  const copy = Object.assign({}, item, {
    stream: item.stream.map(entry => Object.assign({}, entry)),
  });
  return copy;
}

function itemsForSession(sid) {
  const sessionId = str(sid);
  const subagentMap = _subagentsBySession.get(sessionId);
  const bgMap = _bgProcsBySession.get(sessionId);
  const dismissed = _dismissedBySession.get(sessionId);

  return {
    subagents: subagentMap
      ? Array.from(subagentMap.values()).map(copySubagent)
      : [],
    bg: bgMap
      ? Array.from(bgMap.values()).filter(item => !dismissed || !dismissed.has(item.id))
        .map(item => Object.assign({}, item))
      : [],
  };
}

function statusCounts(sid) {
  const items = itemsForSession(str(sid));
  return {
    running: items.subagents.filter(item => item.status === 'running').length,
    bgRunning: items.bg.filter(item => item.state === 'running').length,
  };
}

function dotStates() {
  const current = window._sessionDotState || _recomputeSessionDotStates();
  const snapshot = Object.create(null);
  for (const key of Object.keys(current)) {
    snapshot[key] = Object.assign({}, current[key]);
  }
  return snapshot;
}

function makeStatusElement(tagName, className, text, title) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (text) element.textContent = String(text);
  if (title) element.title = String(title);
  return element;
}

function statusCountsText(counts, items) {
  // WHY: a collapsed line must distinguish failure, waiting, and completion from live work.
  const parts = [];
  if (counts.running > 0) {
    parts.push(counts.running + (counts.running === 1 ? ' agent running' : ' agents running'));
  }
  if (counts.bgRunning > 0) {
    parts.push(counts.bgRunning + (counts.bgRunning === 1 ? ' job running' : ' jobs running'));
  }
  const states = items.subagents.map(item => item.status).concat(items.bg.map(item => item.state));
  for (const [label, matches] of [
    ['queued', ['queued']], ['failed', ['failed']],
    ['interrupted', ['interrupted']], ['completed', ['completed', 'done']],
  ]) {
    const count = states.filter(state => matches.includes(state)).length;
    if (count) parts.push(count + ' ' + label);
  }
  return 'Activity · ' + parts.join(' · ');
}

function statusStackFor(rootEl) {
  if (!rootEl || typeof rootEl.appendChild !== 'function') return null;

  let existing = null;
  if (typeof rootEl.querySelector === 'function') {
    existing = rootEl.querySelector('#composerStatusStack');
  }
  if (!existing) {
    for (const child of Array.from(rootEl.children || [])) {
      if (child && child.id === 'composerStatusStack') {
        existing = child;
        break;
      }
    }
  }
  if (existing) return existing;

  const stack = makeStatusElement('div');
  stack.id = 'composerStatusStack';
  rootEl.appendChild(stack);
  return stack;
}

// WHY: one native disclosure replaces stacked group toggles; preserving its
// summary node keeps keyboard focus and open state stable during polling.
function activityBody(stack, sessionId, summaryText) {
  if (!stack._activity || stack._activity.sessionId !== sessionId) {
    stack.innerHTML = '';
    const details = makeStatusElement('details', 'status-activity');
    const summary = makeStatusElement('summary', 'status-activity-toggle');
    const body = makeStatusElement('div', 'status-activity-body');
    details.appendChild(summary);
    details.appendChild(body);
    stack.appendChild(details);
    stack._activity = {sessionId, details, summary, body};
  }
  stack._activity.summary.textContent = summaryText;
  stack._activity.body.innerHTML = '';
  return stack._activity.body;
}

function statusGlyph(state) {
  if (state === 'queued') return '○';
  if (state === 'failed') return '✕';
  if (state === 'completed') return '✓';
  if (state === 'interrupted') return '⊘';
  return '◉';
}

function appendSubagentRow(group, item) {
  const row = makeStatusElement('div', 'status-row');
  row.dataset.itemType = 'subagent';
  row.dataset.itemState = item.status;
  row.appendChild(makeStatusElement('span', 'status-row-glyph', statusGlyph(item.status)));
  // WHY: the combined list needs an explicit kind/state, not an unexplained glyph.
  row.appendChild(makeStatusElement('span', 'status-row-kind', 'Agent · ' + item.status));

  const preview = item.goal
    || (item.stream.length ? item.stream[item.stream.length - 1].text : '');
  // WHY: transport payloads are not task titles; retain the raw preview only in the tooltip.
  let label = preview || 'Subagent';
  if (/^\s*[\[{]/.test(label)) {
    label = 'Subagent update';
    try {
      const payload = JSON.parse(preview);
      if (typeof payload.goal === 'string') label = payload.goal;
      else if (Array.isArray(payload.goals)) label = payload.goals.filter(goal => typeof goal === 'string').join(' · ') || label;
    } catch (_) { /* WHY: malformed diagnostic payloads must not break live status rendering. */ }
  }
  const title = makeStatusElement('span', 'status-row-title', label);
  if (preview) title.title = preview;
  row.appendChild(title);
  if (item.currentTool) {
    row.appendChild(makeStatusElement('span', 'status-row-tool', item.currentTool));
  }

  const sessionId = str(item.sessionId);
  if (sessionId) {
    const action = makeStatusElement('button', 'status-row-action', 'Open');
    action.type = 'button';
    action.onclick = () => {
      try {
        if (typeof window.openSession === 'function') window.openSession(sessionId);
      } catch (_) {
        // Opening a child session is best-effort; status rendering must remain live.
      }
    };
    row.appendChild(action);
  }
  group.appendChild(row);
}

function appendBackgroundRow(group, item) {
  const row = makeStatusElement('div', 'status-row');
  row.dataset.itemType = 'background';
  row.dataset.itemState = item.state;
  // WHY: jobs need the same readable lifecycle context as agents in the combined list.
  row.appendChild(makeStatusElement('span', 'status-row-kind', 'Job · ' + item.state));
  const title = makeStatusElement('span', 'status-row-title', item.title || item.id);
  if (item.title) title.title = item.title;
  row.appendChild(title);
  if (item.state === 'failed') {
    const code = item.exitCode === undefined ? '?' : item.exitCode;
    row.appendChild(makeStatusElement('span', 'status-row-exit', 'exit ' + code));
  }
  group.appendChild(row);
}

function hasLiveStatusWork(sessionId) {
  const counts = statusCounts(sessionId);
  if (counts.running > 0 || counts.bgRunning > 0) return true;
  return itemsForSession(sessionId).subagents.some(item => item.status === 'queued');
}

function renderStatusStack(rootEl, sessionId) {
  const sid = str(sessionId);
  const counts = statusCounts(sid);
  const stack = statusStackFor(rootEl);
  if (!sid || !stack || rootEl.isConnected === false) {
    if (stack && typeof stack.remove === 'function') stack.remove();
    _statusPollContext = null;
    if (_statusPollTimer !== null) stopStatusPoll();
    return counts;
  }

  const items = itemsForSession(sid);
  // WHY: successful agent history belongs in the transcript, not permanent composer
  // chrome. Filter only this projection; registry, failures and interrupted work survive.
  items.subagents = items.subagents.filter(item => item.status !== 'completed');
  if (!items.subagents.length && !items.bg.length) {
    if (typeof stack.remove === 'function') stack.remove();
    _statusPollContext = null;
    if (_statusPollTimer !== null) stopStatusPoll();
    return counts;
  }

  // WHY: show one outcome-aware line by default; retain all details and Open actions on demand.
  const body = activityBody(stack, sid, statusCountsText(counts, items));
  for (const item of items.subagents) appendSubagentRow(body, item);
  for (const item of items.bg) appendBackgroundRow(body, item);

  _statusPollContext = {rootEl, sessionId: sid};
  if (_statusPollTimer !== null && !hasLiveStatusWork(sid)) stopStatusPoll();
  return counts;
}

function startStatusPoll() {
  if (_statusPollTimer !== null) return _statusPollTimer;
  if (!_statusPollContext || !hasLiveStatusWork(_statusPollContext.sessionId)) return null;

  _statusPollTimer = setInterval(() => {
    const context = _statusPollContext;
    if (!context || !hasLiveStatusWork(context.sessionId)) {
      if (context) renderStatusStack(context.rootEl, context.sessionId);
      else stopStatusPoll();
      return;
    }
    renderStatusStack(context.rootEl, context.sessionId);
  }, 5000);
  return _statusPollTimer;
}

function stopStatusPoll() {
  const timer = _statusPollTimer;
  _statusPollTimer = null;
  if (timer !== null) clearInterval(timer);
  return Boolean(timer);
}

function dismiss(sid, id) {
  const sessionId = str(sid);
  const itemId = str(id);
  if (!sessionId || !itemId) return false;

  const subagents = _subagentsBySession.get(sessionId);
  const bgProcesses = _bgProcsBySession.get(sessionId);
  const isSubagent = !!subagents && subagents.has(itemId);
  const isBackground = !!bgProcesses && bgProcesses.has(itemId);
  if (!isSubagent && !isBackground) return false;

  cancelAutoDismiss(sessionId, itemId);
  if (isBackground) dismissedFor(sessionId).add(itemId);

  // Background registry entries stay present but dismissed so a registry
  // refresh cannot resurrect a finished row. A subagent row has no equivalent
  // refresh source in this slice, so its registry entry is removed directly.
  if (isSubagent) subagents.delete(itemId);
  noteSessionRowUpdated(sessionId, undefined, {});
  return true;
}

_recomputeSessionDotStates();

/**
 * Todo ingestion is intentionally a stub for the S3 registry slice. S7 will
 * define the webui todo_state shape and reducer; callers may already retain the
 * API hook without changing behavior.
 */
function ingestTodoState() {
  return undefined;
}

window._sessionStatus = {
  ingestSubagentFrame,
  ingestBgStatus,
  ingestBgTaskComplete,
  ingestTodoState,
  itemsForSession,
  dismiss,
  statusCounts,
  renderStatusStack,
  startStatusPoll,
  stopStatusPoll,
  _recomputeSessionDotStates,
  noteSessionRowUpdated,
  noteSessionRowsUpdated,
  pruneSession,
  showsRunningArc,
  sessionStatusBucket,
  sessionStatusRank,
  dotStates,
};
})();
