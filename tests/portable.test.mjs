import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import config from "../packages/config.cjs";
import { initializeVault } from "../scripts/init-vault.mjs";

test("initialization preserves every existing file and refuses a nonempty vault", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "second-brain-existing-"));
  try {
    const existing = path.join(root, "keep.md");
    await fs.writeFile(existing, "preserve this file\n", "utf8");
    await assert.rejects(initializeVault(root), /new or empty directory/);
    assert.equal(await fs.readFile(existing, "utf8"), "preserve this file\n");
    assert.deepEqual(await fs.readdir(root), ["keep.md"]);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("bundled MCP starts outside the repository cwd and reads a separate Chinese-path vault", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "第二大脑 portable space-"));
  const vault = path.join(root, "示例 Vault");
  try {
    await initializeVault(vault);
    const env = { ...process.env, SECOND_BRAIN_VAULT: vault };
    delete env.SECOND_BRAIN_RUNTIME;
    const completed = spawnSync(process.execPath, [path.join(config.repoRoot, "packages/mcp/smoke.js"), "第二大脑"], {
      cwd: root, env, encoding: "utf8", windowsHide: true, timeout: 15000,
    });
    assert.equal(completed.status, 0, completed.stderr);
    const result = JSON.parse(completed.stdout);
    assert.equal(result.coreLoaded, true);
    assert.equal(result.status.vault, vault);
    assert.equal(result.tools.length, 11);
    assert(result.search.results.length > 0);
    assert.deepEqual(await fs.readdir(path.join(vault, "01_Sources")), ["README.md"]);
    assert.deepEqual(await fs.readdir(path.join(vault, "05_Review", "Daily")), []);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});
