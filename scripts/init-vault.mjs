import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import config from "../packages/config.cjs";

const templateRoot = fileURLToPath(new URL("../templates/vault/", import.meta.url));
const directories = [
  ".obsidian",
  "00_Inbox", "01_Sources", "02_Knowledge", "03_Topics", "04_Outputs",
  "05_Review/Daily", "05_Review/Weekly", "05_Review/待归档",
  "06_Skills/KnowledgeIteration", "99_Attachments",
];

export async function initializeVault(target = config.getVaultRoot()) {
  const vault = path.resolve(target);
  const info = await fs.lstat(vault).catch((error) => {
    if (error.code === "ENOENT") return null;
    throw error;
  });
  if (info && (!info.isDirectory() || info.isSymbolicLink() || (await fs.readdir(vault)).length)) {
    throw new Error("Vault initialization requires a new or empty directory; existing files are preserved.");
  }
  await fs.mkdir(vault, { recursive: true });
  const today = new Date().toISOString().slice(0, 10);
  async function copy(current, relative = "") {
    for (const entry of await fs.readdir(current, { withFileTypes: true })) {
      const name = relative === "" && entry.name === ".gitignore.template" ? ".gitignore" : entry.name;
      const key = path.join(relative, name);
      if (entry.isDirectory()) {
        await fs.mkdir(path.join(vault, key), { recursive: true });
        await copy(path.join(current, entry.name), key);
      } else if (entry.isFile()) {
        const text = await fs.readFile(path.join(current, entry.name), "utf8");
        await fs.writeFile(path.join(vault, key), text.replaceAll("{{DATE}}", today), { encoding: "utf8", flag: "wx" });
      }
    }
  }
  await copy(templateRoot);
  for (const directory of directories) await fs.mkdir(path.join(vault, directory), { recursive: true });
  return vault;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    if (args.length && (args.length !== 2 || args[0] !== "--path")) {
      throw new Error("Usage: npm run init:vault -- [--path /absolute/path/to/new-vault]");
    }
    console.log(JSON.stringify({ vault: await initializeVault(args[1]), created: true }, null, 2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
