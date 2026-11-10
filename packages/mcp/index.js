#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { z } from "zod";
import config from "../config.cjs";

import {
  buildCoreContext,
  captureInboxItem,
  getVaultStatus,
  readNote,
  searchScopes,
  searchVault,
  upsertKnowledgeCard,
} from "./lib.js";

const runtime = await import(pathToFileURL(
  config.getRuntimePath(),
).href);
const runtimeVault = config.getVaultRoot();

const server = new McpServer({ name: "second-brain", version: "1.0.0" });
const scopeSchema = z.enum(searchScopes);
const scoreDimension = z.number().min(0).max(1);
const scoreCandidateSchema = z.object({
  id: z.string().max(200).optional(),
  name: z.string().max(500).optional(),
  title: z.string().max(500).optional(),
  action: z.string().max(1000).optional(),
  description: z.string().max(1000).optional(),
  evidence: scoreDimension.optional(),
  goalFit: scoreDimension.optional(),
  verifiability: scoreDimension.optional(),
  reversibility: scoreDimension.optional(),
  historicalSuccess: scoreDimension.optional(),
  risk: scoreDimension.optional(),
  uncertainty: scoreDimension.optional(),
  irreversibleCost: scoreDimension.optional(),
  safe: z.boolean(),
}).strict();

function textResult(text, structuredContent) {
  return { content: [{ type: "text", text }], structuredContent };
}

function errorResult(error) {
  const message = error instanceof Error ? error.message : String(error);
  return { isError: true, content: [{ type: "text", text: `Second-brain error: ${message}` }] };
}

server.registerPrompt(
  "second-brain",
  {
    title: "Second brain context",
    description: "Load the compact long-term-memory rules and core knowledge from the local Obsidian vault.",
  },
  async () => ({
    messages: [{ role: "user", content: { type: "text", text: await buildCoreContext() } }],
  }),
);

server.registerResource(
  "second-brain-core",
  "second-brain://core",
  { title: "Second-brain core context", description: "Compact always-on memory context.", mimeType: "text/markdown" },
  async () => ({
    contents: [{ uri: "second-brain://core", mimeType: "text/markdown", text: await buildCoreContext() }],
  }),
);

server.registerResource(
  "second-brain-index",
  "second-brain://index",
  { title: "Second-brain knowledge index", description: "The Obsidian knowledge routing index.", mimeType: "text/markdown" },
  async () => {
    const note = await readNote("02_Knowledge/_知识索引.md", 100000);
    return { contents: [{ uri: "second-brain://index", mimeType: "text/markdown", text: note.content }] };
  },
);

