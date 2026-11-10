import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import config from "../config.cjs";

const LEDGER_RELATIVE = path.join("05_Review", "SecondBrainRuntime", "episode-ledger.jsonl");
const MAX_TEXT = 4000;
const MAX_EVENTS = 100000;
const RECEIPT_HASH = /^[a-f0-9]{64}$/;
const DEFAULT_LOCK_TTL_MS = 60 * 1000;
const DEFAULT_LOCK_ATTEMPTS = 60;
const DEFAULT_LOCK_RETRY_MS = 25;
const LOCK_OWNER_SCHEMA = "second-brain-lock-v1";
const DEFAULT_STALE_TASK_MS = 24 * 60 * 60 * 1000;
const CANONICAL_TASK_EVENT_TYPES = new Set([
  "task_event",
  "action",
  "result",
  "failure",
  "verification",
  "checkpoint",
  "prompt_submitted",
  "assistant_stopped",
]);
const SCORE_DIMENSIONS = Object.freeze([
  "evidence",
  "goalFit",
  "verifiability",
  "reversibility",
  "historicalSuccess",
  "risk",
  "uncertainty",
  "irreversibleCost",
]);
const SCORE_TEXT_FIELDS = Object.freeze(["id", "name", "title", "action", "description"]);
const SCORE_CANDIDATE_KEYS = new Set(["safe", ...SCORE_DIMENSIONS, ...SCORE_TEXT_FIELDS]);
const LEGACY_SAFETY_KEY = /^(?:safety|is[_-]?safe|safe[_-]?to[_-]?run|安全|安全性|安全门)$/iu;

function vaultPath(vaultRoot) {
  return path.resolve(vaultRoot || config.getVaultRoot());
}

function ledgerPath(vaultRoot) {
  return config.resolveWithinVault(vaultPath(vaultRoot), LEDGER_RELATIVE);
}

function compact(value, max = MAX_TEXT) {
  const text = String(value ?? "")
    .replace(/\r/g, "")
    .replace(/\s{3,}/g, " ")
    .trim();
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

export function redact(value) {
  if (Array.isArray(value)) return value.map((item) => redact(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key,
      /^(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret|session|cookie)$/i.test(key)
        ? "[REDACTED]"
        : redact(item),
    ]));
  }
  if (value === null || value === undefined || typeof value === "boolean" || typeof value === "number") return value;
  let text = compact(value);
  const replacements = [
    [/\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b/g, "[REDACTED_KEY]"],
    [/\b(?:ghp|github_pat|glpat|xox[baprs])[-_A-Za-z0-9]{12,}\b/g, "[REDACTED_TOKEN]"],
    [/\bAKIA[0-9A-Z]{16}\b/g, "[REDACTED_AWS_KEY]"],
    [/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [REDACTED]"],
    [/(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret|session|cookie)\s*[:=]\s*["']?[^\s,"';]+/gi, (match) => `${match.split(/[:=]/)[0]}=[REDACTED]`],
    [/(?:验证码|校验码|verification code|otp)\s*[:：]?\s*\d{4,8}\b/gi, "验证码 [REDACTED]"],
  ];
  for (const [pattern, replacement] of replacements) text = text.replace(pattern, replacement);
  return text;
}

function cleanPayload(value) {
  if (Array.isArray(value)) return value.slice(0, 50).map(cleanPayload);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).slice(0, 50).map(([key, item]) => {
      const safeKey = compact(key, 120);
      return [
        safeKey,
        /^(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret|session|cookie)$/i.test(safeKey)
          ? "[REDACTED]"
          : cleanPayload(item),
      ];
    }));
  }
  return redact(value);
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return "null";
    if (Object.is(value, -0) || value === 0) return "0";
    if (Number.isSafeInteger(value)) return String(value);
    const [rawMantissa, exponent] = value.toExponential(17).split("e");
    const mantissa = rawMantissa.replace(/0+$/, "").replace(/\.$/, "");
    return `${mantissa}e${Number(exponent)}`;
  }
  return JSON.stringify(value);
}

