#!/usr/bin/env node
import config from "../config.cjs";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const query = process.argv.slice(2).join(" ").trim() || "第二大脑 全程伴随";
const client = new Client({ name: "second-brain-smoke", version: "1.0.0" });
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [fileURLToPath(new URL("./index.js", import.meta.url))],
  env: { ...process.env, SECOND_BRAIN_VAULT: process.env.SECOND_BRAIN_VAULT || config.getVaultRoot() },
  stderr: "pipe",
});

try {
  await client.connect(transport);
  const tools = await client.listTools();
  const resources = await client.listResources();
  const core = await client.readResource({ uri: "second-brain://core" });
  const search = await client.callTool({
    name: "second_brain_search",
    arguments: { query, scopes: ["knowledge", "topics"], limit: 3 },
  });
  const status = await client.callTool({ name: "second_brain_status", arguments: {} });
  for (const [name, response] of [["search", search], ["status", status]]) {
    if (response.isError) throw new Error(`${name} failed: ${response.content?.[0]?.text || "unknown error"}`);
  }
  const result = {
    tools: tools.tools.map((tool) => tool.name),
    resources: resources.resources.map((resource) => resource.uri),
    coreLoaded: core.contents.some((content) => content.text?.includes("SECOND BRAIN ACTIVE")),
    search: search.structuredContent,
    status: status.structuredContent,
  };
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
} finally {
  await client.close();
}
