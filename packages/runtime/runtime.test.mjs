import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditLedger,
  closeStaleTasks,
  finishTask,
  readLedger,
  recallEpisodes,
  recordEvent,
  scoreCandidates,
  startTask,
  verifyLedger,
  withFileLock,
} from "./runtime.mjs";

async function makeVault() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "second-brain-runtime-"));
  await fs.mkdir(path.join(root, "05_Review"), { recursive: true });
  return root;
}

async function pathExists(filePath) {
  return fs.access(filePath).then(() => true).catch(() => false);
}

test("task lifecycle is append-only, idempotent, and hash chained", async () => {
  const vault = await makeVault();
  const start = await startTask({
    taskId: "task-001",
    goal: "修复检索回归",
    taskType: "development",
    project: "second-brain",
    authority: "本地测试",
    retrieval: { query: "检索回归", candidates: ["02_Knowledge/检索.md"], receipt: "r1" },
    vaultRoot: vault,
  });
  const duplicate = await startTask({
    taskId: "task-001",
    goal: "不同文本不应覆盖已开始任务",
    vaultRoot: vault,
  });
  assert.equal(start.created, true);
  assert.equal(duplicate.created, false);

  const finished = await finishTask({
    taskId: "task-001",
    outcome: "completed",
    verification: { status: "passed", tests: ["runtime.test.mjs"] },
    reflection: "先固定回执，再执行修改。",
    vaultRoot: vault,
  });
  const finishedAgain = await finishTask({
    taskId: "task-001",
    outcome: "failed",
    verification: { status: "failed" },
    reflection: "不能覆盖已经结束的任务。",
    vaultRoot: vault,
  });
  assert.equal(finished.created, true);
  assert.equal(finishedAgain.created, false);

  const ledger = await readLedger(vault);
  assert.equal(ledger.length, 2);
  assert.equal(ledger[0].previous_hash, "GENESIS");
  assert.equal(ledger[1].previous_hash, ledger[0].record_hash);
  assert.equal(ledger[1].payload.reflection, "先固定回执，再执行修改。");
});

test("sensitive values are redacted before they enter the ledger", async () => {
  const vault = await makeVault();
  await startTask({
    taskId: "task-secret",
    goal: "token=fixture-secret-value",
    authority: "Authorization: Bearer fixture-secret-value",
    vaultRoot: vault,
  });
  await recordEvent({
    taskId: "task-secret",
    eventType: "verification_event",
    event: { password: "nested-fixture-secret", safe: true },
    vaultRoot: vault,
  });
  const ledger = await readLedger(vault);
  const serialized = JSON.stringify(ledger);
  assert.doesNotMatch(serialized, /fixture-secret-value|nested-fixture-secret/);
  assert.match(serialized, /REDACTED/);
  assert.equal(ledger[1].event_type, "task_event");
  assert.equal(ledger[1].payload.original_event_type, "verification_event");
  assert.equal(ledger[1].payload.safe, true);
});

test("custom event names are normalized while canonical event types stay stable", async () => {
  const vault = await makeVault();
  await recordEvent({ taskId: "event-types", eventType: "verification", event: { passed: true }, vaultRoot: vault });
  await recordEvent({ taskId: "event-types", eventType: "one_off_custom_name", event: { value: 1 }, vaultRoot: vault });
  const ledger = await readLedger(vault);
  assert.equal(ledger[0].event_type, "verification");
  assert.equal(ledger[0].payload.original_event_type, undefined);
  assert.equal(ledger[1].event_type, "task_event");
  assert.equal(ledger[1].payload.original_event_type, "one_off_custom_name");
});

test("structured verification values keep their boolean and numeric types", async () => {
  const vault = await makeVault();
  await finishTask({
    taskId: "typed-payload",
    outcome: "completed",
    verification: { status: "passed", regressionPassed: true, testsPassed: 7 },
    reflection: "typed verification",
    vaultRoot: vault,
  });
  const ledger = await readLedger(vault);
  assert.equal(ledger[0].payload.verification.regressionPassed, true);
  assert.equal(ledger[0].payload.verification.testsPassed, 7);
});

