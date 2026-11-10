import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import YAML from "yaml";
import config from "../config.cjs";

export const DEFAULT_VAULT = path.join(config.repoRoot, ".local", "vault");

const SCOPE_DIRS = Object.freeze({
  knowledge: "02_Knowledge",
  topics: "03_Topics",
  outputs: "04_Outputs",
  reviews: "05_Review",
  skills: "06_Skills",
  sources: "01_Sources",
});
const CAPTURE_DEDUP_DIRS = Object.freeze([
  "00_Inbox",
  "02_Knowledge",
  "03_Topics",
  "04_Outputs",
  "05_Review",
  "06_Skills",
]);

const READ_ROOT_FILES = new Set(["AGENTS.md", "CLAUDE.md", "知识库首页.md"]);
const CORE_INDEX = "02_Knowledge/_知识索引.md";
const CORE_CARDS = [
  "02_Knowledge/AI幻觉反思——不确定机制时不得编造解释.md",
  "02_Knowledge/第二大脑全程伴随机制（实时记录监督自生长）.md",
  "02_Knowledge/第二大脑自生长体系搭建决策.md",
];
const MAX_FILE_BYTES = 1024 * 1024;

export function getVaultRoot() {
  return config.getVaultRoot();
}

export function redactSecrets(value) {
  let text = String(value || "");
  const replacements = [
    [/\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b/g, "[REDACTED_KEY]"],
    [/\b(?:ghp|github_pat|glpat|xox[baprs])-[_A-Za-z0-9-]{12,}\b/g, "[REDACTED_TOKEN]"],
    [/\bAKIA[0-9A-Z]{16}\b/g, "[REDACTED_AWS_KEY]"],
    [/\bBearer\s+[A-Za-z0-9._~+/=-]+\b/gi, "Bearer [REDACTED]"],
    [/((?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret|session|cookie)\s*[:=]\s*["']?)[^\s,"';]+/gi, "$1[REDACTED]"],
    [/([?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|secret|session|cookie)=)[^&#\s]+/gi, "$1[REDACTED]"],
    [/((?:验证码|校验码|verification code|otp)\s*[:：]?\s*)\d{4,8}\b/gi, "$1[REDACTED]"],
  ];
  for (const [pattern, replacement] of replacements) text = text.replace(pattern, replacement);
  return text;
}

function ensureVault(root) {
  const stat = fs.statSync(root, { throwIfNoEntry: false });
  if (!stat?.isDirectory()) throw new Error(`Second-brain vault is unavailable: ${root}`);
}

function normalizeRelative(relativePath) {
  const normalized = String(relativePath || "").replace(/\\/g, "/").replace(/^\.\//, "");
  if (!normalized || path.posix.isAbsolute(normalized) || /^[A-Za-z]:/.test(normalized)) {
    throw new Error("A vault-relative Markdown path is required.");
  }
  const segments = normalized.split("/");
  if (segments.some((segment) => !segment || segment === "." || segment === ".." || segment.startsWith("."))) {
    throw new Error("Hidden paths and path traversal are not allowed.");
  }
  if (path.extname(normalized).toLowerCase() !== ".md") {
    throw new Error("Only Markdown files can be accessed.");
  }
  return normalized;
}

export function resolveReadablePath(relativePath, vaultRoot = getVaultRoot()) {
  ensureVault(vaultRoot);
  const normalized = normalizeRelative(relativePath);
  const first = normalized.split("/")[0];
  if (!Object.values(SCOPE_DIRS).includes(first) && !READ_ROOT_FILES.has(normalized)) {
    throw new Error("Path is outside the readable knowledge areas.");
  }
  const resolved = config.resolveWithinVault(vaultRoot, normalized);
  const fromRoot = path.relative(vaultRoot, resolved);
  if (!fromRoot || fromRoot.startsWith("..") || path.isAbsolute(fromRoot)) {
    throw new Error("Path escapes the vault.");
  }
  return { normalized, resolved };
}

export function parseMarkdown(markdown) {
  const text = String(markdown || "").replace(/^\uFEFF/, "");
  const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
  let frontmatter = {};
  if (match) {
    const parsed = YAML.parse(match[1]);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) frontmatter = parsed;
  }
  const body = match ? text.slice(match[0].length) : text;
  return { frontmatter, body };
}

function markdownTitle(parsed, fallback) {
  if (typeof parsed.frontmatter.title === "string" && parsed.frontmatter.title.trim()) {
    return parsed.frontmatter.title.trim();
  }
  const heading = parsed.body.match(/^#\s+(.+)$/m);
  return heading ? heading[1].trim() : fallback;
}

function extractSection(markdown, heading) {
  const lines = String(markdown || "").split(/\r?\n/);
  const start = lines.findIndex((line) => line.trim() === `## ${heading}`);
  if (start < 0) return "";
  let end = lines.length;
  for (let index = start + 1; index < lines.length; index += 1) {
    if (/^##\s+/.test(lines[index])) {
      end = index;
      break;
    }
  }
  return lines.slice(start + 1, end).join("\n").trim();
}

function compactText(value, maxLength) {
  const compacted = redactSecrets(value)
    .replace(/\r/g, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return compacted.length > maxLength ? `${compacted.slice(0, maxLength)}...` : compacted;
}

async function walkMarkdown(directory, limit = 5000) {
  const files = [];
  const info = await fs.promises.lstat(directory).catch((error) => {
    if (error.code === "ENOENT") return null;
    throw error;
  });
  if (!info || info.isSymbolicLink()) return files;
  async function walk(current) {
    if (files.length >= limit) return;
    let entries;
    try {
      entries = await fs.promises.readdir(current, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (files.length >= limit) break;
      if (entry.name.startsWith(".")) continue;
      const fullPath = path.join(current, entry.name);
      if (entry.isDirectory()) await walk(fullPath);
      else if (entry.isFile() && path.extname(entry.name).toLowerCase() === ".md") files.push(fullPath);
    }
  }
  await walk(directory);
  return files;
}

function queryTerms(query) {
  const raw = String(query || "").toLocaleLowerCase().match(/[\p{L}\p{N}_-]+/gu) || [];
  const expanded = [];
  for (const term of raw) {
    if (term.length > 1) expanded.push(term);
    if (/^[\p{Script=Han}]+$/u.test(term)) {
      const chars = [...term];
      for (let index = 0; index < chars.length - 1; index += 1) {
        expanded.push(chars.slice(index, index + 2).join(""));
      }
    }
  }
  return [...new Set(expanded)].slice(0, 24);
}

function snippetFor(text, terms, maxLength = 420) {
  const lower = text.toLocaleLowerCase();
  const indexes = terms.map((term) => lower.indexOf(term)).filter((index) => index >= 0);
  const first = indexes.length ? Math.min(...indexes) : 0;
  const start = Math.max(0, first - Math.floor(maxLength / 3));
  return compactText(text.slice(start, start + maxLength), maxLength);
}

const STATUS_SCORE_ADJUSTMENTS = Object.freeze({
  active: 12,
  disputed: 4,
  draft: 0,
  retired: -24,
});

export async function searchVault({ query, scopes = ["knowledge", "topics"], limit = 5 } = {}, vaultRoot = getVaultRoot()) {
  ensureVault(vaultRoot);
  const phrase = String(query || "").trim().toLocaleLowerCase();
  const terms = queryTerms(phrase);
  if (!phrase || terms.length === 0) return [];
  const selectedScopes = [...new Set(scopes)].filter((scope) => SCOPE_DIRS[scope]);
  if (selectedScopes.length === 0) throw new Error("At least one valid search scope is required.");

  const files = [];
  for (const scope of selectedScopes) {
    files.push(...await walkMarkdown(path.join(vaultRoot, SCOPE_DIRS[scope])));
  }

  const results = [];
  for (const filePath of files) {
    let stat;
    try {
      stat = await fs.promises.stat(filePath);
      if (stat.size > MAX_FILE_BYTES) continue;
      const text = await fs.promises.readFile(filePath, "utf8");
      const parsed = parseMarkdown(text);
      const relativePath = path.relative(vaultRoot, filePath).replace(/\\/g, "/");
      const title = markdownTitle(parsed, path.basename(filePath, ".md"));
      const titleLower = title.toLocaleLowerCase();
      const fileLower = relativePath.toLocaleLowerCase();
      const bodyLower = parsed.body.toLocaleLowerCase();
      let score = 0;
      if (titleLower.includes(phrase)) score += 18;
      if (bodyLower.includes(phrase)) score += 10;
      for (const term of terms) {
        if (titleLower.includes(term)) score += 8;
        if (fileLower.includes(term)) score += 5;
        const occurrences = bodyLower.split(term).length - 1;
        score += Math.min(occurrences, 6) * 1.5;
      }
      if (score <= 0) continue;
      const status = String(parsed.frontmatter.status || "").toLocaleLowerCase();
      score += STATUS_SCORE_ADJUSTMENTS[status] ?? 0;
      results.push({
        path: relativePath,
        title,
        type: String(parsed.frontmatter.type || ""),
        status,
        updated: String(parsed.frontmatter.updated || ""),
        score: Math.round(score * 10) / 10,
        snippet: redactSecrets(snippetFor(parsed.body, terms)),
        modifiedAt: stat.mtime.toISOString(),
      });
    } catch {
      // A changing or malformed note should not break retrieval of other notes.
    }
  }
  return results
    .sort((a, b) => b.score - a.score || b.modifiedAt.localeCompare(a.modifiedAt))
    .slice(0, Math.max(1, Math.min(Number(limit) || 5, 20)));
}

export async function readNote(relativePath, maxChars = 20000, vaultRoot = getVaultRoot()) {
  const { normalized, resolved } = resolveReadablePath(relativePath, vaultRoot);
  const stat = await fs.promises.stat(resolved);
  if (!stat.isFile() || stat.size > MAX_FILE_BYTES) throw new Error("Note is unavailable or too large.");
  const content = redactSecrets(await fs.promises.readFile(resolved, "utf8"));
  const limit = Math.max(500, Math.min(Number(maxChars) || 20000, 100000));
  return { path: normalized, content: content.slice(0, limit), truncated: content.length > limit };
}

function firstLayerFromIndex(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const start = lines.findIndex((line) => line.startsWith("### 第一层"));
  if (start < 0) return "";
  let end = lines.length;
  for (let index = start + 1; index < lines.length; index += 1) {
    if (lines[index].startsWith("### 第二层")) {
      end = index;
      break;
    }
  }
  return lines.slice(start, end).join("\n").trim();
}

export async function buildCoreContext(vaultRoot = getVaultRoot()) {
  ensureVault(vaultRoot);
  const sections = [];
  try {
    const index = await readNote(CORE_INDEX, 12000, vaultRoot);
    const core = firstLayerFromIndex(index.content);
    if (core) sections.push(compactText(core, 1500));
  } catch {}

  for (const relativePath of CORE_CARDS) {
    try {
      const note = await readNote(relativePath, 12000, vaultRoot);
      const parsed = parseMarkdown(note.content);
      const cognition = extractSection(parsed.body, "当前认知");
      if (cognition) sections.push(`### ${markdownTitle(parsed, path.basename(relativePath, ".md"))}\n${compactText(cognition, 520)}`);
    } catch {}
  }

  const body = compactText(sections.join("\n\n"), 3300);
  return [
    "SECOND BRAIN ACTIVE",
    "Treat the Obsidian vault as long-term memory, not as unquestionable truth. Current user instructions and current project files take precedence.",
    "Before claims or actions involving user preferences, project history, prior decisions, tools, or recurring workflows, use second_brain_search; use second_brain_read before relying on a result. Mark conflicts or stale facts instead of silently choosing one.",
    body,
  ].filter(Boolean).join("\n\n");
}

export async function buildPromptMemory(query, vaultRoot = getVaultRoot()) {
  const results = await searchVault({ query, scopes: ["knowledge", "topics"], limit: 3 }, vaultRoot);
  if (results.length === 0) return "";
  const lines = results.map((result, index) =>
    `${index + 1}. [[${result.title}]] (${result.path}, score ${result.score})\n${compactText(result.snippet, 500)}`
  );
  return compactText([
    "SECOND BRAIN RELEVANT MEMORY",
    "These are retrieval candidates, not automatic truth. Read the full note through second_brain_read before using a consequential claim.",
    ...lines,
  ].join("\n\n"), 2400);
}

function safeFilename(title) {
  const cleaned = String(title || "")
    .normalize("NFKC")
    .replace(/[<>:"/\\|?*\x00-\x1F]/g, "-")
    .replace(/[. ]+$/g, "")
    .trim();
  if (!cleaned || cleaned.length > 120 || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(cleaned)) {
    throw new Error("Title cannot be converted to a safe knowledge-card filename.");
  }
  return cleaned;
}

function replaceSection(body, heading, replacement) {
  const lines = String(body || "").split(/\r?\n/);
  const start = lines.findIndex((line) => line.trim() === `## ${heading}`);
  if (start < 0) return `${body.trimEnd()}\n\n## ${heading}\n\n${replacement.trim()}\n`;
  let end = lines.length;
  for (let index = start + 1; index < lines.length; index += 1) {
    if (/^##\s+/.test(lines[index])) {
      end = index;
      break;
    }
  }
  return [
    ...lines.slice(0, start),
    `## ${heading}`,
    "",
    replacement.trim(),
    "",
    ...lines.slice(end),
  ].join("\n").replace(/\n{3,}/g, "\n\n").trimEnd() + "\n";
}

function renderMarkdown(frontmatter, body) {
  return `---\n${YAML.stringify(frontmatter, { lineWidth: 0 }).trimEnd()}\n---\n\n${body.trim()}\n`;
}

function firstUrl(value) {
  const match = String(value || "").match(/https?:\/\/[^\s<>"']+/i);
  return match ? match[0].replace(/[.,;:!?)\]}，。；：！？）】]+$/u, "") : "";
}

function captureTitle(value, explicitTitle, url) {
  if (String(explicitTitle || "").trim()) return String(explicitTitle).trim().slice(0, 80);
  const text = String(value || "").trim();
  if (url && text === url) {
    try {
      return new URL(url).hostname;
    } catch {
      return "链接收件";
    }
  }
  const line = text.split(/\r?\n/).find((item) => item.trim()) || "微信收件";
  const withoutUrl = line.replace(/https?:\/\/[^\s<>"']+/gi, "").replace(/\s+/g, " ").trim();
  return (withoutUrl || "链接收件").slice(0, 60);
}

function localTimestamp(date) {
  const pad = (value, length = 2) => String(value).padStart(length, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}-${pad(date.getMilliseconds(), 3)}`;
}

export async function captureInboxItem(input, vaultRoot = getVaultRoot()) {
  ensureVault(vaultRoot);
  const originalText = String(input.text || "").trim();
  if (!originalText) throw new Error("Capture text is required.");

  const sourceLabels = {
    "weixin-clawbot": "微信 ClawBot",
    workbuddy: "WorkBuddy Windows",
    qq: "QQ",
    manual: "手动收件",
  };
  const source = input.source || "workbuddy";
  if (!sourceLabels[source]) throw new Error("Unsupported capture source.");

  const safeText = redactSecrets(originalText);
  const detectedUrl = firstUrl(safeText);
  const sourceUrl = redactSecrets(String(input.sourceUrl || detectedUrl).trim());
  const title = captureTitle(safeText, input.title, sourceUrl);
  const captureId = crypto
    .createHash("sha256")
    .update(`${source}\0${safeText}`, "utf8")
    .digest("hex")
    .slice(0, 12);
  const directory = config.resolveWithinVault(vaultRoot, "00_Inbox/WorkBuddy");
  await fs.promises.mkdir(directory, { recursive: true });

  const existingPath = await findCapturedPath(captureId, vaultRoot);
  if (existingPath) {
    return {
      path: existingPath,
      captureId,
      created: false,
      redacted: safeText !== originalText || sourceUrl !== String(input.sourceUrl || detectedUrl).trim(),
      gitCommitRequired: false,
    };
  }

  const now = new Date();
  const filename = `${localTimestamp(now)}-${safeFilename(title).slice(0, 60)}-${captureId}.md`;
  const relativePath = `00_Inbox/WorkBuddy/${filename}`;
  const target = path.join(directory, filename);
  const frontmatter = {
    title,
    type: "inbox",
    status: "pending",
    created: now.toISOString().slice(0, 10),
    updated: now.toISOString().slice(0, 10),
    source: sourceLabels[source],
    source_url: sourceUrl,
    captured_by: source === "weixin-clawbot" ? "workbuddy-claw" : "workbuddy",
    content_kind: sourceUrl && safeText === sourceUrl ? "link" : "note",
    decision: "pending",
    tags: ["mobile-capture", source],
    confidence: "低",
    redacted: safeText !== originalText || sourceUrl !== String(input.sourceUrl || detectedUrl).trim(),
    capture_id: captureId,
  };
  const body = `# ${title}\n\n## 原始内容\n\n${safeText}\n\n## 处理备注\n\n- 可能关联：\n- 待确认：`;
  await fs.promises.writeFile(target, renderMarkdown(frontmatter, body), { encoding: "utf8", flag: "wx" });
  return {
    path: relativePath,
    captureId,
    created: true,
    redacted: frontmatter.redacted,
    gitCommitRequired: true,
  };
}

async function findCapturedPath(captureId, vaultRoot) {
  const suffix = `-${captureId}.md`;
  for (const directory of CAPTURE_DEDUP_DIRS) {
    const files = await walkMarkdown(path.join(vaultRoot, directory));
    const existing = files.find((file) => path.basename(file).endsWith(suffix));
    if (existing) return path.relative(vaultRoot, existing).split(path.sep).join("/");
  }
  return null;
}

export async function upsertKnowledgeCard(input, vaultRoot = getVaultRoot()) {
  ensureVault(vaultRoot);
  const title = String(input.title || "").trim();
  const filename = `${safeFilename(input.filename || title)}.md`;
  const relativePath = `02_Knowledge/${filename}`;
  const { resolved } = resolveReadablePath(relativePath, vaultRoot);
  const today = new Date().toISOString().slice(0, 10);
  const existing = await fs.promises.readFile(resolved, "utf8").catch((error) => {
    if (error.code === "ENOENT") return "";
    throw error;
  });
  const parsed = parseMarkdown(existing);
  const frontmatter = {
    ...parsed.frontmatter,
    title,
    type: input.type,
    status: input.status,
    confidence: input.confidence,
    created: parsed.frontmatter.created || today,
    updated: today,
    last_reviewed: today,
    source: input.source,
    tags: [...new Set((input.tags || []).map((tag) => String(tag).trim()).filter(Boolean))],
  };
  let body = parsed.body.trim() || `# ${title}`;
  if (!/^#\s+/m.test(body)) body = `# ${title}\n\n${body}`;
  body = replaceSection(body, "当前认知", redactSecrets(input.currentCognition));
  const evidence = `- **${today}**：${redactSecrets(input.evidence).replace(/\s+/g, " ").trim()}`;
  const timeline = extractSection(body, "证据时间线");
  if (!timeline.includes(evidence)) body = replaceSection(body, "证据时间线", [timeline, evidence].filter(Boolean).join("\n"));

  await fs.promises.mkdir(path.dirname(resolved), { recursive: true });
  await fs.promises.writeFile(resolved, renderMarkdown(frontmatter, body), "utf8");
  return { path: relativePath, created: !existing, updated: true, gitCommitRequired: true };
}

export async function getVaultStatus(vaultRoot = getVaultRoot()) {
  ensureVault(vaultRoot);
  const counts = {};
  let latestModified = null;
  for (const [scope, directory] of Object.entries(SCOPE_DIRS)) {
    const files = await walkMarkdown(path.join(vaultRoot, directory));
    counts[scope] = files.length;
    for (const file of files) {
      try {
        const modified = (await fs.promises.stat(file)).mtime.toISOString();
        if (!latestModified || modified > latestModified) latestModified = modified;
      } catch {}
    }
  }
  return { vault: vaultRoot, counts, latestModified };
}

export const searchScopes = Object.keys(SCOPE_DIRS);
