'use strict';

const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { spawnSync } = require('node:child_process');

const config = require('../packages/config.cjs');
const logger = path.join(__dirname, 'second-brain-log.cjs');
function localDate(timestamp) {
  const value = new Date(timestamp);
  if (!Number.isFinite(value.getTime())) throw new Error('invalid_transcript_timestamp');
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(value);
}

async function recoverDay({ transcript, date, apply = false }) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error('invalid_date');
  const root = fs.realpathSync(path.join(config.getCodexHome(), 'sessions'));
  const resolved = fs.realpathSync(transcript);
  const relative = path.relative(root, resolved);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative) || path.extname(resolved) !== '.jsonl') {
    throw new Error('transcript_outside_sessions');
  }
  let session = '';
  let turn = '';
  let submitted = false;
  let candidate = null;
  let count = 0;
  let users = 0;
  let finals = 0;
  let malformed = 0;
  const events = [];
  const seen = new Set();
  for await (const line of readline.createInterface({ input: fs.createReadStream(resolved), crlfDelay: Infinity })) {
    if (!line.trim()) continue;
    let record;
    try { record = JSON.parse(line); } catch { malformed++; continue; }
    const payload = record.payload || {};
    if (record.type === 'session_meta') session = payload.id || '';
    if (record.type === 'event_msg' && payload.type === 'task_started') {
      turn = payload.turn_id || '';
      submitted = false;
      candidate = null;
    }
    if (record.type === 'turn_context') turn = payload.turn_id || turn;
    if (!record.timestamp || localDate(record.timestamp) !== date) continue;
    let event;
    let text;
    if ((record.type === 'event_msg' && payload.type === 'user_message') ||
        (record.type === 'response_item' && payload.type === 'message' && payload.role === 'user')) {
      event = 'userPromptSubmit';
      text = record.type === 'event_msg' ? payload.message :
        (payload.content || []).filter(part => part.type === 'input_text').map(part => part.text || '').join(' ');
      if (/^\s*(?:<environment_context>|# AGENTS\.md instructions|<INSTRUCTIONS>)/.test(text || '')) continue;
      turn = turn || `recovery-${record.timestamp}`;
      submitted = true;
    } else if (submitted && record.type === 'response_item' && payload.type === 'message' &&
               payload.role === 'assistant' && ['final_answer', 'final'].includes(payload.phase)) {
      candidate = { text: (payload.content || []).filter(part => ['output_text', 'text'].includes(part.type)).map(part => part.text || '').join(' '), timestamp: record.timestamp };
      continue;
    } else if (submitted && candidate && record.type === 'event_msg' && payload.type === 'task_complete' && payload.turn_id === turn) {
      event = 'stop';
      text = candidate.text;
      submitted = false;
    }
    if (!event || typeof text !== 'string' || !text.trim()) continue;
    if (!session || !turn) throw new Error('missing_transcript_identity');
    const identity = `${event}\n${session}\n${turn}\n${text}`;
    if (seen.has(identity)) continue;
    seen.add(identity);
    if (event === 'stop') finals++; else users++;
    events.push({ event, data: { session_id: session, turn_id: turn, original_timestamp: event === 'stop' ? candidate.timestamp : record.timestamp,
      ...(event === 'stop' ? { last_assistant_message: text } : { prompt: text }) } });
  }
  if (malformed) throw new Error('malformed_transcript_no_apply');
  for (const { event, data } of events) {
    if (apply) {
      const result = spawnSync(process.execPath, [logger, '--event', event, '--recovery'], {
        input: JSON.stringify(data), encoding: 'utf8', timeout: 5000, windowsHide: true,
      });
      if (result.status !== 0 || result.stderr) throw new Error('logger_recovery_failed');
    }
    count++;
  }
  return { schema: 'codex-day-recovery-v1', date, apply, users, finals, events: count,
    malformed, source: relative, pending_final: submitted, full_day_coverage: false };
}

module.exports = { recoverDay };
if (require.main === module) {
  const get = name => {
    const index = process.argv.indexOf(name);
    return index >= 0 ? process.argv[index + 1] : undefined;
  };
  recoverDay({ transcript: get('--transcript'), date: get('--date'), apply: process.argv.includes('--apply') })
    .then(result => process.stdout.write(JSON.stringify(result) + '\n'))
    .catch(error => { process.stderr.write(`CODEX_DAY_RECOVERY_ERROR:${error.message}\n`); process.exitCode = 1; });
}
