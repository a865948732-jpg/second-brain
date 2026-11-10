// Codex hook: append user prompts and final answers to the Codex session log.
// The hook is fail-open so logging can never block a conversation.

'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const config = require('../packages/config.cjs');
const DEFAULT_LOG_DIR = path.join(config.getVaultRoot(), '05_Review', '会话日志', 'Codex');
const LOG_DIR = process.env.CODEX_SECOND_BRAIN_LOG_DIR || DEFAULT_LOG_DIR;
const MAX_ENTRY_LENGTH = 800;
const TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024;

function readHookInput() {
  const raw = fs.readFileSync(0, 'utf8').trim();
  return raw ? JSON.parse(raw) : {};
}

function parseEventArg() {
  const index = process.argv.indexOf('--event');
  return index >= 0 ? String(process.argv[index + 1] || '') : '';
}

function textFromContent(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return value.map(textFromContent).filter(Boolean).join(' ');
  }
  if (!value || typeof value !== 'object') return '';
  if (typeof value.text === 'string') return value.text;
  if (typeof value.message === 'string') return value.message;
  if (typeof value.input_text === 'string') return value.input_text;
  if (typeof value.output_text === 'string') return value.output_text;
  if (value.content !== undefined) return textFromContent(value.content);
  return '';
}

function firstText(data, keys) {
  for (const key of keys) {
    const value = textFromContent(data[key]);
    if (value.trim()) return value;
  }
  if (data.payload && typeof data.payload === 'object') {
    return firstText(data.payload, keys);
  }
  return '';
}

function transcriptPathFrom(data) {
  return data.transcript_path || data.transcriptPath ||
    (data.payload && (data.payload.transcript_path || data.payload.transcriptPath)) || '';
}

function isAllowedTranscript(filePath) {
  if (!filePath) return false;
  const sessionsRoot = path.resolve(
    config.getCodexHome(),
    'sessions'
  );
  const resolved = path.resolve(filePath);
  const relative = path.relative(sessionsRoot, resolved);
  return relative && !relative.startsWith('..') && !path.isAbsolute(relative) &&
    path.extname(resolved).toLowerCase() === '.jsonl';
}

function readTranscriptTail(filePath) {
  if (!isAllowedTranscript(filePath) || !fs.existsSync(filePath)) return [];
  const stat = fs.statSync(filePath);
  const start = Math.max(0, stat.size - TRANSCRIPT_TAIL_BYTES);
  const length = stat.size - start;
  const buffer = Buffer.alloc(length);
  const fd = fs.openSync(filePath, 'r');
  try {
    fs.readSync(fd, buffer, 0, length, start);
  } finally {
    fs.closeSync(fd);
  }
  const text = buffer.toString('utf8');
  const completeText = start === 0 ? text : text.slice(text.indexOf('\n') + 1);
  return completeText.split(/\r?\n/).filter(Boolean);
}

function assistantTextFromTranscript(filePath) {
  const candidates = [];
  for (const line of readTranscriptTail(filePath)) {
    try {
      const record = JSON.parse(line);
      const payload = record.payload || {};
      if ((record.type === 'event_msg' && payload.type === 'user_message') ||
          (record.type === 'response_item' && payload.type === 'message' && payload.role === 'user')) {
        candidates.length = 0;
      }
      if (record.type === 'event_msg' && payload.type === 'agent_message') {
        candidates.push({ text: textFromContent(payload.message), phase: payload.phase || '' });
      } else if (
        record.type === 'response_item' &&
        payload.type === 'message' &&
        payload.role === 'assistant'
      ) {
        candidates.push({ text: textFromContent(payload.content), phase: payload.phase || '' });
      }
    } catch (_) {
      // Ignore incomplete JSONL lines while Codex is still flushing the transcript.
    }
  }
  for (let index = candidates.length - 1; index >= 0; index -= 1) {
    if (['final_answer', 'final'].includes(candidates[index].phase) && candidates[index].text.trim()) {
      return candidates[index].text;
    }
  }
  return '';
}

