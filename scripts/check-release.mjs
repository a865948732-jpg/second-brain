import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import YAML from "yaml";
import config from "../packages/config.cjs";

const excluded = new Set([".git", ".local", "node_modules", "__pycache__", ".venv", ".pytest_cache", "coverage"]);
function walk(directory, relative = "") {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    if (excluded.has(entry.name)) return [];
    const key = path.posix.join(relative, entry.name);
    return entry.isDirectory() ? walk(path.join(directory, entry.name), key) : [key];
  });
}
let files;
if (fs.existsSync(path.join(config.repoRoot, ".git"))) {
  const listed = spawnSync("git", ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], {
    cwd: config.repoRoot, encoding: "utf8", windowsHide: true,
  });
  if (listed.status !== 0) throw new Error("Git file inventory failed.");
  files = [...new Set(listed.stdout.split("\0").filter(Boolean))];
} else {
  files = walk(config.repoRoot);
}
files.sort();
const findings = [];
const report = (file, line, type) => findings.push({ file, line, type });
const credentialPatterns = [
  ["key_shape", /\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b/g],
  ["github_token_shape", /\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b/g],
  ["aws_key_shape", /\bAKIA[0-9A-Z]{16}\b/g],
  ["jwt_shape", /\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b/g],
  ["private_key_block", /-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{64,}-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----/g],
];
const markdown = new Map();
for (const file of files) {
  if (/^(?:\.local|vault|00_Inbox|01_Sources|02_Knowledge|05_Review|node_modules)(?:\/|$)/.test(file)
      || /\.(?:db(?:-.+)?|sqlite3?(?:-.+)?|jsonl|pem|key|pfx|p12|zip|7z)$/i.test(file)
      || (/(?:^|\/)\.env(?:\..+)?$/.test(file) && file !== ".env.example")
      || /(?:source[-_]snapshot|checkpoint)\.(?:json|md)$/i.test(path.basename(file))) {
    report(file, 1, "private_or_generated_file");
    continue;
  }
  const target = path.resolve(config.repoRoot, file);
  if (fs.lstatSync(target).isSymbolicLink()) {
    report(file, 1, "linked_publication_file");
    continue;
  }
  const bytes = fs.readFileSync(target);
  if (bytes.includes(0)) { report(file, 1, "unexpected_binary_file"); continue; }
  const text = bytes.toString("utf8");
  if (/\.(?:json|yml|yaml)$/.test(file)) {
    try { file.endsWith(".json") ? JSON.parse(text) : YAML.parse(text); }
    catch { report(file, 1, "invalid_configuration_syntax"); }
  }
  for (const [type, pattern] of credentialPatterns) {
    for (const match of text.matchAll(pattern)) {
      // Known synthetic fixtures remain testable; never print the matched value.
      const context = text.slice(Math.max(0, match.index - 100), match.index + match[0].length + 100);
      const synthetic = /fixture|synthetic|testonly|fake|dummy|placeholder/i.test(match[0])
        || (/\.test\.|(?:^|\/)test_/.test(file) && /fixture|synthetic|fake|testonly/i.test(context));
      if (!synthetic) report(file, text.slice(0, match.index).split("\n").length, type);
    }
  }
  for (const match of text.matchAll(/[A-Z]:[\\/]{1,2}Users[\\/]{1,2}[A-Za-z0-9_.-]+/g)) {
    report(file, text.slice(0, match.index).split("\n").length, "personal_absolute_path");
  }
  if (file.endsWith(".md")) markdown.set(file, text);
}
function anchors(text) {
  const counts = new Map();
  const result = new Set();
  for (const match of text.matchAll(/^#{1,6}\s+(.+)$/gm)) {
    const slug = match[1].trim().replace(/\s+#+$/, "").toLowerCase()
      .replace(/[^\p{L}\p{N}_\s-]/gu, "").replace(/\s/g, "-");
    const count = counts.get(slug) || 0;
    result.add(count ? `${slug}-${count}` : slug);
    counts.set(slug, count + 1);
  }
  return result;
}
for (const [file, text] of markdown) {
  for (const match of text.matchAll(/(?<!!)\[[^\]\n]+\]\((<[^>]+>|[^)\s]+)\)/g)) {
    const href = match[1].replace(/^<|>$/g, "");
    if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(href) || href.startsWith("//")) continue;
    const [relative, hash] = href.split("#");
    const target = path.resolve(config.repoRoot, path.dirname(file), decodeURIComponent(relative || path.basename(file)));
    const line = text.slice(0, match.index).split("\n").length;
    const fromRoot = path.relative(config.repoRoot, target);
    if (fromRoot.startsWith("..") || !fs.existsSync(target)) { report(file, line, "broken_local_link"); continue; }
    if (hash && target.endsWith(".md") && !anchors(fs.readFileSync(target, "utf8")).has(decodeURIComponent(hash))) {
      report(file, line, "broken_heading_link");
    }
  }
}
const packageJson = JSON.parse(fs.readFileSync(path.join(config.repoRoot, "package.json"), "utf8"));
const lock = JSON.parse(fs.readFileSync(path.join(config.repoRoot, "package-lock.json"), "utf8"));
if (packageJson.license !== "MIT" || lock.packages[""].license !== "MIT"
    || !fs.readFileSync(path.join(config.repoRoot, "LICENSE"), "utf8").startsWith("MIT License")) {
  report("LICENSE", 1, "license_inconsistent");
}
if (JSON.stringify(packageJson.dependencies) !== JSON.stringify(lock.packages[""].dependencies)) {
  report("package-lock.json", 1, "dependency_lock_mismatch");
}
console.log(JSON.stringify({ filesChecked: files.length, findings, passed: findings.length === 0 }, null, 2));
if (findings.length) process.exitCode = 1;
