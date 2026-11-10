import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test, { after, before } from "node:test";

const hookPath = new URL("../hook.js", import.meta.url);
let vault;

function requireHash(value) {
  return crypto.createHash("sha256").update(value, "utf8").digest("hex").slice(0, 32);
}

function shortTurnHash(value) {
  return crypto.createHash("sha256").update(value, "utf8").digest("hex").slice(0, 24);
}

async function write(relativePath, content) {
  const fullPath = path.join(vault, ...relativePath.split("/"));
  await fs.promises.mkdir(path.dirname(fullPath), { recursive: true });
  await fs.promises.writeFile(fullPath, content, "utf8");
}

function runHook(event, input = "") {
  return spawnSync(process.execPath, [fileURLToPath(hookPath), "--event", event], {
    input,
    encoding: "utf8",
    env: {
      ...process.env,
      SECOND_BRAIN_VAULT: vault,
      SECOND_BRAIN_RUNTIME: fileURLToPath(new URL("../../runtime/runtime.mjs", import.meta.url)),
      CODEX_APP_LAUNCH_ID: "hook-test-launch",
      EIGENFLUX_HEARTBEAT_STATE_FILE: path.join(vault, "hook-heartbeat-state.json"),
    },
    timeout: 5000,
  });
}

async function readEpisodeLedger() {
  const ledgerPath = path.join(vault, "05_Review", "SecondBrainRuntime", "episode-ledger.jsonl");
  try {
    const text = await fs.promises.readFile(ledgerPath, "utf8");
    return text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
  } catch (error) {
    if (error.code === "ENOENT") return [];
    throw error;
  }
}

before(async () => {
  vault = await fs.promises.mkdtemp(path.join(os.tmpdir(), "second-brain-hook-test-"));
  for (const directory of ["01_Sources", "02_Knowledge", "03_Topics", "04_Outputs", "05_Review", "06_Skills"]) {
    await fs.promises.mkdir(path.join(vault, directory), { recursive: true });
  }
  await write("02_Knowledge/_知识索引.md", "### 第一层：核心原则\n- 先查记忆。\n\n### 第二层：领域\n");
  for (const filename of [
    "AI幻觉反思——不确定机制时不得编造解释.md",
    "第二大脑全程伴随机制（实时记录监督自生长）.md",
    "第二大脑自生长体系搭建决策.md",
  ]) {
    await write(`02_Knowledge/${filename}`, `# 核心记忆\n\n## 当前认知\n\n先查第二大脑。\n`);
  }
  await write("03_Topics/暖暖项目.md", "# 暖暖项目\n\n暖暖需要长期身份连续性。\n");
});

after(async () => {
  await fs.promises.rm(vault, { recursive: true, force: true });
});

test("SessionStart emits Codex additionalContext", () => {
  const result = runHook("SessionStart");
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.systemMessage, "SECOND_BRAIN:ACTIVE");
  assert.equal(output.hookSpecificOutput.hookEventName, "SessionStart");
  assert.match(output.hookSpecificOutput.additionalContext, /先查记忆/);
});

test("UserPromptSubmit retrieves relevant memory from UTF-8 stdin", () => {
  const result = runHook("UserPromptSubmit", JSON.stringify({ prompt: "暖暖长期身份" }));
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.hookSpecificOutput.hookEventName, "UserPromptSubmit");
  assert.match(output.hookSpecificOutput.additionalContext, /暖暖项目/);
  assert.doesNotMatch(output.hookSpecificOutput.additionalContext, /EIGENFLUX FIRST-TURN HEARTBEAT/);
});

test("SubagentStart receives core context", () => {
  const result = runHook("SubagentStart");
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.hookSpecificOutput.hookEventName, "SubagentStart");
  assert.match(output.hookSpecificOutput.additionalContext, /SECOND BRAIN ACTIVE/);
});

test("UserPromptSubmit starts an episode and is idempotent for duplicate hook delivery", async () => {
  const input = JSON.stringify({
    session_id: "session-hook-lifecycle",
    turn_id: "turn-001",
    prompt: "记录暖暖的连续自我研究问题",
  });
  const first = runHook("UserPromptSubmit", input);
  const second = runHook("UserPromptSubmit", input);
  assert.equal(first.status, 0, first.stderr);
  assert.equal(second.status, 0, second.stderr);

  const ledger = await readEpisodeLedger();
  const taskId = `codex-${requireHash("session-hook-lifecycle\nturn-001")}`;
  const taskRecords = ledger.filter((record) => record.task_id === taskId);
  assert.deepEqual(taskRecords.map((record) => record.event_type), ["task_started", "prompt_submitted"]);
  assert.equal(taskRecords[0].task_id, taskRecords[1].task_id);
  assert.equal(taskRecords[1].payload.event_id, `${taskId}:${shortTurnHash("turn-001")}:prompt_submitted`);
});

