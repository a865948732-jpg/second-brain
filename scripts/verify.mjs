import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import config from "../packages/config.cjs";

const args = process.argv.slice(2);
if (args.some((arg) => !["--node", "--python"].includes(arg))) {
  throw new Error("Usage: node scripts/verify.mjs [--node | --python]");
}
const nodeOnly = args.includes("--node");
const pythonOnly = args.includes("--python");
if (nodeOnly && pythonOnly) throw new Error("Choose one test group or omit flags to run all groups.");
function run(command, argv, label) {
  console.log(`\n${label}`);
  const completed = spawnSync(command, argv, {
    cwd: config.repoRoot, stdio: "inherit", windowsHide: true,
    env: { ...process.env, PYTHONUTF8: "1", PYTHONDONTWRITEBYTECODE: "1" },
  });
  if (completed.error) throw completed.error;
  if (completed.status !== 0) process.exit(completed.status || 1);
}
function files(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const target = path.join(directory, entry.name);
    return entry.isDirectory() ? files(target) : [target];
  });
}
if (!pythonOnly) {
  const tests = ["packages", "hooks", "scripts", "tests"].flatMap((directory) => files(path.join(config.repoRoot, directory)))
    .filter((file) => /(?:\.test\.(?:js|mjs|cjs)|[/\\]test_[^/\\]+\.(?:mjs|cjs))$/.test(file)).sort();
  if (!tests.length) throw new Error("No Node tests found.");
  run(process.execPath, ["--test", "--test-reporter=spec", ...tests], "Node: MCP、Hook、账本和可移植运行回归");
}
if (!nodeOnly) {
  const python = process.env.SECOND_BRAIN_PYTHON || "python";
  for (const directory of ["governance", "integrations/workbuddy"]) {
    run(python, ["-B", "-m", "unittest", "discover", "-s", directory, "-p", "test_*.py"], `Python: ${directory}`);
  }
}
console.log("\n所选测试组全部通过。");
