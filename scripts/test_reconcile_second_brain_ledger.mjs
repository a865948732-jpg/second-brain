import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { reconcileLedger } from "./reconcile_second_brain_ledger.mjs";


const RUNTIME_PATH = fileURLToPath(new URL("../packages/runtime/runtime.mjs", import.meta.url));
const runtime = await import(pathToFileURL(RUNTIME_PATH).href);


async function makeVault() {
  return fs.mkdtemp(path.join(os.tmpdir(), "second-brain-reconcile-"));
}


test("dry-run audits stale tasks without changing the ledger", async () => {
  const vault = await makeVault();
  await runtime.startTask({ taskId: "stale-a", goal: "old work", vaultRoot: vault });
  await runtime.startTask({ taskId: "stale-b", goal: "old work", vaultRoot: vault });
  const future = Date.now() + 2 * 24 * 60 * 60 * 1000;
  const before = await runtime.readLedger(vault);

  const result = await reconcileLedger({
    vaultRoot: vault,
    runtimePath: RUNTIME_PATH,
    now: future,
    maxTasks: 100,
    dryRun: true,
  });

  const after = await runtime.readLedger(vault);
  assert.equal(result.status, "PASS");
  assert.equal(result.action, "DRY_RUN");
  assert.equal(result.total_eligible, 2);
  assert.equal(result.closed, 0);
  assert.equal(result.append_only, true);
  assert.equal(after.length, before.length);
  assert.equal(result.chain_head_before, result.chain_head_after);
});


test("apply appends only the bounded set and honors exclusions", async () => {
  const vault = await makeVault();
  for (const taskId of ["stale-a", "stale-b", "excluded-current-task"]) {
    await runtime.startTask({ taskId, goal: "old work", vaultRoot: vault });
  }
  const future = Date.now() + 2 * 24 * 60 * 60 * 1000;
  const before = await runtime.readLedger(vault);

  const result = await reconcileLedger({
    vaultRoot: vault,
    runtimePath: RUNTIME_PATH,
    now: future,
    maxTasks: 1,
    excludeTaskIds: ["excluded-current-task"],
    dryRun: false,
  });

  const after = await runtime.readLedger(vault);
  assert.equal(result.status, "PASS");
  assert.equal(result.action, "APPLY");
  assert.equal(result.total_eligible, 2);
  assert.equal(result.closed, 1);
  assert.equal(result.remaining, 1);
  assert.equal(result.excluded_count, 1);
  assert.equal(after.length, before.length + 1);
  assert.equal(runtime.verifyLedger(after).valid, true);
  assert.equal(after.at(-1).payload.outcome, "stale_unclosed");
  assert.notEqual(after.at(-1).task_id, "excluded-current-task");
});


test("a reconciliation that mutates the old prefix fails closed", async () => {
  const vault = await makeVault();
  const fakeRuntime = path.join(vault, "prefix-mutating-runtime.mjs");
  await fs.writeFile(fakeRuntime, `
let readCount = 0;
const before = [{ sequence: 1, record_hash: "original", payload: { value: "before" } }];
const after = [
  { sequence: 1, record_hash: "rewritten", payload: { value: "after" } },
  { sequence: 2, record_hash: "appended", payload: { outcome: "stale_unclosed" } },
];
export async function readLedger() {
  const records = readCount === 0 ? before : after;
  readCount += 1;
  return JSON.parse(JSON.stringify(records));
}
export function verifyLedger() { return { valid: true }; }
export function auditLedger(records) {
  return {
    openTaskCount: records.length === 1 ? 1 : 0,
    staleTaskCount: records.length === 1 ? 1 : 0,
    chainHead: records.at(-1)?.record_hash || "GENESIS",
  };
}
export async function closeStaleTasks() {
  return { closed: 1, eligible: 1, totalEligible: 1, remaining: 0 };
}
`, "utf8");

  await assert.rejects(
    reconcileLedger({
      vaultRoot: vault,
      runtimePath: fakeRuntime,
      now: Date.now() + 2 * 24 * 60 * 60 * 1000,
      maxTasks: 100,
      dryRun: false,
    }),
    /append-only/i,
  );
});


test("a damaged ledger fails closed without adding a record", async () => {
  const vault = await makeVault();
  await runtime.startTask({ taskId: "stale-a", goal: "old work", vaultRoot: vault });
  const ledger = path.join(
    vault,
    "05_Review",
    "SecondBrainRuntime",
    "episode-ledger.jsonl",
  );
  const records = await runtime.readLedger(vault);
  records[0].payload.goal = "tampered";
  await fs.writeFile(ledger, `${JSON.stringify(records[0])}\n`, "utf8");
  const before = await fs.readFile(ledger, "utf8");

  await assert.rejects(
    reconcileLedger({
      vaultRoot: vault,
      runtimePath: RUNTIME_PATH,
      now: Date.now() + 2 * 24 * 60 * 60 * 1000,
      maxTasks: 100,
      dryRun: false,
    }),
    /hash|chain/i,
  );

  assert.equal(await fs.readFile(ledger, "utf8"), before);
});
