import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { fileURLToPath } from "node:url";
import config from "../packages/config.cjs";

const client = new Client({ name: "second-brain-example", version: "1.0.0" });
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [fileURLToPath(new URL("../packages/mcp/index.js", import.meta.url))],
  env: { ...process.env, SECOND_BRAIN_VAULT: config.getVaultRoot() },
  stderr: "pipe",
});
try {
  await client.connect(transport);
  const search = await client.callTool({
    name: "second_brain_search",
    arguments: { query: process.argv.slice(2).join(" ") || "第二大脑", limit: 3 },
  });
  if (search.isError) throw new Error(search.content[0]?.text || "Search failed.");
  const first = search.structuredContent.results[0];
  const note = first ? await client.callTool({ name: "second_brain_read", arguments: { path: first.path } }) : null;
  if (note?.isError) throw new Error(note.content[0]?.text || "Read failed.");
  console.log(JSON.stringify({ candidates: search.structuredContent.results, firstNote: note?.structuredContent || null }, null, 2));
} finally {
  await client.close();
}
