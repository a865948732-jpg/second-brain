#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { pathToFileURL } from "node:url";

import { buildCoreContext, buildPromptMemory } from "./lib.js";
import config from "../config.cjs";

const PROMPT_RETRY_WINDOW_MS = 2 * 60 * 1000;

function eventArg() {
  const index = process.argv.indexOf("--event");
  return index >= 0 ? String(process.argv[index + 1] || "") : "";
}

function readInput() {
  const raw = fs.readFileSync(0, "utf8").replace(/^\uFEFF/, "").trim();
  return raw ? JSON.parse(raw) : {};
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

function promptFrom(data) {
  const keys = ["prompt", "user_prompt", "userPrompt", "message", "input", "content"];
  for (const key of keys) {
    const value = textFrom(data[key]);
    if (value.trim()) return value.trim();
  }
  if (data.payload && typeof data.payload === "object") return promptFrom(data.payload);
  return "";
}

function firstText(data, keys) {
  for (const key of keys) {
    const value = textFrom(data?.[key]);
    if (value.trim()) return value.trim();
  }
  if (data?.payload && typeof data.payload === "object") return firstText(data.payload, keys);
  return "";
}

function transcriptFrom(data) {
  return firstText(data, ["transcript_tail", "transcriptTail", "last_output", "lastOutput"]);
}

function transcriptPathFrom(data) {
  return nestedValue(data, ["transcript_path", "transcriptPath"]);
}

function allowedTranscriptPath(filePath) {
  if (!filePath) return false;
  const home = config.getCodexHome();
  const sessionsRoot = path.resolve(home, "sessions");
  const resolved = path.resolve(filePath);
  const relative = path.relative(sessionsRoot, resolved);
  return relative && !relative.startsWith("..") && !path.isAbsolute(relative) &&
    path.extname(resolved).toLowerCase() === ".jsonl";
}

function assistantFromTranscript(filePath) {
  if (!allowedTranscriptPath(filePath) || !fs.existsSync(filePath)) return "";
  const stat = fs.statSync(filePath);
  const length = Math.min(stat.size, 4 * 1024 * 1024);
  const buffer = Buffer.alloc(length);
  const handle = fs.openSync(filePath, "r");
  try {
    fs.readSync(handle, buffer, 0, length, Math.max(0, stat.size - length));
  } finally {
    fs.closeSync(handle);
  }
  const candidates = [];
  for (const line of buffer.toString("utf8").split(/\r?\n/).filter(Boolean)) {
    try {
      const record = JSON.parse(line);
      const payload = record.payload || {};
      if (record.type === "event_msg" && payload.type === "agent_message") {
        candidates.push({ text: textFrom(payload.message), phase: payload.phase || "" });
      } else if (record.type === "response_item" && payload.type === "message" && payload.role === "assistant") {
        candidates.push({ text: textFrom(payload.content), phase: payload.phase || "" });
      }
    } catch {
      // Ignore an incomplete final line while the transcript is flushing.
    }
  }
  return candidates.findLast((candidate) => candidate.phase === "final_answer" && candidate.text.trim())?.text ||
    candidates.findLast((candidate) => candidate.text.trim())?.text || "";
}

function writeContext(event, context, systemMessage = "") {
  if (!context) return;
  const output = {
    hookSpecificOutput: { hookEventName: event, additionalContext: context },
  };
  if (systemMessage) output.systemMessage = systemMessage;
  process.stdout.write(JSON.stringify(output));
}

function combineContexts(...contexts) {
  return contexts.filter((context) => String(context || "").trim()).join("\n\n");
}

function nestedValue(data, keys) {
  for (const key of keys) {
    const direct = data?.[key];
    if (typeof direct === "string" && direct.trim()) return direct.trim();
  }
  if (data?.payload && typeof data.payload === "object") return nestedValue(data.payload, keys);
  return "";
}

function shortHash(value, length = 24) {
  return crypto.createHash("sha256").update(String(value || ""), "utf8").digest("hex").slice(0, length);
}

function episodeIdentity(data, prompt = "") {
  const sessionId = nestedValue(data, ["session_id", "sessionId", "conversation_id", "conversationId"]);
  const threadId = nestedValue(data, ["thread_id", "threadId"]);
  const turnId = nestedValue(data, ["turn_id", "turnId"]);
  const transcriptPath = nestedValue(data, ["transcript_path", "transcriptPath"]);
  const scopes = [...new Set([threadId, sessionId, transcriptPath].filter(Boolean))];
  const scope = scopes[0] || "anonymous";
  const scopeKeys = scopes.length ? scopes.map((value) => shortHash(value)) : [shortHash(scope)];
  const sessionKey = scopeKeys[0];
  const turn = turnId || shortHash(prompt, 16);
  const turnKey = shortHash(turn);
  const stableKey = `${scope}\n${turn}`;
  const taskId = `codex-${shortHash(stableKey, 32)}`;
  return {
    taskId,
    eventPrefix: `${taskId}:${turnKey}`,
    sessionKey,
    scopeKeys,
    turnKey,
    explicitTurnId: Boolean(turnId),
  };
}

function latestOpenPrompt(records, identity, { promptKey = "", maxAgeMs = Number.POSITIVE_INFINITY } = {}) {
  const finishedTasks = new Set(records
    .filter((record) => record.event_type === "task_finished")
    .map((record) => record.task_id));
  const identityScopes = new Set(identity.scopeKeys);
  const now = Date.now();
  return records.findLast((record) => {
    if (record.event_type !== "prompt_submitted" || finishedTasks.has(record.task_id)) return false;
    const recordScopes = record.payload?.scope_keys || [record.payload?.session_key].filter(Boolean);
    if (!recordScopes.some((scopeKey) => identityScopes.has(scopeKey))) return false;
    if (promptKey && record.payload?.prompt_key !== promptKey) return false;
    const recordedAt = Date.parse(record.recorded_at);
    return !Number.isFinite(maxAgeMs) || (Number.isFinite(recordedAt) && now - recordedAt <= maxAgeMs);
  });
}

async function loadRuntime() {
  const runtimePath = config.getRuntimePath();
  return import(pathToFileURL(path.resolve(runtimePath)).href);
}

function runtimeVault() {
  return config.getVaultRoot();
}

function episodeLedgerPath(vaultRoot) {
  return path.join(path.resolve(vaultRoot), "05_Review", "SecondBrainRuntime", "episode-ledger.jsonl");
}

async function withEpisodeLock(callback) {
  const lockPath = `${episodeLedgerPath(runtimeVault())}.hook.lock`;
  const runtime = await loadRuntime();
  return runtime.withFileLock(lockPath, callback, {
    errorMessage: "Second-brain hook ledger lock is busy.",
  });
}

async function recordEventOnce(runtime, { taskId, eventType, eventId, event }) {
  const records = await runtime.readLedger(runtimeVault());
  const existing = records.find((record) => record.task_id === taskId &&
    record.event_type === eventType && record.payload?.event_id === eventId);
  if (existing) return { created: false, record: existing };
  return runtime.recordEvent({
    taskId,
    eventType,
    event: { event_id: eventId, ...event },
    vaultRoot: runtimeVault(),
  });
}

async function recordPromptEpisode(data, prompt) {
  try {
    const runtime = await loadRuntime();
    let identity = episodeIdentity(data, prompt);
    const promptKey = shortHash(runtime.redact(prompt));
    await withEpisodeLock(async () => {
      const records = await runtime.readLedger(runtimeVault());
      const previous = latestOpenPrompt(records, identity, {
        promptKey,
        maxAgeMs: PROMPT_RETRY_WINDOW_MS,
      });
      if (previous) {
        identity = {
          ...identity,
          taskId: previous.task_id,
          eventPrefix: String(previous.payload?.event_id || "").replace(/:prompt_submitted$/, "") || previous.task_id,
        };
      }
      await runtime.startTask({
        taskId: identity.taskId,
        goal: prompt || "Codex turn",
        taskType: "codex_turn",
        project: "second-brain",
        authority: "local hook",
        source: "codex-hook",
        vaultRoot: runtimeVault(),
      });
      await recordEventOnce(runtime, {
        taskId: identity.taskId,
        eventType: "prompt_submitted",
        eventId: `${identity.eventPrefix}:prompt_submitted`,
        event: {
          prompt,
          prompt_key: promptKey,
          session_key: identity.sessionKey,
          scope_keys: identity.scopeKeys,
          turn_key: identity.turnKey,
        },
      });
    });
  } catch {
    // Fail open: episode logging must never block prompt handling.
  }
}

async function recordStopEpisode(data) {
  try {
    const assistant = firstText(data, [
      "last_assistant_message", "lastAssistantMessage", "assistant_message", "assistantMessage",
      "response", "output", "message", "content",
    ]);
    const transcript = transcriptFrom(data) || assistantFromTranscript(transcriptPathFrom(data));
    const text = assistant || transcript;
    let identity = episodeIdentity(data, text);
    const stopReason = nestedValue(data, ["stop_reason", "stopReason", "reason"]) || "stop_hook";
    const runtime = await loadRuntime();
    await withEpisodeLock(async () => {
      const records = await runtime.readLedger(runtimeVault());
      const exactPrompt = records.findLast((record) =>
        record.task_id === identity.taskId && record.event_type === "prompt_submitted");
      const previous = exactPrompt || latestOpenPrompt(records, identity);
      if (previous) {
        identity = {
          ...identity,
          taskId: previous.task_id,
          eventPrefix: previous.task_id,
        };
      }
      await runtime.startTask({
        taskId: identity.taskId,
        goal: text || "Codex turn stopped",
        taskType: "codex_turn",
        project: "second-brain",
        authority: "local hook",
        source: "codex-hook",
        vaultRoot: runtimeVault(),
      });
      await recordEventOnce(runtime, {
        taskId: identity.taskId,
        eventType: "assistant_stopped",
        eventId: `${identity.eventPrefix}:assistant_stopped`,
        event: {
          assistant: text,
          stop_reason: stopReason,
          verification: { status: "unverified", regressionPassed: "not_run" },
        },
      });
      await runtime.finishTask({
        taskId: identity.taskId,
        outcome: "stopped",
        result: { assistant: text },
        verification: { status: "unverified", regressionPassed: "not_run", source: "codex-hook" },
        reflection: "Hook captured the turn outcome; no verification claim was made.",
        stopReason,
        source: "codex-hook",
        vaultRoot: runtimeVault(),
      });
    });
  } catch {
    // Fail open: episode logging must never block stop handling.
  }
}

async function main() {
  const event = eventArg();
  if (event === "SessionStart") {
    readInput();
    writeContext(event, await buildCoreContext(), "SECOND_BRAIN:ACTIVE");
    return;
  }
  if (event === "SubagentStart") {
    writeContext(event, await buildCoreContext());
    return;
  }
  if (event === "UserPromptSubmit") {
    const data = readInput();
    const prompt = promptFrom(data);
    await recordPromptEpisode(data, prompt);
    const memory = prompt ? await buildPromptMemory(prompt) : "";
    writeContext(event, memory);
    return;
  }
  if (event === "Stop" || event === "stop") {
    const data = readInput();
    await recordStopEpisode(data);
  }
}

try {
  await main();
} catch {
  // Fail open: a missing drive, malformed note, or locked file must not block Codex.
}
