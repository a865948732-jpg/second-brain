import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test, { after, before } from "node:test";

import {
  buildCoreContext,
  buildPromptMemory,
  captureInboxItem,
  getVaultStatus,
  parseMarkdown,
  readNote,
  resolveReadablePath,
  searchVault,
  upsertKnowledgeCard,
} from "../lib.js";

let vault;

async function write(relativePath, content) {
  const fullPath = path.join(vault, ...relativePath.split("/"));
  await fs.promises.mkdir(path.dirname(fullPath), { recursive: true });
  await fs.promises.writeFile(fullPath, content, "utf8");
}

before(async () => {
  vault = await fs.promises.mkdtemp(path.join(os.tmpdir(), "second-brain-test-"));
  for (const directory of ["01_Sources", "02_Knowledge", "03_Topics", "04_Outputs", "05_Review", "06_Skills"]) {
    await fs.promises.mkdir(path.join(vault, directory), { recursive: true });
  }
  await write("02_Knowledge/_知识索引.md", `---
title: 知识索引
type: index
status: active
---

# 知识索引

### 第一层：核心原则
- [[可信事实]]：先验证再下结论。

### 第二层：领域
- 其他内容
`);
  await write("02_Knowledge/可信事实.md", `---
title: 可信事实
type: habit
status: active
updated: 2026-08-08
---

# 可信事实

## 当前认知

项目历史相关结论必须先检索证据，不能依靠印象。

## 证据时间线
- **2026-08-08**：测试资料。
`);
  const coreCard = (title) => `---
title: ${title}
type: decision
status: active
---

# ${title}

## 当前认知

先查第二大脑，再执行涉及历史的任务。
`;
  await write("02_Knowledge/AI幻觉反思——不确定机制时不得编造解释.md", coreCard("防幻觉"));
  await write("02_Knowledge/第二大脑全程伴随机制（实时记录监督自生长）.md", coreCard("全程伴随"));
  await write("02_Knowledge/第二大脑自生长体系搭建决策.md", coreCard("自生长决策"));
  await write("03_Topics/暖暖项目.md", `# 暖暖项目\n\n暖暖需要保持长期身份连续性。\n`);
  await write("01_Sources/原始证据.md", "password=not-a-real-test-secret\n");
});

after(async () => {
  await fs.promises.rm(vault, { recursive: true, force: true });
});

test("search ranks matching Chinese knowledge and returns bounded snippets", async () => {
  const results = await searchVault({ query: "项目历史 检索证据", scopes: ["knowledge"], limit: 3 }, vault);
  assert.equal(results[0].title, "可信事实");
  assert.ok(results[0].snippet.length <= 423);
});

test("search redacts secret-shaped values before returning snippets", async () => {
  const results = await searchVault({ query: "password", scopes: ["sources"], limit: 3 }, vault);
  assert.match(results[0].snippet, /password=\[REDACTED\]/);
  assert.doesNotMatch(results[0].snippet, /not-a-real-test-secret/);
});

test("search prioritizes active current cognition over a retired historical snapshot", async () => {
  await write("02_Knowledge/自动蒸馏历史验收.md", `---
title: WorkBuddy 第二大脑自动蒸馏出院验收
type: decision
status: retired
updated: 2026-08-31
---

# WorkBuddy 第二大脑自动蒸馏出院验收

## 当前认知

这是已经退役的自动蒸馏历史出院结论。
`);
  await write("02_Knowledge/自动蒸馏当前状态.md", `---
title: WorkBuddy 第二大脑自动蒸馏当前状态
type: decision
status: active
updated: 2026-08-31
---

# WorkBuddy 第二大脑自动蒸馏当前状态

## 当前认知

自动蒸馏当前结论为 HOLD，等待下一次自然验收。
`);

  const results = await searchVault({
    query: "WorkBuddy 第二大脑 自动蒸馏 当前出院状态",
    scopes: ["knowledge"],
    limit: 5,
  }, vault);
  assert.equal(results[0].title, "WorkBuddy 第二大脑自动蒸馏当前状态");
  assert.equal(results[0].status, "active");

  const historical = await searchVault({
    query: "自动蒸馏历史验收",
    scopes: ["knowledge"],
    limit: 5,
  }, vault);
  assert.ok(historical.some((result) => result.status === "retired"));
});

test("read allows source evidence but redacts secret-shaped values", async () => {
  const note = await readNote("01_Sources/原始证据.md", 2000, vault);
  assert.match(note.content, /password=\[REDACTED\]/);
  assert.doesNotMatch(note.content, /not-a-real-test-secret/);
});