function hashRecord(record) {
  return crypto.createHash("sha256").update(canonical(record), "utf8").digest("hex");
}

export function verifyLedger(records = []) {
  let previousHash = "GENESIS";
  for (let index = 0; index < records.length; index += 1) {
    const record = records[index];
    if (!record || record.sequence !== index + 1 || record.previous_hash !== previousHash) {
      throw new Error(`Second-brain episode ledger chain is invalid at sequence ${index + 1}.`);
    }
    const { record_hash: actualHash, ...withoutHash } = record;
    if (!actualHash || hashRecord(withoutHash) !== actualHash) {
      throw new Error(`Second-brain episode ledger hash is invalid at sequence ${index + 1}.`);
    }
    previousHash = actualHash;
  }
  return { valid: true, count: records.length, head: previousHash };
}

async function ensureLedger(vaultRoot) {
  const target = ledgerPath(vaultRoot);
  await fs.mkdir(path.dirname(target), { recursive: true });
  return target;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function lockOptionNumber(value, fallback, minimum, maximum) {
  const number = Number(value);
  return Number.isFinite(number) && number >= minimum && number <= maximum ? number : fallback;
}

function lockOptions(options = {}) {
  return {
    staleTtlMs: lockOptionNumber(options.staleTtlMs ?? process.env.SECOND_BRAIN_LOCK_TTL_MS, DEFAULT_LOCK_TTL_MS, 1000, 24 * 60 * 60 * 1000),
    attempts: Math.floor(lockOptionNumber(options.attempts, DEFAULT_LOCK_ATTEMPTS, 1, 1000)),
    retryMs: lockOptionNumber(options.retryMs, DEFAULT_LOCK_RETRY_MS, 1, 5000),
    errorMessage: options.errorMessage || "Second-brain file lock is busy.",
  };
}

function lockOwner() {
  return {
    schema: LOCK_OWNER_SCHEMA,
    pid: process.pid,
    created_at: new Date().toISOString(),
    owner: crypto.randomUUID(),
  };
}

function parseLockOwner(value) {
  try {
    const parsed = JSON.parse(value);
    if (parsed?.schema !== LOCK_OWNER_SCHEMA || !Number.isInteger(parsed.pid) || parsed.pid <= 0 ||
      typeof parsed.created_at !== "string" || !Number.isFinite(Date.parse(parsed.created_at)) ||
      typeof parsed.owner !== "string" || !/^[a-f0-9-]{16,100}$/i.test(parsed.owner)) return null;
    return parsed;
  } catch {
    return null;
  }
}

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH" || error?.code === "EINVAL") return false;
    return null;
  }
}

async function staleLockInfo(lockPath, staleTtlMs) {
  let stat;
  try {
    stat = await fs.stat(lockPath);
  } catch (error) {
    if (error.code === "ENOENT") return { reclaimable: false, missing: true };
    throw error;
  }
  if (Date.now() - stat.mtimeMs <= staleTtlMs) return { reclaimable: false, reason: "fresh" };
  let owner;
  try {
    owner = parseLockOwner(await fs.readFile(lockPath, "utf8"));
  } catch {
    return { reclaimable: false, reason: "unreadable-owner" };
  }
  if (!owner) return { reclaimable: false, reason: "invalid-owner" };
  if (Date.now() - Date.parse(owner.created_at) <= staleTtlMs) return { reclaimable: false, reason: "fresh-owner" };
  const alive = processIsAlive(owner.pid);
  return { reclaimable: alive === false, reason: alive === false ? "dead-owner" : "live-or-unknown-owner", owner };
}

async function quarantineLock(lockPath) {
  const quarantinedPath = `${lockPath}.stale-${Date.now()}-${crypto.randomUUID()}`;
  try {
    await fs.rename(lockPath, quarantinedPath);
    return quarantinedPath;
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "EEXIST") return "";
    throw error;
  }
}

