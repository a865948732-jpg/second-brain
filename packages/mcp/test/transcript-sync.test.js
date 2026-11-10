import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const syncPath = fileURLToPath(new URL("../../../hooks/second-brain-transcript-sync.mjs", import.meta.url));

async function runFixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "codex-transcript-sync-"));
  const codexHome = path.join(root, "codex-home");
  const sessions = path.join(codexHome, "sessions", "2026", "08", "13");
  const vault = path.join(root, "vault");
  const transcript = path.join(sessions, "turn.jsonl");
  await fs.mkdir(sessions, { recursive: true });
  await fs.mkdir(vault, { recursive: true });
  const env = {
    ...process.env,
    CODEX_HOME: codexHome,
    SECOND_BRAIN_VAULT: vault,
    CODEX_SECOND_BRAIN_LOG_DIR: path.join(vault, "05_Review", "会话日志", "Codex"),
    CODEX_TRANSCRIPT_SYNC_STATE: path.join(codexHome, "sync-state.json"),
  };
  const run = () => spawnSync(process.execPath, [syncPath, "--verbose"], { env, encoding: "utf8", timeout: 5000 });
  await fs.writeFile(transcript, `${JSON.stringify({ type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "历史消息" }] } })}\n`);
  const initialized = run();
  assert.equal(initialized.status, 0, initialized.stderr);
  const logPath = path.join(env.CODEX_SECOND_BRAIN_LOG_DIR, "2026-08-13.md");
  assert.equal(await fs.access(logPath).then(() => true).catch(() => false), false);
  await fs.appendFile(transcript, `${JSON.stringify({ timestamp: "2026-08-13T15:00:00.000Z", type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "新增消息" }] } })}\n`);
  await fs.appendFile(transcript, `${JSON.stringify({ timestamp: "2026-08-13T15:00:01.000Z", type: "response_item", payload: { type: "message", role: "assistant", phase: "final_answer", content: [{ type: "output_text", text: "新增回答" }] } })}\n`);
  const synced = run();
  assert.equal(synced.status, 0, synced.stderr);
  const firstLog = await fs.readFile(logPath, "utf8");
  assert.match(firstLog, /新增消息/);
  assert.match(firstLog, /新增回答/);
  const repeated = run();
  assert.equal(repeated.status, 0, repeated.stderr);
  const secondLog = await fs.readFile(logPath, "utf8");
  assert.equal(secondLog, firstLog);
  const newTranscript = path.join(sessions, "new-turn.jsonl");
  await fs.writeFile(newTranscript, `${JSON.stringify({ timestamp: "2026-08-13T15:01:00.000Z", type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "新会话第一条消息" }] } })}\n`);
  const newFileRun = run();
  assert.equal(newFileRun.status, 0, newFileRun.stderr);
  const thirdLog = await fs.readFile(logPath, "utf8");
  assert.match(thirdLog, /新会话第一条消息/);
  await fs.rm(root, { recursive: true, force: true });
}

test("transcript sync initializes at the tail and appends only new records", runFixture);
