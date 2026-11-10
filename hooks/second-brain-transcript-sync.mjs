import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import config from "../packages/config.cjs";

const CODEX_HOME = config.getCodexHome();
const SESSIONS_ROOT = path.resolve(CODEX_HOME, "sessions");
const VAULT_ROOT = config.getVaultRoot();
const LOG_DIR = process.env.CODEX_SECOND_BRAIN_LOG_DIR || path.join(VAULT_ROOT, "05_Review", "会话日志", "Codex");
const STATE_PATH = process.env.CODEX_TRANSCRIPT_SYNC_STATE || path.join(config.repoRoot, ".local", "runtime", "transcript-sync-state.json");
const LOCK_PATH = `${STATE_PATH}.lock`;
const MAX_ENTRY_LENGTH = 800;
const MAX_SCAN_BYTES = 16 * 1024 * 1024;

function redactSecrets(value) {
  let text = String(value || "");
  const replacements = [
    [/\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b/g, "[REDACTED_KEY]"],
    [/\b(?:ghp|github_pat|glpat|xox[baprs])-[_A-Za-z0-9-]{12,}\b/g, "[REDACTED_TOKEN]"],
    [/\bAKIA[0-9A-Z]{16}\b/g, "[REDACTED_AWS_KEY]"],
    [/Bearer\s+[A-Za-z0-9._~+\/-]+=*/gi, "Bearer [REDACTED]"],
    [/((?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret|session|cookie)\s*[:=]\s*["']?)[^\s,"';]+/gi, "$1[REDACTED]"],
  ];
  for (const [pattern, replacement] of replacements) text = text.replace(pattern, replacement);
  return text;
}

function compact(value) {
  const text = redactSecrets(value).replace(/\r/g, " ").replace(/\n/g, " ").replace(/\s+/g, " ").trim();
  if (!text) return "";
  return text.length > MAX_ENTRY_LENGTH ? `${text.slice(0, MAX_ENTRY_LENGTH)}...` : text;
}

function textFrom(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(textFrom).filter(Boolean).join(" ");
  if (!value || typeof value !== "object") return "";
  if (typeof value.text === "string") return value.text;
  if (typeof value.message === "string") return value.message;
  if (typeof value.input_text === "string") return value.input_text;
  if (typeof value.output_text === "string") return value.output_text;
  if (value.content !== undefined) return textFrom(value.content);
  return "";
}

function localDateParts(timestamp) {
  const now = timestamp ? new Date(timestamp) : new Date();
  const pad = (number) => String(number).padStart(2, "0");
  return {
    date: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
    time: `${pad(now.getHours())}:${pad(now.getMinutes())}`,
  };
}

function fingerprint(filePath, lineNumber, event, text) {
  return crypto.createHash("sha256")
    .update(`${filePath}\n${lineNumber}\n${event}\n${text}`, "utf8")
    .digest("hex")
    .slice(0, 16);
}

function ensureLogFile(logFile, date) {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  if (fs.existsSync(logFile)) return;
  const header = `---\ntitle: "${date} Codex 会话日志"\ntype: session-log\nstatus: raw\ndate: ${date}\ntool: Codex\n---\n\n# ${date} Codex 会话日志\n\n> Codex 用户输入与最终产出的增量流水；敏感值自动脱敏，每晚再蒸馏进知识卡。\n\n`;
  try {
    fs.writeFileSync(logFile, header, { encoding: "utf8", flag: "wx" });
  } catch (error) {
    if (error?.code !== "EEXIST") throw error;
  }
}

function appendEntry(filePath, lineNumber, event, record, text) {
  const compacted = compact(text);
  if (!compacted) return;
  const { date, time } = localDateParts(record.timestamp);
  const logFile = path.join(LOG_DIR, `${date}.md`);
  const marker = `<!-- codex-event:${fingerprint(filePath, lineNumber, event, compacted)} -->`;
  ensureLogFile(logFile, date);
  const existing = fs.readFileSync(logFile, "utf8");
  const label = event === "user" ? "用户" : "Codex";
  if (existing.includes(marker) || existing.includes(`**${label}**：${compacted}`)) return;
  fs.appendFileSync(logFile, `- [${time}] **${label}**：${compacted} ${marker}\n`, "utf8");
}

function eventFromRecord(record) {
  const payload = record?.payload || {};
  if (record?.type === "event_msg" && payload.type === "user_message") {
    return ["user", textFrom(payload.message)];
  }
  if (record?.type === "response_item" && payload.type === "message" && payload.role === "user") {
    return ["user", textFrom(payload.content)];
  }
  if (record?.type === "event_msg" && payload.type === "agent_message") {
    return ["assistant", textFrom(payload.message)];
  }
  if (record?.type === "response_item" && payload.type === "message" && payload.role === "assistant") {
    return ["assistant", textFrom(payload.content)];
  }
  return ["", ""];
}

function walkJsonl(directory, result = []) {
  if (!fs.existsSync(directory)) return result;
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) walkJsonl(fullPath, result);
    else if (entry.isFile() && entry.name.endsWith(".jsonl")) result.push(fullPath);
  }
  return result;
}

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, "utf8"));
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
    return { version: 1, initialized: false, files: {} };
  }
}