async function releaseFileLock(lockPath, handle, owner) {
  await handle.close().catch(() => {});
  try {
    const current = parseLockOwner(await fs.readFile(lockPath, "utf8"));
    if (current?.owner === owner.owner) await fs.rm(lockPath, { force: true });
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
}

async function acquireFileLock(lockPath, options) {
  await fs.mkdir(path.dirname(lockPath), { recursive: true });
  const owner = lockOwner();
  for (let attempt = 0; attempt < options.attempts; attempt += 1) {
    let handle;
    try {
      handle = await fs.open(lockPath, "wx");
      try {
        await handle.writeFile(JSON.stringify(owner), "utf8");
        await handle.sync();
      } catch (error) {
        await handle.close().catch(() => {});
        await fs.rm(lockPath, { force: true }).catch(() => {});
        throw error;
      }
      return { handle, owner };
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      const stale = await staleLockInfo(lockPath, options.staleTtlMs);
      if (stale.reclaimable && await quarantineLock(lockPath)) continue;
      await sleep(options.retryMs);
    }
  }
  throw new Error(options.errorMessage);
}

export async function withFileLock(lockPath, callback, options = {}) {
  if (typeof callback !== "function") throw new TypeError("File lock callback is required.");
  const resolvedPath = path.resolve(lockPath);
  const acquired = await acquireFileLock(resolvedPath, lockOptions(options));
  try {
    return await callback();
  } finally {
    await releaseFileLock(resolvedPath, acquired.handle, acquired.owner);
  }
}

async function readLedgerFile(target) {
  const text = await fs.readFile(target, "utf8").catch((error) => {
    if (error.code === "ENOENT") return "";
    throw error;
  });
  const records = text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
  verifyLedger(records);
  return records;
}

export async function readLedger(vaultRoot) {
  const target = ledgerPath(vaultRoot);
  const lockPath = `${target}.lock`;
  let lastError;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    try {
      return await readLedgerFile(target);
    } catch (error) {
      lastError = error;
      if (attempt === 7) throw error;
      const lockPresent = await fs.access(lockPath).then(() => true).catch(() => false);
      if (!lockPresent) throw error;
      await sleep(10);
    }
  }
  throw lastError;
}

async function appendRecord({ eventType, taskId, payload, vaultRoot, dedupe }) {
  const target = await ensureLedger(vaultRoot);
  const lockPath = `${target}.lock`;
  return withFileLock(lockPath, async () => {
    const existing = await readLedgerFile(target);
    const duplicate = typeof dedupe === "function" ? dedupe(existing) : null;
    if (duplicate) return { created: false, record: duplicate };
    if (existing.length >= MAX_EVENTS) throw new Error("Second-brain episode ledger event limit reached.");
    const safePayload = cleanPayload(payload);
    const record = {
      schema: "second-brain-episode-v1",
      sequence: existing.length + 1,
      event_type: eventType,
      task_id: compact(taskId, 120),
      recorded_at: new Date().toISOString(),
      previous_hash: existing.at(-1)?.record_hash || "GENESIS",
      payload: safePayload,
    };
    record.record_hash = hashRecord(record);
    await fs.appendFile(target, `${JSON.stringify(record)}\n`, "utf8");
    return { created: true, record };
  }, { errorMessage: "Second-brain episode ledger is busy." });
}

function findTaskEvent(records, taskId, eventType) {
  return records.find((record) => record.task_id === taskId && record.event_type === eventType);
}

function normalizeTaskEventType(value) {
  const requested = compact(value || "task_event", 80).toLocaleLowerCase();
  if (!/^[a-z][a-z0-9_]{2,60}$/.test(requested)) throw new Error("eventType is invalid.");
  return CANONICAL_TASK_EVENT_TYPES.has(requested)
    ? { eventType: requested, originalEventType: "" }
    : { eventType: "task_event", originalEventType: requested };
}