server.registerTool(
  "second_brain_search",
  {
    title: "Search second brain",
    description: "Search long-term memory before answering or acting on user preferences, project history, prior decisions, tools, or recurring workflows. Results are candidates; read the note before consequential use.",
    inputSchema: {
      query: z.string().min(2).max(500).describe("Natural-language query or keywords."),
      scopes: z.array(scopeSchema).max(6).optional().describe("Default: knowledge and topics. reviews and sources are opt-in."),
      limit: z.number().int().min(1).max(20).optional().describe("Maximum results; default 5."),
    },
    outputSchema: {
      query: z.string(),
      results: z.array(z.object({
        path: z.string(),
        title: z.string(),
        type: z.string(),
        status: z.string(),
        updated: z.string(),
        score: z.number(),
        snippet: z.string(),
      })),
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  async ({ query, scopes, limit }) => {
    try {
      const results = await searchVault({ query, scopes, limit });
      const visible = results.map(({ modifiedAt: _modifiedAt, ...result }) => result);
      const structuredContent = { query, results: visible };
      return textResult(JSON.stringify(structuredContent, null, 2), structuredContent);
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "second_brain_read",
  {
    title: "Read second-brain note",
    description: "Read a Markdown note returned by second_brain_search. Hidden tool/config paths are blocked; 01_Sources is readable but never writable.",
    inputSchema: {
      path: z.string().min(4).max(500).describe("Vault-relative Markdown path."),
      maxChars: z.number().int().min(500).max(100000).optional(),
    },
    outputSchema: { path: z.string(), content: z.string(), truncated: z.boolean() },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  async ({ path, maxChars }) => {
    try {
      const note = await readNote(path, maxChars);
      return textResult(note.content, note);
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "second_brain_core_context",
  {
    title: "Load core second-brain context",
    description: "Return the compact core-principles context used by the SessionStart hook.",
    inputSchema: {},
    outputSchema: { context: z.string() },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  async () => {
    try {
      const context = await buildCoreContext();
      return textResult(context, { context });
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "second_brain_status",
  {
    title: "Inspect second-brain status",
    description: "Report readable Markdown counts and the latest modification time without changing the vault.",
    inputSchema: {},
    outputSchema: {
      vault: z.string(),
      counts: z.record(z.number()),
      latestModified: z.string().nullable(),
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  async () => {
    try {
      const status = await getVaultStatus();
      return textResult(JSON.stringify(status, null, 2), status);
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "second_brain_capture",
  {
    title: "Capture an explicit second-brain inbox item",
    description: "Save text explicitly marked by the user for retention into 00_Inbox/WorkBuddy. The tool redacts common credentials, is idempotent by content, and cannot write the evidence or knowledge layers.",
    inputSchema: {
      text: z.string().min(1).max(20000).describe("The exact user-selected text to retain."),
      source: z.enum(["weixin-clawbot", "workbuddy", "qq", "manual"]).optional(),
      title: z.string().min(1).max(80).optional(),
      sourceUrl: z.string().max(2000).optional(),
    },
    outputSchema: {
      path: z.string(),
      captureId: z.string(),
      created: z.boolean(),
      redacted: z.boolean(),
      gitCommitRequired: z.boolean(),
    },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  async (input) => {
    try {
      const result = await captureInboxItem(input);
      return textResult(
        result.created ? `Captured ${result.path}. A focused vault git commit is required.` : `Already captured as ${result.path}.`,
        result,
      );
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "second_brain_upsert_card",
  {
    title: "Upsert second-brain knowledge card",
    description: "Create or update one structured card in 02_Knowledge. It cannot delete, move, or write 01_Sources. After success, update the vault index/log as needed and make a focused git commit.",
    inputSchema: {
      title: z.string().min(2).max(120),
      filename: z.string().min(2).max(120).optional().describe("Optional filename without .md; defaults to title."),
      type: z.enum(["knowledge", "decision", "habit", "project", "topic", "comparison"]),
      status: z.enum(["active", "disputed", "draft"]),
      confidence: z.enum(["高", "中", "低"]),
      source: z.string().min(2).max(500),
      tags: z.array(z.string().min(1).max(50)).max(20),
      currentCognition: z.string().min(2).max(10000),
      evidence: z.string().min(2).max(5000),
    },
    outputSchema: {
      path: z.string(),
      created: z.boolean(),
      updated: z.boolean(),
      gitCommitRequired: z.boolean(),
    },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  async (input) => {
    try {
      const result = await upsertKnowledgeCard(input);
      return textResult(
        `${result.created ? "Created" : "Updated"} ${result.path}. A focused vault git commit is required.`,
        result,
      );
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "second_brain_task_start",
  {
    title: "Start second-brain task envelope",
    description: "Append an auditable task-start envelope with authority and retrieval receipt. It never writes 01_Sources or promotes knowledge.",
    inputSchema: {
      taskId: z.string().min(2).max(120),
      goal: z.string().min(2).max(4000),
      taskType: z.string().max(120).optional(),
      project: z.string().max(160).optional(),
      authority: z.string().max(300).optional(),
      retrieval: z.object({
        query: z.string().max(500).optional(),
        candidates: z.array(z.string().max(300)).max(20).optional(),
        receipt: z.string().max(120).optional(),
      }).optional(),
      source: z.string().max(80).optional(),
    },
    outputSchema: z.object({ created: z.boolean(), taskId: z.string(), record: z.record(z.any()) }),
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  async (input) => {
    try {
      const result = await runtime.startTask({ ...input, vaultRoot: runtimeVault });
      return textResult(JSON.stringify(result, null, 2), result);
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "second_brain_record_event",
  {
    title: "Record second-brain task event",
    description: "Append one redacted action, result, failure, verification, or checkpoint event to the current task ledger.",
    inputSchema: {
      taskId: z.string().min(2).max(120),
      eventType: z.string().regex(/^[a-z][a-z0-9_]{2,60}$/),
      event: z.record(z.any()).optional(),
    },
    outputSchema: z.object({ created: z.boolean(), taskId: z.string(), record: z.record(z.any()) }),
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
  },
  async ({ taskId, eventType, event }) => {
    try {
      const result = await runtime.recordEvent({ taskId, eventType, event, vaultRoot: runtimeVault });
      return textResult(JSON.stringify(result, null, 2), result);
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "second_brain_task_finish",
  {
    title: "Finish second-brain task with reflection",
    description: "Append outcome, verification, reflection, and an optional lesson candidate. Unverified results remain candidates and cannot be promoted automatically.",
    inputSchema: {
      taskId: z.string().min(2).max(120),
      outcome: z.string().min(2).max(120),
      result: z.record(z.any()).optional(),
      verification: z.record(z.any()).optional(),
      reflection: z.string().min(2).max(3000),
      lessonCandidate: z.object({
        errorSignature: z.string().max(160),
        lesson: z.string().max(2000),
        nextChange: z.string().max(1000).optional(),
      }).nullable().optional(),
      nextChange: z.string().max(1000).optional(),
      stopReason: z.string().max(500).optional(),
      source: z.string().max(80).optional(),
    },
    outputSchema: z.object({ created: z.boolean(), taskId: z.string(), record: z.record(z.any()) }),
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  async (input) => {
    try {
      const result = await runtime.finishTask({ ...input, vaultRoot: runtimeVault });
      return textResult(JSON.stringify(result, null, 2), result);
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "second_brain_recall_experience",
  {
    title: "Recall verified second-brain experiences",
    description: "Retrieve prior task outcomes and lesson candidates by query, task type, project, outcome, or error signature. Results are candidates with evidence levels and a receipt.",
    inputSchema: {
      query: z.string().max(500).optional(),
      taskType: z.string().max(120).optional(),
      project: z.string().max(160).optional(),
      outcome: z.string().max(120).optional(),
      errorSignature: z.string().max(160).optional(),
      limit: z.number().int().min(1).max(50).optional(),
    },
    outputSchema: z.object({ receipt: z.string(), results: z.array(z.record(z.any())) }),
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  async (input) => {
    try {
      const result = await runtime.recallEpisodes({ ...input, vaultRoot: runtimeVault });
      return textResult(JSON.stringify(result, null, 2), result);
    } catch (error) {
      return errorResult(error);
    }
  },
);

server.registerTool(
  "second_brain_score_candidates",
  {
    title: "Score second-brain action candidates",
    description: "Rank candidates only when safe:boolean and bounded numeric evidence fields are explicit; invalid or unsafe candidates are held.",
    inputSchema: { candidates: z.array(scoreCandidateSchema).min(1).max(30) },
    outputSchema: z.object({ results: z.array(z.record(z.any())) }),
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  async ({ candidates }) => {
    try {
      const results = runtime.scoreCandidates(candidates);
      const structuredContent = { results };
      return textResult(JSON.stringify(structuredContent, null, 2), structuredContent);
    } catch (error) {
      return errorResult(error);
    }
  },
);

await server.connect(new StdioServerTransport());