test("UserPromptSubmit quarantines a stale dead-owner hook lock", async () => {
  const lockPath = path.join(vault, "05_Review", "SecondBrainRuntime", "episode-ledger.jsonl.hook.lock");
  const old = new Date(Date.now() - 120_000);
  await fs.promises.mkdir(path.dirname(lockPath), { recursive: true });
  await fs.promises.writeFile(lockPath, JSON.stringify({
    schema: "second-brain-lock-v1",
    pid: 2147483647,
    created_at: old.toISOString(),
    owner: "dead-00000000-0000-4000-8000-000000000000",
  }), "utf8");
  await fs.promises.utimes(lockPath, old, old);

  const result = runHook("UserPromptSubmit", JSON.stringify({
    session_id: "session-stale-hook-lock",
    turn_id: "turn-stale-hook-lock",
    prompt: "陈旧 Hook 锁恢复后仍能记录",
  }));
  assert.equal(result.status, 0, result.stderr);
  assert.equal(fs.existsSync(lockPath), false);
  const names = await fs.promises.readdir(path.dirname(lockPath));
  assert.equal(names.some((name) => name.startsWith("episode-ledger.jsonl.hook.lock.stale-")), true);
});

test("UserPromptSubmit collapses host retries when session and turn identifiers drift", async () => {
  const prompt = "同一用户提交被宿主重试时只保留一个经历";
  for (const suffix of ["a", "b", "c"]) {
    const result = runHook("UserPromptSubmit", JSON.stringify({
      thread_id: "thread-retry-stable",
      session_id: `session-retry-${suffix}`,
      turn_id: `turn-retry-${suffix}`,
      prompt,
    }));
    assert.equal(result.status, 0, result.stderr);
  }

  const ledger = await readEpisodeLedger();
  const starts = ledger.filter((record) =>
    record.event_type === "task_started" && record.payload?.goal === prompt);
  const prompts = ledger.filter((record) =>
    record.event_type === "prompt_submitted" && record.payload?.prompt === prompt);
  assert.equal(starts.length, 1);
  assert.equal(prompts.length, 1);
  assert.equal(starts[0].task_id, prompts[0].task_id);
});

test("Stop rejoins the open prompt when explicit session and turn identifiers drift", async () => {
  const prompt = "结束事件必须回到原始用户回合";
  runHook("UserPromptSubmit", JSON.stringify({
    thread_id: "thread-stop-stable",
    session_id: "session-stop-before-retry",
    turn_id: "turn-stop-before-retry",
    prompt,
  }));
  runHook("Stop", JSON.stringify({
    thread_id: "thread-stop-stable",
    session_id: "session-stop-after-retry",
    turn_id: "turn-stop-after-retry",
    last_assistant_message: "这是同一回合的最终回答。",
  }));

  const ledger = await readEpisodeLedger();
  const started = ledger.find((record) =>
    record.event_type === "task_started" && record.payload?.goal === prompt);
  const taskRecords = ledger.filter((record) => record.task_id === started.task_id);
  assert.deepEqual(taskRecords.map((record) => record.event_type), [
    "task_started",
    "prompt_submitted",
    "assistant_stopped",
    "task_finished",
  ]);
});

test("a repeated prompt starts a new episode after the earlier task finishes", async () => {
  const prompt = "相同问题在前一回合结束后应当成为新经历";
  const firstIdentity = {
    thread_id: "thread-repeat-after-finish",
    session_id: "session-repeat-first",
    turn_id: "turn-repeat-first",
  };
  runHook("UserPromptSubmit", JSON.stringify({ ...firstIdentity, prompt }));
  runHook("Stop", JSON.stringify({
    ...firstIdentity,
    last_assistant_message: "第一回合已结束。",
  }));
  runHook("UserPromptSubmit", JSON.stringify({
    thread_id: "thread-repeat-after-finish",
    session_id: "session-repeat-second",
    turn_id: "turn-repeat-second",
    prompt,
  }));

  const ledger = await readEpisodeLedger();
  const prompts = ledger.filter((record) =>
    record.event_type === "prompt_submitted" && record.payload?.prompt === prompt);
  assert.equal(prompts.length, 2);
  assert.notEqual(prompts[0].task_id, prompts[1].task_id);
});

test("Stop resumes the latest prompt task when the event omits turn_id", async () => {
  const prompt = "没有显式回合编号也要保持同一经历";
  runHook("UserPromptSubmit", JSON.stringify({ session_id: "session-no-turn", prompt }));
  runHook("Stop", JSON.stringify({
    session_id: "session-no-turn",
    last_assistant_message: "本轮只完成了记录，尚未验证。",
  }));

  const ledger = await readEpisodeLedger();
  const sessionKey = shortTurnHash("session-no-turn");
  const promptRecord = ledger.find((record) =>
    record.event_type === "prompt_submitted" && record.payload?.session_key === sessionKey);
  const taskRecords = ledger.filter((record) => record.task_id === promptRecord.task_id);
  assert.deepEqual(taskRecords.map((record) => record.event_type), [
    "task_started",
    "prompt_submitted",
    "assistant_stopped",
    "task_finished",
  ]);
});