export async function startTask({ taskId, goal, taskType = "general", project = "", authority = "", retrieval = {}, source = "codex", vaultRoot } = {}) {
  if (!String(taskId || "").trim()) throw new Error("taskId is required.");
  if (!String(goal || "").trim()) throw new Error("goal is required.");
  const result = await appendRecord({
    eventType: "task_started",
    taskId,
    vaultRoot,
    dedupe: (records) => findTaskEvent(records, taskId, "task_started"),
    payload: {
      goal: compact(goal),
      task_type: compact(taskType, 120),
      project: compact(project, 160),
      authority: compact(authority, 300),
      source: compact(source, 80),
      retrieval: {
        query: compact(retrieval.query, 500),
        candidates: Array.isArray(retrieval.candidates) ? retrieval.candidates.slice(0, 20).map((item) => compact(item, 300)) : [],
        receipt: compact(retrieval.receipt, 120),
      },
    },
  });
  return { created: result.created, taskId, record: result.record };
}

export async function recordEvent({ taskId, eventType = "task_event", event = {}, vaultRoot } = {}) {
  if (!String(taskId || "").trim()) throw new Error("taskId is required.");
  const normalized = normalizeTaskEventType(eventType);
  const payload = normalized.originalEventType
    ? { ...(event && typeof event === "object" && !Array.isArray(event) ? event : {}), original_event_type: normalized.originalEventType }
    : event;
  const eventId = event && typeof event === "object" && !Array.isArray(event) ? event.event_id : "";
  const result = await appendRecord({
    eventType: normalized.eventType,
    taskId,
    payload,
    vaultRoot,
    dedupe: eventId
      ? (records) => records.find((record) => record.task_id === taskId &&
        record.event_type === normalized.eventType &&
        record.payload?.event_id === eventId &&
        (!normalized.originalEventType || record.payload?.original_event_type === normalized.originalEventType))
      : undefined,
  });
  return { created: result.created, taskId, record: result.record };
}

export async function finishTask({ taskId, outcome, result = {}, verification = {}, reflection, lessonCandidate = null, nextChange = "", stopReason = "", source = "codex", vaultRoot } = {}) {
  if (!String(taskId || "").trim()) throw new Error("taskId is required.");
  if (!String(outcome || "").trim()) throw new Error("outcome is required.");
  const appendResult = await appendRecord({
    eventType: "task_finished",
    taskId,
    vaultRoot,
    dedupe: (records) => findTaskEvent(records, taskId, "task_finished"),
    payload: {
      outcome: compact(outcome, 120),
      result,
      verification,
      reflection: compact(reflection, 3000),
      lesson_candidate: lessonCandidate,
      next_change: compact(nextChange, 1000),
      stop_reason: compact(stopReason, 500),
      source: compact(source, 80),
    },
  });
  return { created: appendResult.created, taskId, record: appendResult.record };
}

function normalizedNow(value) {
  if (value instanceof Date) return value.getTime();
  if (typeof value === "string") return Date.parse(value);
  return Number(value ?? Date.now());
}

export function auditLedger(records = [], { now = Date.now(), staleAfterMs = DEFAULT_STALE_TASK_MS } = {}) {
  verifyLedger(records);
  const nowMs = normalizedNow(now);
  const staleMs = Number(staleAfterMs);
  if (!Number.isFinite(nowMs) || !Number.isFinite(staleMs) || staleMs < 60_000) {
    throw new Error("Ledger audit time boundary is invalid.");
  }

  const eventTypeCounts = new Map();
  const startedByTask = new Map();
  const finishedTasks = new Set();
  const lastActivityByTask = new Map();
  for (const record of records) {
    eventTypeCounts.set(record.event_type, (eventTypeCounts.get(record.event_type) || 0) + 1);
    if (record.event_type === "task_started" && !startedByTask.has(record.task_id)) {
      startedByTask.set(record.task_id, record);
    }
    if (record.event_type === "task_finished") finishedTasks.add(record.task_id);
    lastActivityByTask.set(record.task_id, record);
  }

  const openTasks = [...startedByTask.values()]
    .filter((record) => !finishedTasks.has(record.task_id))
    .map((record) => {
      const lastActivity = lastActivityByTask.get(record.task_id) || record;
      return {
        taskId: record.task_id,
        taskType: record.payload?.task_type || "",
        startedAt: record.recorded_at,
        startSequence: record.sequence,
        lastActivityAt: lastActivity.recorded_at,
        lastActivitySequence: lastActivity.sequence,
        stale: nowMs - Date.parse(lastActivity.recorded_at) > staleMs,
      };
    });

  return {
    totalRecords: records.length,
    chainHead: records.at(-1)?.record_hash || "GENESIS",
    eventTypeCount: eventTypeCounts.size,
    singletonEventTypeCount: [...eventTypeCounts.values()].filter((count) => count === 1).length,
    openTaskCount: openTasks.length,
    staleTaskCount: openTasks.filter((task) => task.stale).length,
    openTasks,
  };
}