test("read blocks traversal, hidden paths, and non-Markdown files", () => {
  assert.throws(() => resolveReadablePath("../secret.md", vault), /not allowed|escapes/i);
  assert.throws(() => resolveReadablePath(".tools/config.md", vault), /not allowed/i);
  assert.throws(() => resolveReadablePath("02_Knowledge/data.json", vault), /Markdown/i);
});

test("core context is compact and includes retrieval routing", async () => {
  const context = await buildCoreContext(vault);
  assert.match(context, /SECOND BRAIN ACTIVE/);
  assert.match(context, /second_brain_search/);
  assert.match(context, /先验证再下结论/);
  assert.ok(context.length <= 4000);
});

test("prompt memory injects only relevant top candidates", async () => {
  const context = await buildPromptMemory("暖暖长期身份", vault);
  assert.match(context, /SECOND BRAIN RELEVANT MEMORY/);
  assert.match(context, /暖暖项目/);
  assert.ok(context.length <= 2500);
});

test("upsert writes only a structured 02_Knowledge card and is idempotent", async () => {
  const input = {
    title: "MCP 写卡护栏",
    type: "decision",
    status: "active",
    confidence: "高",
    source: "测试",
    tags: ["MCP", "第二大脑", "MCP"],
    currentCognition: "写卡只允许进入知识层。",
    evidence: "临时 Vault 回归测试通过。",
  };
  const created = await upsertKnowledgeCard(input, vault);
  const updated = await upsertKnowledgeCard(input, vault);
  assert.equal(created.path, "02_Knowledge/MCP 写卡护栏.md");
  assert.equal(created.created, true);
  assert.equal(updated.created, false);
  const content = await fs.promises.readFile(path.join(vault, "02_Knowledge", "MCP 写卡护栏.md"), "utf8");
  const parsed = parseMarkdown(content);
  assert.deepEqual(parsed.frontmatter.tags, ["MCP", "第二大脑"]);
  assert.equal(parsed.frontmatter.last_reviewed, parsed.frontmatter.updated);
  assert.equal((content.match(/临时 Vault 回归测试通过/g) || []).length, 1);
});

test("capture writes only an idempotent redacted WorkBuddy inbox item", async () => {
  const input = {
    text: "收藏：测试资料 token=not-a-real-secret 验证码 123456 https://example.com?a=1&token=unsafe",
    source: "weixin-clawbot",
  };
  const created = await captureInboxItem(input, vault);
  const duplicate = await captureInboxItem(input, vault);
  assert.equal(created.created, true);
  assert.equal(duplicate.created, false);
  assert.equal(duplicate.path, created.path);
  assert.match(created.path, /^00_Inbox\/WorkBuddy\//);
  const content = await fs.promises.readFile(path.join(vault, ...created.path.split("/")), "utf8");
  assert.match(content, /token=\[REDACTED\]/);
  assert.match(content, /验证码 \[REDACTED\]/);
  assert.doesNotMatch(content, /not-a-real-secret|123456|token=unsafe/);
});

test("capture remains idempotent after the item moves out of Inbox", async () => {
  const input = {
    text: "CLAW-ARCHIVE-DEDUPE-20260810",
    source: "weixin-clawbot",
    title: "CLAW-ARCHIVE-DEDUPE-20260810",
  };
  const created = await captureInboxItem(input, vault);
  const archivedPath = path.join(vault, "05_Review", "待归档", "2026-08-10", path.basename(created.path));
  await fs.promises.mkdir(path.dirname(archivedPath), { recursive: true });
  await fs.promises.rename(path.join(vault, ...created.path.split("/")), archivedPath);

  const duplicate = await captureInboxItem(input, vault);
  assert.equal(duplicate.created, false);
  assert.equal(duplicate.path, "05_Review/待归档/2026-08-10/" + path.basename(created.path));
  const inboxMatches = (await fs.promises.readdir(path.join(vault, "00_Inbox", "WorkBuddy")))
    .filter((name) => name.endsWith(`-${duplicate.captureId}.md`));
  assert.equal(inboxMatches.length, 0);
});

test("status counts Markdown by memory scope", async () => {
  const status = await getVaultStatus(vault);
  assert.ok(status.counts.knowledge >= 6);
  assert.equal(status.counts.topics, 1);
  assert.equal(status.vault, vault);
});