test("lesson stays pending until two independent verified regressions exist", async () => {
  const vault = await makeVault();
  const base = {
    outcome: "failed",
    verification: { status: "passed", regressionPassed: true },
    reflection: "错误签名复现后修正了排序边界。",
    lessonCandidate: {
      errorSignature: "retrieval-boundary-v1",
      lesson: "先检查空查询和最小长度，再计算召回分数。",
      nextChange: "增加边界测试",
    },
  };
  await finishTask({
    taskId: "lesson-a",
    source: "codex",
    vaultRoot: vault,
    ...base,
    verification: {
      status: "passed",
      regressionPassed: true,
      receipt: { artifactHash: "a".repeat(64), runId: "run-a", verifier: "pytest-a" },
    },
  });
  const first = await recallEpisodes({ errorSignature: "retrieval-boundary-v1", vaultRoot: vault });
  assert.equal(first.results[0].lesson_status, "pending");

  await finishTask({
    taskId: "lesson-b",
    source: "claude",
    vaultRoot: vault,
    ...base,
    verification: {
      status: "passed",
      regressionPassed: true,
      receipt: { artifactHash: "b".repeat(64), runId: "run-b", verifier: "pytest-b" },
    },
  });
  const second = await recallEpisodes({ errorSignature: "retrieval-boundary-v1", vaultRoot: vault });
  assert.equal(second.results[0].lesson_status, "eligible_for_review");
  assert.equal(second.results[0].independent_sources, 2);
  assert.equal(second.results[0].verified_independent_sources, 2);
  assert.equal(second.results[0].distinct_verification_receipts, 2);
  assert.equal(second.results[0].distinct_verifiers, 2);
});

test("lesson eligibility uses the full ledger and requires explicit regression passes", async () => {
  const vault = await makeVault();
  const lessonCandidate = {
    errorSignature: "global-evidence-v1",
    lesson: "require an explicit regression result",
  };
  await finishTask({
    taskId: "global-a",
    outcome: "completed",
    source: "codex",
    verification: {
      status: "passed",
      regressionPassed: true,
      receipt: { artifactHash: "c".repeat(64), runId: "global-a", verifier: "node-test" },
    },
    reflection: "only-alpha-signal",
    lessonCandidate,
    vaultRoot: vault,
  });
  await finishTask({
    taskId: "global-b",
    outcome: "completed",
    source: "claude",
    verification: { status: "passed" },
    reflection: "second run did not record regression status",
    lessonCandidate,
    vaultRoot: vault,
  });
  let recalled = await recallEpisodes({ query: "only-alpha-signal", vaultRoot: vault });
  assert.equal(recalled.results[0].lesson_status, "pending");
  assert.equal(recalled.results[0].verified_independent_sources, 1);

  await finishTask({
    taskId: "global-c",
    outcome: "completed",
    source: "workbuddy",
    verification: {
      status: "passed",
      regressionPassed: true,
      receipt: { artifactHash: "d".repeat(64), runId: "global-c", verifier: "pytest" },
    },
    reflection: "third independent regression",
    lessonCandidate,
    vaultRoot: vault,
  });
  recalled = await recallEpisodes({ query: "only-alpha-signal", vaultRoot: vault });
  assert.equal(recalled.results[0].lesson_status, "eligible_for_review");
  assert.equal(recalled.results[0].verified_independent_sources, 2);
  assert.equal(recalled.results[0].distinct_verification_receipts, 2);
});

test("source labels alone cannot promote a lesson without distinct verification receipts", async () => {
  const vault = await makeVault();
  const lessonCandidate = { errorSignature: "labels-are-not-proof", lesson: "require real receipts" };
  for (const [taskId, source] of [["label-a", "codex"], ["label-b", "claude"]]) {
    await finishTask({
      taskId,
      source,
      outcome: "completed",
      verification: { status: "passed", regressionPassed: true },
      reflection: "a source label is not an independent verification artifact",
      lessonCandidate,
      vaultRoot: vault,
    });
  }
  const recalled = await recallEpisodes({ errorSignature: "labels-are-not-proof", vaultRoot: vault });
  assert.equal(recalled.results[0].verified_independent_sources, 0);
  assert.equal(recalled.results[0].distinct_verification_receipts, 0);
  assert.equal(recalled.results[0].lesson_status, "pending");
});

test("recall returns a receipt and ranks matching verified episodes", async () => {
  const vault = await makeVault();
  await finishTask({
    taskId: "episode-1",
    taskType: "research",
    project: "warm-coco",
    outcome: "completed",
    verification: { status: "passed" },
    reflection: "公开来源必须记录版本。",
    vaultRoot: vault,
  });
  const result = await recallEpisodes({ query: "公开来源 版本", project: "warm-coco", vaultRoot: vault });
  assert.match(result.receipt, /^[a-f0-9]{16}$/);
  assert.equal(result.results[0].task_id, "episode-1");
  assert.equal(result.results[0].evidence_level, "verified");
});