export async function closeStaleTasks({
  vaultRoot,
  now = Date.now(),
  staleAfterMs = DEFAULT_STALE_TASK_MS,
  excludeTaskIds = [],
  maxTasks = 100,
  dryRun = false,
  source = "second-brain-ledger-maintenance",
} = {}) {
  const limit = Number(maxTasks);
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
    throw new Error("Stale-task closure limit must be an integer from 1 to 1000.");
  }
  const excluded = new Set(Array.isArray(excludeTaskIds) ? excludeTaskIds.map((item) => compact(item, 120)) : []);
  const selectStale = (records) => {
    const all = auditLedger(records, { now, staleAfterMs }).openTasks
      .filter((task) => task.stale && !excluded.has(task.taskId));
    return { all, selected: all.slice(0, limit) };
  };

  if (dryRun) {
    const records = await readLedger(vaultRoot);
    const { all, selected } = selectStale(records);
    return {
      dryRun: true,
      closed: 0,
      eligible: selected.length,
      totalEligible: all.length,
      remaining: all.length,
      taskIds: selected.map((task) => task.taskId),
    };
  }

  const target = await ensureLedger(vaultRoot);
  return withFileLock(`${target}.lock`, async () => {
    const records = await readLedgerFile(target);
    const { all, selected: staleTasks } = selectStale(records);
    if (!staleTasks.length) {
      return { dryRun: false, closed: 0, eligible: 0, totalEligible: 0, remaining: 0, taskIds: [] };
    }

    const lines = [];
    for (const task of staleTasks) {
      const record = {
        schema: "second-brain-episode-v1",
        sequence: records.length + 1,
        event_type: "task_finished",
        task_id: task.taskId,
        recorded_at: new Date(normalizedNow(now)).toISOString(),
        previous_hash: records.at(-1)?.record_hash || "GENESIS",
        payload: cleanPayload({
          outcome: "stale_unclosed",
          result: {
            administrative_closure: true,
            original_start_sequence: task.startSequence,
            last_activity_sequence: task.lastActivitySequence,
            last_activity_at: task.lastActivityAt,
          },
          verification: { status: "unverified", regressionPassed: false },
          reflection: "该历史任务超过治理阈值且没有 task_finished；本记录只追加管理性闭环，不推断原任务成功或失败。",
          lesson_candidate: null,
          next_change: "如需继续原目标，创建新的 task_id 并重新检索当前证据。",
          stop_reason: "historical_stale_task_reconciliation",
          source: compact(source, 80),
        }),
      };
      record.record_hash = hashRecord(record);
      records.push(record);
      lines.push(JSON.stringify(record));
    }
    await fs.appendFile(target, `${lines.join("\n")}\n`, "utf8");
    verifyLedger(records);
    return {
      dryRun: false,
      closed: staleTasks.length,
      eligible: staleTasks.length,
      totalEligible: all.length,
      remaining: all.length - staleTasks.length,
      taskIds: staleTasks.map((task) => task.taskId),
    };
  }, { errorMessage: "Second-brain episode ledger is busy." });
}

function textForRecord(record) {
  return JSON.stringify(record.payload || {}).toLocaleLowerCase();
}

function evidenceLevel(record) {
  const verification = record.payload?.verification || {};
  if (verification.status === "passed" && (verification.regressionPassed !== false)) return "verified";
  if (verification.status === "partial") return "inferred";
  return "unverified";
}

