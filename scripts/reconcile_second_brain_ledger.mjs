#!/usr/bin/env node

import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";
import { isDeepStrictEqual } from "node:util";
import config from "../packages/config.cjs";


const DEFAULT_VAULT = config.getVaultRoot();
const DEFAULT_RUNTIME = config.getRuntimePath();


function positiveInteger(value, label, maximum) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw new Error(`${label} must be an integer from 1 to ${maximum}.`);
  }
  return parsed;
}


export async function reconcileLedger({
  vaultRoot = DEFAULT_VAULT,
  runtimePath = DEFAULT_RUNTIME,
  now = Date.now(),
  staleAfterMs = 24 * 60 * 60 * 1000,
  maxTasks = 100,
  excludeTaskIds = [],
  dryRun,
} = {}) {
  if (typeof dryRun !== "boolean") {
    throw new Error("Reconciliation mode must be explicitly dry-run or apply.");
  }
  const limit = positiveInteger(maxTasks, "maxTasks", 1000);
  const staleMs = positiveInteger(staleAfterMs, "staleAfterMs", 366 * 24 * 60 * 60 * 1000);
  const excluded = [...new Set(
    (Array.isArray(excludeTaskIds) ? excludeTaskIds : [])
      .map((value) => String(value || "").trim())
      .filter(Boolean),
  )];
  const runtime = await import(pathToFileURL(path.resolve(runtimePath)).href);
  for (const name of ["readLedger", "auditLedger", "closeStaleTasks", "verifyLedger"]) {
    if (typeof runtime[name] !== "function") {
      throw new Error(`Second-brain runtime is missing ${name}.`);
    }
  }

  const beforeRecords = await runtime.readLedger(vaultRoot);
  const beforeSnapshot = structuredClone(beforeRecords);
  const beforeVerification = runtime.verifyLedger(beforeSnapshot);
  const beforeAudit = runtime.auditLedger(beforeSnapshot, {
    now,
    staleAfterMs: staleMs,
  });
  const operation = await runtime.closeStaleTasks({
    vaultRoot,
    now,
    staleAfterMs: staleMs,
    excludeTaskIds: excluded,
    maxTasks: limit,
    dryRun,
    source: "workbuddy-weekly-ledger-reconciliation",
  });
  const afterRecords = await runtime.readLedger(vaultRoot);
  const afterVerification = runtime.verifyLedger(afterRecords);
  const afterAudit = runtime.auditLedger(afterRecords, {
    now,
    staleAfterMs: staleMs,
  });
  const observedDelta = afterRecords.length - beforeSnapshot.length;
  const prefixUnchanged = beforeSnapshot.every((record, index) => (
    isDeepStrictEqual(record, afterRecords[index])
  ));
  const appendOnly = prefixUnchanged && observedDelta >= operation.closed;
  if (!beforeVerification.valid || !afterVerification.valid || !appendOnly) {
    throw new Error("Ledger reconciliation did not preserve the append-only hash chain.");
  }
  if (operation.closed > limit) {
    throw new Error("Ledger reconciliation exceeded its per-cycle limit.");
  }

  return {
    schema: "second-brain-ledger-reconcile-v1",
    status: "PASS",
    action: dryRun ? "DRY_RUN" : "APPLY",
    stale_after_hours: staleMs / (60 * 60 * 1000),
    max_tasks: limit,
    excluded_count: excluded.length,
    record_count_before: beforeSnapshot.length,
    record_count_after: afterRecords.length,
    observed_record_delta: observedDelta,
    open_count_before: beforeAudit.openTaskCount,
    stale_count_before: beforeAudit.staleTaskCount,
    open_count_after: afterAudit.openTaskCount,
    stale_count_after: afterAudit.staleTaskCount,
    total_eligible: operation.totalEligible,
    selected: operation.eligible,
    closed: operation.closed,
    remaining: operation.remaining,
    chain_head_before: beforeAudit.chainHead,
    chain_head_after: afterAudit.chainHead,
    chain_valid: true,
    prefix_unchanged: true,
    append_only: true,
  };
}


export function parseArgs(argv) {
  const options = {
    vaultRoot: DEFAULT_VAULT,
    staleAfterMs: 24 * 60 * 60 * 1000,
    maxTasks: 100,
    excludeTaskIds: [],
    dryRun: undefined,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--dry-run" || argument === "--apply") {
      if (options.dryRun !== undefined) throw new Error("Choose exactly one mode.");
      options.dryRun = argument === "--dry-run";
    } else if (argument === "--vault") {
      options.vaultRoot = argv[++index];
    } else if (argument === "--stale-hours") {
      const hours = positiveInteger(argv[++index], "stale-hours", 24 * 366);
      options.staleAfterMs = hours * 60 * 60 * 1000;
    } else if (argument === "--max-tasks") {
      options.maxTasks = positiveInteger(argv[++index], "max-tasks", 1000);
    } else if (argument === "--exclude-task-id") {
      options.excludeTaskIds.push(argv[++index]);
    } else {
      throw new Error(`Unknown argument: ${argument || "<missing>"}`);
    }
  }
  if (options.dryRun === undefined) {
    throw new Error("Choose exactly one of --dry-run or --apply.");
  }
  if (!options.vaultRoot) throw new Error("--vault requires a value.");
  if (options.excludeTaskIds.some((value) => !value)) {
    throw new Error("--exclude-task-id requires a value.");
  }
  return options;
}


async function main() {
  try {
    const result = await reconcileLedger(parseArgs(process.argv.slice(2)));
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({
      schema: "second-brain-ledger-reconcile-v1",
      status: "HOLD",
      error_type: error?.constructor?.name || "Error",
      error: String(error?.message || error),
    })}\n`);
    process.exitCode = 1;
  }
}


if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await main();
}
