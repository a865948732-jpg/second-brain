const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const recovery = path.join(__dirname, 'recover_codex_day.cjs');
function fixture(records) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-day-recovery-'));
  fs.mkdirSync(path.join(root, 'sessions'));
  const transcript = path.join(root, 'sessions', 'turn.jsonl');
  fs.writeFileSync(transcript, records.map(record => JSON.stringify(record)).join('\n') + '\n');
  return { root, transcript, logs: path.join(root, 'logs') };
}
function run(f, apply = false) {
  return spawnSync(process.execPath, [recovery, '--transcript', f.transcript, '--date', '2026-10-02', ...(apply ? ['--apply'] : [])], {
    encoding: 'utf8', timeout: 5000, windowsHide: true,
    env: { ...process.env, CODEX_HOME: f.root, CODEX_SECOND_BRAIN_LOG_DIR: f.logs },
  });
}

test('scoped recovery uses real record roles, original Beijing dates, and remains idempotent', () => {
  const at = '2026-10-01T16:01:00Z';
  const f = fixture([
    { type: 'session_meta', payload: { id: 'session-1' } },
    { timestamp: '2026-10-01T15:00:00Z', type: 'event_msg', payload: { type: 'user_message', message: '前一天不回填' } },
    { timestamp: at, type: 'event_msg', payload: { type: 'task_started', turn_id: 'turn-1' } },
    { timestamp: at, type: 'response_item', payload: { type: 'message', role: 'user', content: [{ type: 'input_text', text: '<environment_context>test</environment_context>' }] } },
    { timestamp: at, type: 'response_item', payload: { type: 'message', role: 'user', content: [{ type: 'input_text', text: '真实形状的测试问题' }] } },
    { timestamp: at, type: 'response_item', payload: { type: 'message', role: 'assistant', phase: 'commentary', content: [{ type: 'output_text', text: '过程文本不作为结案' }] } },
    { timestamp: at, type: 'response_item', payload: { type: 'message', role: 'assistant', phase: 'final_answer', content: [{ type: 'output_text', text: '最终回答' }] } },
    { timestamp: at, type: 'event_msg', payload: { type: 'task_complete', turn_id: 'turn-1' } },
  ]);
  const dry = run(f);
  assert.equal(dry.status, 0, dry.stderr);
  assert.equal(JSON.parse(dry.stdout).events, 2);
  assert.equal(fs.existsSync(f.logs), false);
  assert.equal(run(f, true).status, 0);
  const log = path.join(f.logs, '2026-10-02.md');
  const before = fs.readFileSync(log, 'utf8');
  assert.match(before, /\[00:01\]/);
  assert.match(before, /codex-recovered:/);
  assert.equal(before.includes('过程文本'), false);
  assert.equal(before.includes('前一天'), false);
  assert.equal(run(f, true).status, 0);
  assert.equal(fs.readFileSync(log, 'utf8'), before);
});

test('a malformed transcript is rejected before any recovery write', () => {
  const f = fixture([]);
  fs.appendFileSync(f.transcript, '{malformed}\n');
  const result = run(f, true);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /malformed_transcript_no_apply/);
  assert.equal(fs.existsSync(f.logs), false);
});