test("candidate scoring applies hard safety gates before quality ranking", () => {
  const result = scoreCandidates([
    { id: "unsafe", evidence: 1, goalFit: 1, verifiability: 1, reversibility: 1, historicalSuccess: 1, risk: 0, uncertainty: 0, irreversibleCost: 0, safe: false },
    { id: "safe", evidence: 0.8, goalFit: 0.8, verifiability: 0.8, reversibility: 0.8, historicalSuccess: 0.6, risk: 0.1, uncertainty: 0.1, irreversibleCost: 0.1, safe: true },
  ]);
  assert.equal(result[0].id, "safe");
  assert.equal(result.find((item) => item.id === "unsafe").decision, "HOLD");
});

test("candidate scoring fails closed for missing or misspelled safety fields", () => {
  const base = {
    evidence: 1,
    goalFit: 1,
    verifiability: 1,
    reversibility: 1,
    historicalSuccess: 1,
    risk: 0,
    uncertainty: 0,
    irreversibleCost: 0,
  };
  const result = scoreCandidates([
    { ...base, id: "misspelled", safety: false },
    { ...base, id: "missing" },
    { ...base, id: "wrong-type", safe: "true" },
    { ...base, id: "safe", safe: true },
  ]);

  assert.equal(result.find((item) => item.id === "misspelled").decision, "HOLD");
  assert.equal(result.find((item) => item.id === "missing").decision, "HOLD");
  assert.equal(result.find((item) => item.id === "wrong-type").decision, "HOLD");
  assert.equal(result.find((item) => item.id === "safe").decision, "ELIGIBLE");
  assert.equal(result.find((item) => item.id === "misspelled").score, null);
  assert.equal(result.find((item) => item.id === "safe").score_status, undefined);
});

test("candidate scoring rejects non-finite and out-of-range dimensions without echoing unknown data", () => {
  const result = scoreCandidates([
    { id: "nan", safe: true, evidence: Number.NaN, secret: "must-not-echo" },
    { id: "out-of-range", safe: true, goalFit: 2 },
  ]);

  assert.equal(result[0].decision, "HOLD");
  assert.equal(result[1].decision, "HOLD");
  assert.doesNotMatch(JSON.stringify(result), /must-not-echo/);
  assert.equal(result.every((item) => item.score === null), true);
});

test("ledger verification rejects tampering and accepts a valid hash chain", async () => {
  const vault = await makeVault();
  await startTask({ taskId: "chain-001", goal: "verify chain", vaultRoot: vault });
  await finishTask({
    taskId: "chain-001",
    outcome: "completed",
    verification: { status: "passed" },
    reflection: "chain is intact",
    vaultRoot: vault,
  });
  const ledger = await readLedger(vault);
  assert.equal(verifyLedger(ledger).valid, true);

  ledger[1].payload.reflection = "tampered";
  assert.throws(() => verifyLedger(ledger), /hash|chain/i);
});

test("stale task reconciliation is append-only, explicit, and idempotent", async () => {
  const vault = await makeVault();
  await startTask({ taskId: "stale-task", goal: "historical work", vaultRoot: vault });
  await startTask({ taskId: "excluded-task", goal: "still intentionally open", vaultRoot: vault });
  const future = Date.now() + 2 * 24 * 60 * 60 * 1000;

  const before = await readLedger(vault);
  const audit = auditLedger(before, { now: future });
  assert.equal(audit.openTaskCount, 2);
  assert.equal(audit.staleTaskCount, 2);

  const preview = await closeStaleTasks({
    vaultRoot: vault,
    now: future,
    excludeTaskIds: ["excluded-task"],
    dryRun: true,
  });
  assert.equal(preview.eligible, 1);
  assert.equal((await readLedger(vault)).length, before.length);

  const applied = await closeStaleTasks({
    vaultRoot: vault,
    now: future,
    excludeTaskIds: ["excluded-task"],
  });
  assert.equal(applied.closed, 1);
  const after = await readLedger(vault);
  assert.equal(verifyLedger(after).valid, true);
  assert.equal(after.at(-1).task_id, "stale-task");
  assert.equal(after.at(-1).event_type, "task_finished");
  assert.equal(after.at(-1).payload.outcome, "stale_unclosed");
  assert.equal(after.at(-1).payload.verification.status, "unverified");

  const repeated = await closeStaleTasks({ vaultRoot: vault, now: future });
  assert.equal(repeated.closed, 1);
  assert.equal(repeated.taskIds[0], "excluded-task");
  const final = await closeStaleTasks({ vaultRoot: vault, now: future });
  assert.equal(final.closed, 0);
});