test("Stop can read the final assistant message from an allowed Codex transcript", async () => {
  const codexHome = path.join(vault, "codex-home");
  const transcriptPath = path.join(codexHome, "sessions", "2026", "08", "turn.jsonl");
  await fs.promises.mkdir(path.dirname(transcriptPath), { recursive: true });
  await fs.promises.writeFile(transcriptPath, `${JSON.stringify({
    type: "response_item",
    payload: {
      type: "message",
      role: "assistant",
      phase: "final_answer",
      content: [{ type: "output_text", text: "来自允许范围内 transcript 的最终回答" }],
    },
  })}\n`, "utf8");
  runHook("UserPromptSubmit", JSON.stringify({
    session_id: "session-transcript",
    turn_id: "turn-transcript",
    prompt: "读取最终输出",
  }));
  const result = spawnSync(process.execPath, [fileURLToPath(hookPath), "--event", "Stop"], {
    input: JSON.stringify({
      session_id: "session-transcript",
      turn_id: "turn-transcript",
      transcript_path: transcriptPath,
    }),
    encoding: "utf8",
    env: {
      ...process.env,
      CODEX_HOME: codexHome,
      SECOND_BRAIN_VAULT: vault,
      SECOND_BRAIN_RUNTIME: fileURLToPath(new URL("../../runtime/runtime.mjs", import.meta.url)),
    },
    timeout: 5000,
  });
  assert.equal(result.status, 0, result.stderr);
  const ledger = await readEpisodeLedger();
  const taskId = `codex-${requireHash("session-transcript\nturn-transcript")}`;
  const finished = ledger.find((record) => record.task_id === taskId && record.event_type === "task_finished");
  assert.match(finished.payload.result.assistant, /最终回答/);
});

test("Stop records an unverified result and remains idempotent", async () => {
  const identity = { session_id: "session-hook-stop", turn_id: "turn-002" };
  runHook("UserPromptSubmit", JSON.stringify({ ...identity, prompt: "验证一个记忆回归" }));
  const first = runHook("Stop", JSON.stringify({
    ...identity,
    last_assistant_message: "验证尚未完成，不能宣称通过。",
    stop_reason: "turn_completed",
  }));
  const second = runHook("Stop", JSON.stringify({
    ...identity,
    last_assistant_message: "这次重复停止不应追加记录。",
  }));
  assert.equal(first.status, 0, first.stderr);
  assert.equal(second.status, 0, second.stderr);

  const ledger = await readEpisodeLedger();
  const taskId = `codex-${requireHash("session-hook-stop\nturn-002")}`;
  const taskRecords = ledger.filter((record) => record.task_id === taskId);
  assert.deepEqual(taskRecords.map((record) => record.event_type), [
    "task_started",
    "prompt_submitted",
    "assistant_stopped",
    "task_finished",
  ]);
  const finished = taskRecords.find((record) => record.event_type === "task_finished");
  assert.equal(finished.payload.verification.status, "unverified");
  assert.equal(finished.payload.verification.regressionPassed, "not_run");
  assert.equal(finished.payload.stop_reason, "turn_completed");
  assert.match(finished.payload.result.assistant, /验证尚未完成/);
});

test("episode logging is fail-open when the runtime is unavailable", () => {
  const result = spawnSync(process.execPath, [fileURLToPath(hookPath), "--event", "UserPromptSubmit"], {
    input: JSON.stringify({ session_id: "session-fail-open", turn_id: "turn-003", prompt: "暖暖" }),
    encoding: "utf8",
    env: {
      ...process.env,
      SECOND_BRAIN_VAULT: vault,
      SECOND_BRAIN_RUNTIME: path.join(vault, "missing-runtime.mjs"),
      EIGENFLUX_HEARTBEAT_STATE_FILE: path.join(vault, "hook-heartbeat-state.json"),
    },
    timeout: 5000,
  });
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.equal(output.hookSpecificOutput.hookEventName, "UserPromptSubmit");
});

test("sensitive values are redacted before hook episodes are persisted", async () => {
  const secret = "fixture-secret-value";
  runHook("UserPromptSubmit", JSON.stringify({
    session_id: "session-secret",
    turn_id: "turn-secret",
    prompt: `password=${secret}`,
  }));
  runHook("Stop", JSON.stringify({
    session_id: "session-secret",
    turn_id: "turn-secret",
    last_assistant_message: `Authorization: Bearer ${secret}`,
  }));
  const serialized = JSON.stringify(await readEpisodeLedger());
  assert.doesNotMatch(serialized, /fixture-secret-value/);
  assert.match(serialized, /REDACTED/);
});

test("raw session and turn identifiers are stored only as hashes", async () => {
  runHook("UserPromptSubmit", JSON.stringify({
    session_id: "private-session-identifier",
    turn_id: "private-turn-identifier",
    prompt: "只记录非敏感目标",
  }));
  const serialized = JSON.stringify(await readEpisodeLedger());
  assert.doesNotMatch(serialized, /private-session-identifier/);
  assert.doesNotMatch(serialized, /private-turn-identifier/);
});