function verificationReceipt(record) {
  const verification = record.payload?.verification || {};
  const receipt = verification.receipt;
  if (!receipt || typeof receipt !== "object") return null;
  const artifactHash = String(receipt.artifactHash || "").toLowerCase();
  const runId = compact(receipt.runId, 160);
  const verifier = compact(receipt.verifier, 160);
  if (!RECEIPT_HASH.test(artifactHash) || !runId || !verifier) return null;
  return { artifactHash, runId, verifier };
}

export async function recallEpisodes({ query = "", taskType = "", project = "", outcome = "", errorSignature = "", limit = 10, vaultRoot } = {}) {
  const allRecords = await readLedger(vaultRoot);
  const startedByTask = new Map(allRecords.filter((record) => record.event_type === "task_started").map((record) => [record.task_id, record]));
  const records = allRecords.filter((record) => record.event_type === "task_finished");
  const terms = String(query).toLocaleLowerCase().split(/\s+/).filter(Boolean).slice(0, 12);
  const matching = records.map((record) => {
    const payloadText = textForRecord(record);
    const taskStarted = startedByTask.get(record.task_id);
    let score = 0;
    for (const term of terms) if (payloadText.includes(term)) score += 2;
    if (taskType && String(taskStarted?.payload?.task_type || "") === taskType) score += 4;
    if (project && String(taskStarted?.payload?.project || "") === project) score += 4;
    if (outcome && String(record.payload?.outcome || "") === outcome) score += 2;
    if (errorSignature && String(record.payload?.lesson_candidate?.errorSignature || "") === errorSignature) score += 8;
    return { record, score, taskStarted };
  }).filter(({ score, record }) => (!terms.length && !taskType && !project && !outcome && !errorSignature) || score > 0)
    .sort((a, b) => b.score - a.score || b.record.sequence - a.record.sequence)
    .slice(0, Math.max(1, Math.min(Number(limit) || 10, 50)));

  const lessonGroups = new Map();
  for (const record of records) {
    const candidate = record.payload?.lesson_candidate;
    if (!candidate?.errorSignature) continue;
    const group = lessonGroups.get(candidate.errorSignature) || {
      all: new Set(),
      verified: new Set(),
      receiptKeys: new Set(),
      verifierKeys: new Set(),
    };
    group.all.add(record.payload?.source || "unknown");
    const receipt = verificationReceipt(record);
    if (evidenceLevel(record) === "verified" && record.payload?.verification?.regressionPassed === true && receipt) {
      group.verified.add(record.payload?.source || "unknown");
      group.receiptKeys.add(`${receipt.artifactHash}:${receipt.runId}`);
      group.verifierKeys.add(receipt.verifier);
    }
    lessonGroups.set(candidate.errorSignature, group);
  }
  const results = matching.map(({ record, score, taskStarted }) => {
    const candidate = record.payload?.lesson_candidate;
    const sources = candidate?.errorSignature ? lessonGroups.get(candidate.errorSignature) : {
      all: new Set(), verified: new Set(), receiptKeys: new Set(), verifierKeys: new Set(),
    };
    const independentSources = sources.all.size;
    const verifiedIndependentSources = sources.verified.size;
    const distinctVerificationReceipts = sources.receiptKeys.size;
    const distinctVerifiers = sources.verifierKeys.size;
    return {
      task_id: record.task_id,
      task_type: taskStarted?.payload?.task_type || "",
      project: taskStarted?.payload?.project || "",
      outcome: record.payload?.outcome || "",
      reflection: record.payload?.reflection || "",
      lesson: candidate?.lesson || "",
      error_signature: candidate?.errorSignature || "",
      lesson_status: candidate?.errorSignature
        ? (verifiedIndependentSources >= 2 && distinctVerificationReceipts >= 2 && distinctVerifiers >= 2
          ? "eligible_for_review" : "pending")
        : "none",
      independent_sources: independentSources,
      verified_independent_sources: verifiedIndependentSources,
      distinct_verification_receipts: distinctVerificationReceipts,
      distinct_verifiers: distinctVerifiers,
      evidence_level: evidenceLevel(record),
      score,
      sequence: record.sequence,
    };
  });
  const receipt = crypto.createHash("sha256").update(canonical({ query, taskType, project, outcome, errorSignature, results }), "utf8").digest("hex").slice(0, 16);
  return { receipt, results };
}

