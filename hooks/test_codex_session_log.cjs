const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const logger = path.join(__dirname, 'second-brain-log.cjs');
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'second-brain-log-'));
  const logs = path.join(root, 'logs');
  const sessions = path.join(root, 'sessions');
  fs.mkdirSync(sessions);
  return {
    root, logs, sessions,
    run(event, input) {
      return spawnSync(process.execPath, [logger, '--event', event], {
        input: typeof input === 'string' ? input : JSON.stringify(input),
        env: { ...process.env, CODEX_HOME: root, CODEX_SECOND_BRAIN_LOG_DIR: logs },
        encoding: 'utf8', timeout: 5000, windowsHide: true,
      });
    },
    read() {
      if (!fs.existsSync(logs)) return '';
      return fs.readdirSync(logs).map(name => fs.readFileSync(path.join(logs, name), 'utf8')).join('');
    },
  };
}

test('same content in distinct real turns is retained, duplicate deliveries are not', () => {
  const f = fixture();
  const input = { thread_id: 'thread-1', turn_id: 'turn-1', prompt: '重复问题用于追问' };
  assert.equal(f.run('userPromptSubmit', input).status, 0);
  assert.equal(f.run('userPromptSubmit', input).status, 0);
  assert.equal(f.run('userPromptSubmit', { ...input, turn_id: 'turn-2' }).status, 0);
  assert.equal((f.read().match(/\*\*用户\*\*/g) || []).length, 2);
});

test('transcript fallback cannot label commentary as the final answer', () => {
  const f = fixture();
  const transcript = path.join(f.sessions, 'turn.jsonl');
  fs.writeFileSync(transcript, JSON.stringify({ type: 'response_item', payload: {
    type: 'message', role: 'assistant', phase: 'commentary', content: [{ type: 'output_text', text: '工作进行中' }],
  } }) + '\n');
  assert.equal(f.run('stop', { thread_id: 'thread-1', turn_id: 'turn-1', transcript_path: transcript }).status, 0);
  assert.equal(f.read().includes('工作进行中'), false);
});

test('UTF-8 paired logging redacts synthetic credentials and deduplicates final delivery', () => {
  const f = fixture();
  const ids = { thread_id: 'thread-2', turn_id: 'turn-1' };
  const fake = 'sk-' + 'testonly'.repeat(4);
  assert.equal(f.run('userPromptSubmit', { ...ids, prompt: `中文测试 api_key=${fake}` }).status, 0);
  for (let i = 0; i < 2; i++) assert.equal(f.run('stop', { ...ids, last_assistant_message: '最终回答完成' }).status, 0);
  assert.match(f.read(), /中文测试/);
  assert.equal(f.read().includes(fake), false);
  assert.equal((f.read().match(/\*\*Codex\*\*/g) || []).length, 1);
});

test('transcript fallback never reuses a prior turn final answer', () => {
  const f = fixture();
  const transcript = path.join(f.sessions, 'turn.jsonl');
  const records = [
    { type: 'response_item', payload: { type: 'message', role: 'assistant', phase: 'final_answer', content: [{ type: 'output_text', text: '旧回合回答' }] } },
    { type: 'event_msg', payload: { type: 'user_message', message: '新的问题' } },
    { type: 'response_item', payload: { type: 'message', role: 'assistant', phase: 'commentary', content: [{ type: 'output_text', text: '正在处理' }] } },
  ];
  fs.writeFileSync(transcript, records.map(record => JSON.stringify(record)).join('\n') + '\n');
  assert.equal(f.run('stop', { thread_id: 'thread-1', turn_id: 'turn-2', transcript_path: transcript }).status, 0);
  assert.equal(f.read().includes('旧回合回答'), false);
});

test('malformed input is observable without blocking the conversation', () => {
  const f = fixture();
  const result = f.run('stop', '{not-json');
  assert.equal(result.status, 0);
  assert.match(result.stderr, /SECOND_BRAIN_LOG_ERROR/);
  assert.equal(result.stderr.includes('not-json'), false);
});

test('Windows hook subprocess forwards stdin and exits', { skip: process.platform !== 'win32' }, () => {
  const f = fixture();
  const result = spawnSync(process.execPath, [logger, '--event', 'userPromptSubmit'], {
    input: JSON.stringify({ thread_id: 'hidden-fixture', turn_id: 'turn-1', prompt: 'hidden-launcher-evidence' }),
    env: { ...process.env, CODEX_HOME: f.root, CODEX_SECOND_BRAIN_LOG_DIR: f.logs },
    encoding: 'utf8', timeout: 5000, windowsHide: true,
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(f.read(), /hidden-launcher-evidence/);
});
