import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import config from "../packages/config.cjs";
import { initializeVault } from "./init-vault.mjs";
import { captureInboxItem, upsertKnowledgeCard, searchVault, readNote } from "../packages/mcp/lib.js";
import { startTask, recordEvent, finishTask, recallEpisodes, readLedger, verifyLedger } from "../packages/runtime/runtime.mjs";

// 每次演示使用新目录；SECOND_BRAIN_VAULT 不会改变演示的写入目标。
const runId = `demo-${crypto.randomUUID()}`;
const vault = await initializeVault(path.join(config.repoRoot, ".local", runId, "vault"));
const taskId = `${runId}-task`;
await startTask({ taskId, goal: "验证第二大脑本地记忆闭环", taskType: "demo", project: "second-brain", vaultRoot: vault });
const capture = { text: "第二大脑演示：保留来源，先检索和阅读，再引用知识。", source: "manual", title: "演示收件" };
const first = await captureInboxItem(capture, vault);
const duplicate = await captureInboxItem(capture, vault);
assert.equal(first.created, true);
assert.equal(duplicate.created, false);
assert.equal(first.path, duplicate.path);
const card = await upsertKnowledgeCard({
  title: "第二大脑演示知识卡", type: "knowledge", status: "active", confidence: "高",
  source: "仓库内置本地演示；仅描述本次断言", tags: ["demo", "second-brain"],
  currentCognition: "示例库可以收件去重、保存知识卡、检索并读取卡片，再追加任务结果。",
  evidence: "scripts/demo.mjs 对本次生成的文件执行了实际断言。",
}, vault);
const results = await searchVault({ query: "第二大脑演示知识卡", limit: 5 }, vault);
assert(results.some((result) => result.path === card.path));
const note = await readNote(card.path, 20000, vault);
assert.match(note.content, /示例库可以收件去重/);
await recordEvent({ taskId, eventType: "verification", event: { captureDeduplicated: true, cardReadBack: true }, vaultRoot: vault });
const artifactHash = crypto.createHash("sha256").update(await fs.readFile(path.join(vault, card.path))).digest("hex");
await finishTask({
  taskId, outcome: "completed", result: { card: card.path },
  verification: { status: "passed", regressionPassed: false, receipt: { artifactHash, runId, verifier: "local-demo-assertions" } },
  reflection: "已验证本次本地示例闭环；未验证宿主 Hook、自动化、独立经验晋升或生产运行。",
  vaultRoot: vault,
});
const recalled = await recallEpisodes({ project: "second-brain", limit: 1, vaultRoot: vault });
assert.equal(recalled.results[0].task_id, taskId);
const records = await readLedger(vault);
assert.equal(verifyLedger(records).valid, true);
console.log(JSON.stringify({
  vault, capturePath: first.path, cardPath: card.path,
  captureDeduplicated: true, cardReadBack: true,
  recalledTask: recalled.results[0].task_id, ledgerRecords: records.length,
  ledgerValid: true, hostAutomationVerified: false,
}, null, 2));