export function scoreCandidates(candidates = []) {
  if (!Array.isArray(candidates)) return [];
  const scored = candidates.map((candidate) => {
    const validationError = validateScoreCandidate(candidate);
    if (validationError) return holdCandidate(candidate, validationError);
    const output = scoreCandidateOutput(candidate);
    if (candidate.safe === false) {
      return { ...output, decision: "HOLD", score: null, score_status: "not_scored", reason: "安全硬门未通过" };
    }
    const score = 0.24 * (candidate.evidence ?? 0)
      + 0.2 * (candidate.goalFit ?? 0)
      + 0.16 * (candidate.verifiability ?? 0)
      + 0.14 * (candidate.reversibility ?? 0)
      + 0.12 * (candidate.historicalSuccess ?? 0)
      - 0.06 * (candidate.risk ?? 0)
      - 0.04 * (candidate.uncertainty ?? 0)
      - 0.04 * (candidate.irreversibleCost ?? 0);
    return { ...output, decision: "ELIGIBLE", score: Math.round(score * 10000) / 10000 };
  });
  return scored.sort((a, b) => {
    const aHold = a.decision === "HOLD" ? 1 : 0;
    const bHold = b.decision === "HOLD" ? 1 : 0;
    return aHold - bHold || (b.score ?? Number.NEGATIVE_INFINITY) - (a.score ?? Number.NEGATIVE_INFINITY);
  });
}

function scoreCandidateOutput(candidate) {
  return Object.fromEntries(Object.entries(candidate).filter(([key]) => SCORE_CANDIDATE_KEYS.has(key)));
}

function holdCandidate(candidate, reason) {
  const output = candidate && typeof candidate === "object" && !Array.isArray(candidate)
    ? Object.fromEntries(Object.entries(candidate).filter(([key, value]) => SCORE_TEXT_FIELDS.includes(key) && typeof value === "string").map(([key, value]) => [key, compact(value, 500)]))
    : {};
  if (candidate && typeof candidate === "object" && !Array.isArray(candidate) && typeof candidate.safe === "boolean") {
    output.safe = candidate.safe;
  }
  return { ...output, decision: "HOLD", score: null, score_status: "not_scored", reason };
}

function validateScoreCandidate(candidate) {
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return "候选必须是对象";
  const prototype = Object.getPrototypeOf(candidate);
  if (prototype !== Object.prototype && prototype !== null) return "候选必须是普通对象";
  const unknownKeys = Object.keys(candidate).filter((key) => !SCORE_CANDIDATE_KEYS.has(key));
  if (unknownKeys.length) {
    const safetyKey = unknownKeys.find((key) => LEGACY_SAFETY_KEY.test(key));
    return safetyKey
      ? `安全字段 ${safetyKey} 未在契约中定义；必须使用 safe:boolean`
      : `候选包含未声明字段：${unknownKeys.slice(0, 3).join(",")}`;
  }
  if (!Object.prototype.hasOwnProperty.call(candidate, "safe")) return "缺少必填安全字段 safe:boolean";
  if (typeof candidate.safe !== "boolean") return "安全字段 safe 必须是 boolean";
  for (const key of SCORE_TEXT_FIELDS) {
    if (candidate[key] !== undefined && (typeof candidate[key] !== "string" || candidate[key].length > 1000)) {
      return `字段 ${key} 必须是长度不超过 1000 的字符串`;
    }
  }
  for (const key of SCORE_DIMENSIONS) {
    if (candidate[key] !== undefined && (typeof candidate[key] !== "number" || !Number.isFinite(candidate[key]) || candidate[key] < 0 || candidate[key] > 1)) {
      return `评分字段 ${key} 必须是 0 到 1 之间的有限数字`;
    }
  }
  return "";
}

export const runtimePaths = Object.freeze({ ledger: LEDGER_RELATIVE });