function redactSecrets(value) {
  let text = String(value || '');
  const replacements = [
    [/\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b/g, '[REDACTED_KEY]'],
    [/\b(?:ghp|github_pat|glpat|xox[baprs])-[_A-Za-z0-9-]{12,}\b/g, '[REDACTED_TOKEN]'],
    [/\bAKIA[0-9A-Z]{16}\b/g, '[REDACTED_AWS_KEY]'],
    [/\bBearer\s+[A-Za-z0-9._~+\/-]+=*\b/gi, 'Bearer [REDACTED]'],
    [/((?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret|session|cookie)\s*[:=]\s*["']?)[^\s,"';]+/gi, '$1[REDACTED]'],
  ];
  for (const [pattern, replacement] of replacements) {
    text = text.replace(pattern, replacement);
  }
  return text;
}

function compact(value) {
  const text = redactSecrets(value)
    .replace(/\r/g, ' ')
    .replace(/\n/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (!text) return '';
  return text.length > MAX_ENTRY_LENGTH ? `${text.slice(0, MAX_ENTRY_LENGTH)}...` : text;
}

function localDateParts(now) {
  const pad = (number) => String(number).padStart(2, '0');
  return {
    date: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
    time: `${pad(now.getHours())}:${pad(now.getMinutes())}`,
  };
}

function sessionIdFrom(data) {
  return String(
    data.session_id || data.sessionId || data.thread_id || data.threadId ||
    (data.payload && (data.payload.session_id || data.payload.sessionId ||
      data.payload.thread_id || data.payload.threadId)) || ''
  );
}

function turnIdFrom(data) {
  return String(
    data.turn_id || data.turnId ||
    (data.payload && (data.payload.turn_id || data.payload.turnId)) || ''
  );
}

function ensureLogFile(logFile, date) {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  if (fs.existsSync(logFile)) return;
  const header =
    `---\ntitle: "${date} Codex 会话日志"\ntype: session-log\n` +
    `status: raw\ndate: ${date}\ntool: Codex\n---\n\n` +
    `# ${date} Codex 会话日志\n\n` +
    `> Codex 用户输入与最终产出的实时流水；敏感值自动脱敏，每晚再蒸馏进知识卡。\n\n`;
  try {
    fs.writeFileSync(logFile, header, { encoding: 'utf8', flag: 'wx' });
  } catch (error) {
    if (!error || error.code !== 'EEXIST') throw error;
  }
}

function appendEntry(event, data, text) {
  const compacted = compact(text);
  if (!compacted) return;

  const sessionId = sessionIdFrom(data);
  const turnId = turnIdFrom(data);
  const fingerprint = crypto
    .createHash('sha256')
    .update(`${event}\n${sessionId}\n${turnId}\n${compacted}`)
    .digest('hex')
    .slice(0, 16);
  const marker = `<!-- codex-event:${fingerprint} -->`;
  const recovery = process.argv.includes('--recovery');
  const now = recovery ? new Date(data.original_timestamp) : new Date();
  if (!Number.isFinite(now.getTime())) throw new Error('invalid_recovery_timestamp');
  const { date, time } = localDateParts(now);
  const logFile = path.join(LOG_DIR, `${date}.md`);

  ensureLogFile(logFile, date);
  const existing = fs.readFileSync(logFile, 'utf8');
  const label = event === 'userPromptSubmit' ? '用户' : 'Codex';
  if (existing.includes(marker)) return;

  const provenance = recovery ? ` <!-- codex-recovered:${localDateParts(new Date()).date} -->` : '';
  fs.appendFileSync(logFile, `- [${time}] **${label}**：${compacted} ${marker}${provenance}\n`, 'utf8');
}

function main() {
  const event = parseEventArg();
  const data = readHookInput();
  if (event === 'userPromptSubmit') {
    appendEntry(event, data, firstText(data, ['prompt', 'user_prompt', 'userPrompt', 'message', 'input', 'content']));
    return;
  }
  if (event === 'stop') {
    let text = firstText(data, [
      'last_assistant_message',
      'lastAssistantMessage',
      'assistant_message',
      'assistantMessage',
      'response',
      'output',
      'message',
      'content',
    ]);
    if (!text) text = assistantTextFromTranscript(transcriptPathFrom(data));
    appendEntry(event, data, text);
  }
}

try {
  main();
} catch (error) {
  // Fail open: a missing drive, malformed event, or locked file must not block Codex.
  process.stderr.write(`SECOND_BRAIN_LOG_ERROR:${error.code || error.name || 'unknown'}\n`);
}