function saveState(state) {
  const tempPath = `${STATE_PATH}.tmp-${process.pid}`;
  fs.writeFileSync(tempPath, JSON.stringify(state, null, 2), "utf8");
  fs.renameSync(tempPath, STATE_PATH);
}

function acquireLock() {
  try {
    fs.writeFileSync(LOCK_PATH, `${process.pid}\n`, { flag: "wx" });
    return true;
  } catch (error) {
    if (error?.code === "EEXIST") return false;
    throw error;
  }
}

function releaseLock() {
  try { fs.unlinkSync(LOCK_PATH); } catch {}
}

function processFile(filePath, state) {
  const stat = fs.statSync(filePath);
  const previous = state.files[filePath];
  if (!previous) {
    state.files[filePath] = { offset: 0, line: 0, initializedAt: new Date().toISOString() };
  }
  const cursor = state.files[filePath];
  if (stat.size <= cursor.offset) return;
  const start = cursor.offset;
  const length = Math.min(stat.size - start, MAX_SCAN_BYTES);
  const buffer = Buffer.alloc(length);
  const handle = fs.openSync(filePath, "r");
  try { fs.readSync(handle, buffer, 0, length, start); } finally { fs.closeSync(handle); }
  const text = buffer.toString("utf8");
  const lines = text.split(/\r?\n/);
  const completeCount = text.endsWith("\n") ? lines.length - 1 : Math.max(0, lines.length - 1);
  let consumed = 0;
  const priorLine = cursor.line || 0;
  for (let index = 0; index < completeCount; index += 1) {
    const line = lines[index];
    consumed += Buffer.byteLength(line, "utf8") + 1;
    try {
      const record = JSON.parse(line);
      const [event, value] = eventFromRecord(record);
      if (event && value) appendEntry(filePath, priorLine + index, event, record, value);
    } catch {}
  }
  state.files[filePath] = { offset: start + consumed, line: priorLine + completeCount, initializedAt: cursor.initializedAt };
}

function syncOnce() {
  if (!acquireLock()) return { skipped: true, reason: "lock-held" };
  try {
    const state = loadState();
    const files = walkJsonl(SESSIONS_ROOT);
    if (!state.initialized) {
      for (const filePath of files) {
        const stat = fs.statSync(filePath);
        state.files[filePath] = { offset: stat.size, initializedAt: new Date().toISOString() };
      }
      state.initialized = true;
      saveState(state);
      return { initialized: true, files: files.length };
    }
    for (const filePath of files) processFile(filePath, state);
    saveState(state);
    return { initialized: false, files: files.length };
  } finally {
    releaseLock();
  }
}

try {
  const result = syncOnce();
  if (process.argv.includes("--verbose")) process.stdout.write(`${JSON.stringify(result)}\n`);
} catch {
  // Logging is fail-open and must never block Codex.
  releaseLock();
}
