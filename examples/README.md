# 接入示例

- [mcp.json](mcp.json)：stdio MCP 配置，替换绝对路径后由客户端注册。
- [hook-commands.json](hook-commands.json)：Hook 命令入口说明；需要按宿主版本手动合入现有配置。
- [mcp-client.mjs](mcp-client.mjs)：真实 MCP 客户端，先 search，再 read 第一张命中卡片；只读当前配置的 Vault。

```sh
node examples/mcp-client.mjs "第二大脑"
```

先运行 `npm run init:vault` 或配置已有 Vault。完整本地写入闭环使用 `npm run demo`，每次保留一个新的独立示例库。

示例配置不包含真实路径、账户认证或工具放行。参见 [配置说明](../docs/CONFIGURATION.md) 和 [工作流](../docs/WORKFLOWS.md)。
