import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import config from "../packages/config.cjs";
import { initializeVault } from "../scripts/init-vault.mjs";

function invoke(script, args, vault) {
  return spawnSync(process.env.SECOND_BRAIN_POWERSHELL || "pwsh", [
    "-NoProfile", "-File", path.join(config.repoRoot, "scripts/inbox", script),
    "-VaultRoot", vault, ...args,
  ], { encoding: "utf8", windowsHide: true, timeout: 10000 });
}
function successful(result) {
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}
async function fixture(run) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "second-brain-inbox-"));
  try { await run(await initializeVault(path.join(root, "vault")), root); }
  finally { await fs.rm(root, { recursive: true, force: true }); }
}

test("explicit text capture and queue preview preserve source evidence and require Apply to move", async () => {
  await fixture(async (vault) => {
    const evidence = await fs.readFile(path.join(vault, "01_Sources/README.md"), "utf8");
    const captured = successful(invoke("Save-ClipboardToInbox.ps1", ["-Text", "第二大脑摄取夹具", "-Source", "manual"], vault));
    const original = await fs.readFile(path.join(vault, captured.path), "utf8");
    const enqueued = successful(invoke("Invoke-InboxPipeline.ps1", ["-Action", "Enqueue", "-Path", captured.path,
      "-Decision", "knowledge", "-Reason", "fixture"], vault));
    assert.equal(enqueued.action, "enqueued");
    const plan = successful(invoke("Invoke-InboxPipeline.ps1", ["-Action", "Process"], vault));
    assert.equal(plan.action, "dry-run");
    assert.equal(await fs.readFile(path.join(vault, captured.path), "utf8"), original);
    const applied = successful(invoke("Invoke-InboxPipeline.ps1", ["-Action", "Process", "-Apply"], vault));
    assert.equal(applied.action, "process");
    assert(applied.destination.startsWith("05_Review/摄取队列/待编译/knowledge/"));
    assert.equal(await fs.readFile(path.join(vault, applied.destination), "utf8"), original);
    assert.equal(await fs.readFile(path.join(vault, "01_Sources/README.md"), "utf8"), evidence);
    assert.equal((await fs.readdir(path.join(vault, "02_Knowledge"))).length, 4);
  });
});

test("queue rejects outside input and preserves a source changed after enqueue", async () => {
  await fixture(async (vault, root) => {
    const outside = path.join(root, "outside.md");
    await fs.writeFile(outside, "outside fixture\n");
    const rejected = invoke("Invoke-InboxPipeline.ps1", ["-Action", "Enqueue", "-Path", outside,
      "-Decision", "knowledge", "-Reason", "fixture"], vault);
    assert.notEqual(rejected.status, 0);
    assert.equal(await fs.readFile(outside, "utf8"), "outside fixture\n");
    const input = path.join(vault, "00_Inbox/item.md");
    await fs.writeFile(input, "first fixture\n");
    successful(invoke("Invoke-InboxPipeline.ps1", ["-Action", "Enqueue", "-Path", input,
      "-Decision", "knowledge", "-Reason", "fixture"], vault));
    await fs.writeFile(input, "changed fixture\n");
    const processed = successful(invoke("Invoke-InboxPipeline.ps1", ["-Action", "Process", "-Apply"], vault));
    assert.equal(processed.action, "failed");
    assert.equal(await fs.readFile(input, "utf8"), "changed fixture\n");
  });
});