test("stale task reconciliation enforces a per-cycle closure limit", async () => {
  const vault = await makeVault();
  for (const taskId of ["stale-a", "stale-b", "stale-c"]) {
    await startTask({ taskId, goal: "bounded historical work", vaultRoot: vault });
  }
  const future = Date.now() + 2 * 24 * 60 * 60 * 1000;

  const preview = await closeStaleTasks({
    vaultRoot: vault,
    now: future,
    maxTasks: 2,
    dryRun: true,
  });
  assert.equal(preview.totalEligible, 3);
  assert.equal(preview.eligible, 2);
  assert.equal(preview.taskIds.length, 2);

  const applied = await closeStaleTasks({
    vaultRoot: vault,
    now: future,
    maxTasks: 2,
  });
  assert.equal(applied.totalEligible, 3);
  assert.equal(applied.closed, 2);
  const audit = auditLedger(await readLedger(vault), { now: future });
  assert.equal(audit.staleTaskCount, 1);

  await assert.rejects(
    closeStaleTasks({ vaultRoot: vault, now: future, maxTasks: 0 }),
    /limit/i,
  );
});

test("file locks quarantine stale locks owned by dead processes", async () => {
  const vault = await makeVault();
  const lockPath = path.join(vault, "05_Review", "episode.lock");
  const old = new Date(Date.now() - 10_000);
  await fs.writeFile(lockPath, JSON.stringify({
    schema: "second-brain-lock-v1",
    pid: 2147483647,
    created_at: old.toISOString(),
    owner: "dead-00000000-0000-4000-8000-000000000000",
  }), "utf8");
  await fs.utimes(lockPath, old, old);

  await withFileLock(lockPath, async () => {
    const owner = JSON.parse(await fs.readFile(lockPath, "utf8"));
    assert.equal(owner.schema, "second-brain-lock-v1");
    assert.equal(owner.pid, process.pid);
  }, { staleTtlMs: 1000, attempts: 5, retryMs: 1 });

  assert.equal(await pathExists(lockPath), false);
  const quarantined = (await fs.readdir(path.dirname(lockPath))).filter((name) => name.startsWith("episode.lock.stale-"));
  assert.equal(quarantined.length, 1);
});

test("file locks fail closed for live and malformed stale locks", async () => {
  const vault = await makeVault();
  const directory = path.join(vault, "05_Review");
  const liveLock = path.join(directory, "live.lock");
  const old = new Date(Date.now() - 10_000);
  await fs.writeFile(liveLock, JSON.stringify({
    schema: "second-brain-lock-v1",
    pid: process.pid,
    created_at: old.toISOString(),
    owner: "1eaf0000-0000-4000-8000-000000000000",
  }), "utf8");
  await fs.utimes(liveLock, old, old);
  await assert.rejects(() => withFileLock(liveLock, async () => {}, { staleTtlMs: 1000, attempts: 2, retryMs: 1 }), /busy/);
  assert.equal(await pathExists(liveLock), true);

  const malformedLock = path.join(directory, "malformed.lock");
  await fs.writeFile(malformedLock, "legacy-lock", "utf8");
  await fs.utimes(malformedLock, old, old);
  await assert.rejects(() => withFileLock(malformedLock, async () => {}, { staleTtlMs: 1000, attempts: 2, retryMs: 1 }), /busy/);
  assert.equal(await pathExists(malformedLock), true);
});

test("concurrent lifecycle calls remain idempotent under the ledger lock", async () => {
  const vault = await makeVault();
  const starts = await Promise.all(Array.from({ length: 12 }, () => startTask({
    taskId: "concurrent-task",
    goal: "one task only",
    vaultRoot: vault,
  })));
  assert.equal(starts.filter((result) => result.created).length, 1);

  const events = await Promise.all(Array.from({ length: 12 }, () => recordEvent({
    taskId: "concurrent-task",
    eventType: "concurrent_event",
    event: { event_id: "concurrent-event-001", value: true },
    vaultRoot: vault,
  })));
  assert.equal(events.filter((result) => result.created).length, 1);
  const ledger = await readLedger(vault);
  assert.deepEqual(ledger.map((record) => record.event_type), ["task_started", "task_event"]);
  assert.equal(ledger[1].payload.original_event_type, "concurrent_event");
});
