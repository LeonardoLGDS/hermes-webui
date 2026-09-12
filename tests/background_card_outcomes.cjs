const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const root = path.resolve(process.argv[2] || path.join(__dirname, '..'));
const ui = fs.readFileSync(path.join(root, 'static/ui.js'), 'utf8');
function sourceFunction(name) {
  const start = ui.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, name);
  const end = ui.indexOf('\nfunction ', start + 1);
  return ui.slice(start, end === -1 ? undefined : end);
}
function sandbox() {
  const rows = [];
  const dock = {
    querySelector(selector) {
      return rows.find(row => selector.includes(`"${row.dataset.runStatusId}"`)) || null;
    },
    appendChild(row) { rows.push(row); },
  };
  const context = vm.createContext({
    window: {}, document: {createElement() {
      const row = {
        dataset: {},
        get tool() { return this._tcData; },
        setAttribute() {}, removeAttribute() {},
        replaceWith(next) { rows[rows.indexOf(row)] = next; },
      };
      return row;
    }}, console,
    setTimeout: () => 1, clearTimeout() {},
    S: {session: {session_id: 'test'}},
    $: () => dock, CSS: {escape: value => value},
    _toolDisplayName: tool => tool.name,
    _decodeToolLabelEntities: value => value,
    _redactToolTargetLabel: value => value,
    esc: value => String(value), li: () => '', toolIcon: () => '',
    _toolCardPreviewText: () => '', _formatToolArgPreview: () => '',
    _isMemorySave: () => false, _isSkillUpdate: () => false,
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'static/session-status.js'), 'utf8'), context);
  for (const name of ['_shortToolLabel', '_toolI18n', '_toolActionKind', '_toolTargetLabel',
    '_toolPathBasename', '_toolReadRangeLabel', '_toolVisibleTargetLabel', '_toolQueryTitle',
    '_toolActionLabelText', 'buildToolCard', 'syncBackgroundToolRunCards']) {
    vm.runInContext(sourceFunction(name), context);
  }
  return {context, rows, api: context.window._sessionStatus};
}
let passed = 0;
let failed = 0;
function test(name, callback) {
  try { callback(); passed++; console.log(`PASS ${name}`); }
  catch (error) { failed++; console.log(`FAIL ${name}: ${error.message.replace(/\s+/g, ' ')}`); }
}
const cases = [
  ['delegation completed', {task_id: 'deleg_0b076c17', status: 'completed'}, 'done', 'delegate_task'],
  ['delegation success alias', {task_id: 'deleg_success', status: 'success'}, 'done', 'delegate_task'],
  ['delegation error', {task_id: 'deleg_error', status: 'error'}, 'failed', 'delegate_task'],
  ['delegation failed', {task_id: 'deleg_failed', status: 'failed'}, 'failed', 'delegate_task'],
  ['delegation interrupted', {task_id: 'deleg_interrupted', status: 'interrupted'}, 'failed', 'delegate_task'],
  ['delegation stalled', {task_id: 'deleg_stalled', status: 'stalled'}, 'failed', 'delegate_task'],
  ['shell zero', {task_id: 'proc_zero', exit_code: 0}, 'done', 'terminal'],
  ['shell two', {task_id: 'proc_7d96aaf242b9', exit_code: 2}, 'failed', 'terminal'],
  ['shell one', {task_id: 'proc_becd2471b927', exit_code: 1}, 'failed', 'terminal'],
  ['legacy unknown outcome', {task_id: 'legacy'}, 'failed', 'terminal'],
  ['legacy null exit', {task_id: 'legacy_null', exit_code: null}, 'failed', 'terminal'],
  ['legacy camel exit', {task_id: 'legacy_camel', exitCode: 0}, 'done', 'terminal'],
  ['nonzero beats success', {task_id: 'proc_conflict', status: 'completed', exit_code: 2}, 'failed', 'terminal'],
  ['explicit kind nonprefixed id', {task_id: 'opaque', task_type: 'delegation', status: 'completed'}, 'done', 'delegate_task'],
  ['completion cannot remain running', {task_id: 'proc_running', status: 'running'}, 'failed', 'terminal'],
];
for (const [name, frame, state, toolName] of cases) {
  test(name, () => {
    const {context, rows, api} = sandbox();
    const item = api.ingestBgTaskComplete({session_id: 'test', ...frame});
    assert.equal(item.state, state);
    context.syncBackgroundToolRunCards([item], 'test');
    assert.equal(rows.length, 1);
    assert.equal(rows[0].tool.name, toolName);
    assert.equal(rows[0].tool.args.status, state === 'done' ? 'completed' : 'failed');
    assert.equal(rows[0].tool.is_error, state === 'failed');
    const label = context._toolActionLabelText(rows[0].tool, {generic: true});
    assert.equal(/delegat/i.test(label), toolName === 'delegate_task');
  });
}
for (const [code, state] of [[0, 'done'], [2, 'failed'], [null, 'failed']]) {
  test(`status snapshot exit ${code}`, () => {
    const {context, rows, api} = sandbox();
    api.ingestBgStatus({session_id: 'test', processes: [
      {id: 'proc_status', title: 'bash check.sh', state, exit_code: code},
    ]});
    const item = api.itemsForSession('test').bg[0];
    assert.equal(item.state, state);
    context.syncBackgroundToolRunCards([item], 'test');
    assert.equal(rows[0].tool.name, 'terminal');
    assert.equal(rows[0].tool.args.command, 'bash check.sh');
  });
}
test('title metadata and shell replacement identity', () => {
  const {context, rows, api} = sandbox();
  api.ingestBgStatus({session_id: 'test', processes: [
    {id: 'proc_title', title: 'bash deploy_gate_stamp.sh', state: 'running'},
  ]});
  context.syncBackgroundToolRunCards(api.itemsForSession('test').bg, 'test');
  const done = api.ingestBgTaskComplete({session_id: 'test', task_id: 'proc_title', exit_code: 2});
  context.syncBackgroundToolRunCards([done], 'test');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].tool.args.command, 'bash deploy_gate_stamp.sh');
  assert.equal(rows[0].tool.args.status, 'failed');
});
test('delegation goal survives completion', () => {
  const {context, rows, api} = sandbox();
  const item = api.ingestBgTaskComplete({session_id: 'test', task_id: 'deleg_title',
    title: 'Review the three services', status: 'completed'});
  context.syncBackgroundToolRunCards([item], 'test');
  assert.equal(rows[0].tool.args.goal, 'Review the three services');
  assert.equal(rows[0].tool.args.status, 'completed');
});
test('missing title is not duplicated id', () => {
  const {context, rows, api} = sandbox();
  const item = api.ingestBgTaskComplete({session_id: 'test', task_id: 'deleg_legacy'});
  context.syncBackgroundToolRunCards([item], 'test');
  assert.notEqual(rows[0].tool.args.goal, 'deleg_legacy');
});
test('known completion metadata remains compatible', () => {
  const {api} = sandbox();
  const item = api.ingestBgTaskComplete({task_id: 'proc_known'}, 'test', {exitCode: 0, title: 'make test'});
  assert.equal(item.state, 'done');
  assert.equal(item.title, 'make test');
});
test('active stream shell insertion and later replacement', () => {
  const {context, rows, api} = sandbox();
  context.S.activeStreamId = 'stream-test';
  context.appendLiveToolCard = tool => {
    const row = context.buildToolCard(tool);
    row.dataset.liveTid = 'live-test';
    rows.push(row);
  };
  api.ingestBgStatus({session_id: 'test', processes: [
    {id: 'proc_live', title: 'bash check.sh', state: 'running'},
  ]});
  context.syncBackgroundToolRunCards(api.itemsForSession('test').bg, 'test');
  assert.equal(rows.length, 1);
  const item = api.ingestBgTaskComplete({session_id: 'test', task_id: 'proc_live', exit_code: 0});
  context.syncBackgroundToolRunCards([item], 'test');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].dataset.liveTid, 'live-test');
  assert.equal(rows[0].tool.args.status, 'completed');
});
test('explicit process kind beats legacy id prefix', () => {
  const {context, rows, api} = sandbox();
  const item = api.ingestBgTaskComplete({session_id: 'test', task_id: 'deleg_not_a_delegation',
    task_type: 'process', exit_code: 2});
  context.syncBackgroundToolRunCards([item], 'test');
  assert.equal(rows[0].tool.name, 'terminal');
});
test('different session does not paint current chat', () => {
  const {context, rows, api} = sandbox();
  const item = api.ingestBgTaskComplete({session_id: 'other', task_id: 'proc_other', exit_code: 0});
  context.syncBackgroundToolRunCards([item], 'other');
  assert.equal(rows.length, 0);
});
if (process.argv[3]) {
  const wire = JSON.parse(process.argv[3]);
  test('server payload through reducer and card', () => {
    const {context, rows, api} = sandbox();
    let item;
    if (wire.channel === 'bg_status') {
      api.ingestBgStatus(wire.frame);
      item = api.itemsForSession('test').bg[0];
    } else {
      item = api.ingestBgTaskComplete(wire.frame);
    }
    assert.equal(item.state, wire.state);
    context.syncBackgroundToolRunCards([item], 'test');
    assert.equal(rows.length, 1);
    assert.equal(rows[0].tool.name, wire.tool);
    assert.equal(rows[0].tool.args.status, wire.state === 'done' ? 'completed' : 'failed');
    assert.equal(rows[0].tool.args.goal || rows[0].tool.args.command, wire.title);
  });
}
console.log(`TOTAL ${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
