import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const serverPath = fileURLToPath(new URL("../index.js", import.meta.url));
const runtimePath = fileURLToPath(new URL("../../runtime/runtime.mjs", import.meta.url));

async function withClient(run) {
  const vault = await fs.mkdtemp(path.join(os.tmpdir(), "second-brain-mcp-runtime-"));
  for (const directory of ["01_Sources", "02_Knowledge", "03_Topics", "04_Outputs", "05_Review", "06_Skills"]) {
    await fs.mkdir(path.join(vault, directory), { recursive: true });
  }
  const client = new Client({ name: "runtime-integration-test", version: "1.0.0" });
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverPath],
    env: { ...process.env, SECOND_BRAIN_VAULT: vault, SECOND_BRAIN_RUNTIME: runtimePath },
    stderr: "pipe",
  });
  try {
    await client.connect(transport);
    await run(client, vault);
  } finally {
    await client.close();
    await fs.rm(vault, { recursive: true, force: true });
  }
}

test("MCP task lifecycle is persisted, redacted, idempotent, and recallable", async () => {
  await withClient(async (client, vault) => {
    const startArgs = {
      taskId: "mcp-lifecycle-001",
      goal: "build memory loop token=fixture-secret",
      taskType: "development",
      project: "second-brain",
      retrieval: { query: "memory loop", candidates: ["02_Knowledge/example.md"], receipt: "r-1" },
    };
    const start = await client.callTool({ name: "second_brain_task_start", arguments: startArgs });
    const duplicateStart = await client.callTool({ name: "second_brain_task_start", arguments: startArgs });
    assert.equal(start.structuredContent.created, true);
    assert.equal(duplicateStart.structuredContent.created, false);

    await client.callTool({
      name: "second_brain_record_event",
      arguments: { taskId: startArgs.taskId, eventType: "test_result", event: { passed: true, tests: 3 } },
    });
    const finishArgs = {
      taskId: startArgs.taskId,
      outcome: "completed",
      result: { changedFiles: 2 },
      verification: { status: "passed", regressionPassed: true },
      reflection: "the lifecycle closed with a real client",
      lessonCandidate: { errorSignature: "mcp-life-v1", lesson: "verify through a real MCP client" },
    };
    const finish = await client.callTool({ name: "second_brain_task_finish", arguments: finishArgs });
    const duplicateFinish = await client.callTool({ name: "second_brain_task_finish", arguments: finishArgs });
    assert.equal(finish.structuredContent.created, true);
    assert.equal(duplicateFinish.structuredContent.created, false);

    const recalled = await client.callTool({
      name: "second_brain_recall_experience",
      arguments: { query: "real client", project: "second-brain" },
    });
    assert.equal(recalled.structuredContent.results[0].task_id, startArgs.taskId);
    assert.equal(recalled.structuredContent.results[0].evidence_level, "verified");
    assert.equal(recalled.structuredContent.results[0].lesson_status, "pending");

    const ledgerPath = path.join(vault, "05_Review", "SecondBrainRuntime", "episode-ledger.jsonl");
    const ledgerText = await fs.readFile(ledgerPath, "utf8");
    assert.doesNotMatch(ledgerText, /fixture-secret/);
    assert.equal(ledgerText.trim().split(/\r?\n/).length, 3);
    assert.equal(await fs.stat(path.join(vault, "01_Sources")).then((stat) => stat.isDirectory()), true);
    assert.deepEqual(await fs.readdir(path.join(vault, "01_Sources")), []);
  });
});

test("MCP candidate scoring enforces the safety HOLD gate", async () => {
  await withClient(async (client) => {
    const scored = await client.callTool({
      name: "second_brain_score_candidates",
      arguments: {
        candidates: [
          { id: "unsafe", safe: false, evidence: 1, goalFit: 1 },
          { id: "safe", safe: true, evidence: 0.8, goalFit: 0.8, verifiability: 0.8, reversibility: 0.8 },
        ],
      },
    });
    assert.equal(scored.structuredContent.results[0].id, "safe");
    assert.equal(scored.structuredContent.results.find((item) => item.id === "unsafe").decision, "HOLD");
  });
});

test("MCP candidate scoring rejects legacy or misspelled safety contracts", async () => {
  await withClient(async (client) => {
    const rejected = await client.callTool({
      name: "second_brain_score_candidates",
      arguments: {
        candidates: [{ id: "legacy", safety: false, goal_fit: 1 }],
      },
    });

    assert.equal(rejected.isError, true);
    assert.match(rejected.content[0].text, /safe/);
    assert.match(rejected.content[0].text, /Unrecognized key/);
  });
});
