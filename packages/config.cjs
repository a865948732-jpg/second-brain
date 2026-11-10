const path = require("node:path");
const os = require("node:os");
const fs = require("node:fs");

const repoRoot = path.resolve(__dirname, "..");

function getVaultRoot() {
  return path.resolve(process.env.SECOND_BRAIN_VAULT || path.join(repoRoot, ".local", "vault"));
}

function getRuntimePath() {
  return path.resolve(process.env.SECOND_BRAIN_RUNTIME || path.join(__dirname, "runtime", "runtime.mjs"));
}

function getCodexHome() {
  return path.resolve(process.env.CODEX_HOME || path.join(os.homedir(), ".codex"));
}

function resolveWithinVault(vaultRoot, relativePath) {
  const root = path.resolve(vaultRoot || getVaultRoot());
  const resolved = path.resolve(root, relativePath);
  const relative = path.relative(root, resolved);
  if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error("Path escapes the vault.");
  }
  let current = root;
  for (const segment of relative.split(path.sep)) {
    current = path.join(current, segment);
    const info = fs.lstatSync(current, { throwIfNoEntry: false });
    if (info?.isSymbolicLink()) {
      throw new Error("Symbolic links inside the vault are not allowed.");
    }
  }
  return resolved;
}

module.exports = { repoRoot, getVaultRoot, getRuntimePath, getCodexHome, resolveWithinVault };
