import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { readNote, upsertKnowledgeCard, captureInboxItem } from "../packages/mcp/lib.js";
import { startTask } from "../packages/runtime/runtime.mjs";

async function withLinkedDirectory(relative, run) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "second-brain-link-"));
  try {
    const vault = path.join(root, "vault");
    const outside = path.join(root, "outside-fixture");
    await fs.mkdir(vault);
    await fs.mkdir(outside);
    const link = path.join(vault, relative);
    await fs.mkdir(path.dirname(link), { recursive: true });
    await fs.symlink(outside, link, process.platform === "win32" ? "junction" : "dir");
    await run(vault, outside);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
}

test("MCP reads reject a linked directory inside the Vault", async () => {
  await withLinkedDirectory("02_Knowledge/linked", async (vault, outside) => {
    await fs.writeFile(path.join(outside, "outside.md"), "synthetic outside fixture\n");
    await assert.rejects(readNote("02_Knowledge/linked/outside.md", 500, vault), /Symbolic links/);
  });
});

test("knowledge writes preserve files beyond a linked knowledge directory", async () => {
  await withLinkedDirectory("02_Knowledge", async (vault, outside) => {
    await assert.rejects(upsertKnowledgeCard({
      title: "隔离验证", type: "knowledge", status: "active", confidence: "高", source: "fixture",
      currentCognition: "仅用于路径边界测试", evidence: "fixture", tags: [],
    }, vault), /Symbolic links/);
    assert.deepEqual(await fs.readdir(outside), []);
  });
});

test("capture cannot create a note through a linked Inbox directory", async () => {
  await withLinkedDirectory("00_Inbox/WorkBuddy", async (vault, outside) => {
    await assert.rejects(captureInboxItem({ text: "path-isolation fixture", source: "manual" }, vault), /Symbolic links/);
    assert.deepEqual(await fs.readdir(outside), []);
  });
});

test("the runtime cannot create a ledger through a linked Review directory", async () => {
  await withLinkedDirectory("05_Review", async (vault, outside) => {
    await assert.rejects(startTask({ taskId: "linked-ledger", goal: "fixture", vaultRoot: vault }), /Symbolic links/);
    assert.deepEqual(await fs.readdir(outside), []);
  });
});
