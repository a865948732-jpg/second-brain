# 配置与客户端接入

所有代码默认读取仓库下的 `.local/vault`。`npm run init:vault` 创建新库；已有库通过环境变量指定。`.env.example` 是变量清单，代码不会自动载入 `.env`。

## 环境变量

| 变量 | 默认值与作用 |
| --- | --- |
| `SECOND_BRAIN_VAULT` | `<repo>/.local/vault`；MCP、账本、治理、收件和健康检查的资料库。 |
| `SECOND_BRAIN_RUNTIME` | `<repo>/packages/runtime/runtime.mjs`；Node 记忆运行时，可选替换。 |
| `CODEX_HOME` | `<用户主目录>/.codex`；仅供 Codex Hook 和日志工具检查允许的 transcript 位置。 |
| `CODEX_SECOND_BRAIN_LOG_DIR` | `<vault>/05_Review/会话日志/Codex`；会话日志写入位置。 |
| `CODEX_TRANSCRIPT_SYNC_STATE` | `<repo>/.local/runtime/transcript-sync-state.json`；增量同步的游标状态。 |
| `SECOND_BRAIN_WORKBUDDY_HOME` | `<repo>/.local/workbuddy`；可选适配器的数据上下文，须由接入方准备。 |
| `SECOND_BRAIN_WORKBUDDY_DAILY_ID` | `automation-1000000000001`；示例每日任务身份。接入真实运行时显式替换。 |
| `SECOND_BRAIN_WORKBUDDY_WEEKLY_ID` | `automation-1000000000002`；示例每周任务身份。 |
| `SECOND_BRAIN_POWERSHELL` | `pwsh`；PowerShell 7+ 的命令名或可执行文件绝对路径。 |
| `SECOND_BRAIN_PYTHON` | `python`；Python 3.11+ 的命令名或可执行文件绝对路径。 |

Python 和 PowerShell 的可执行文件变量只填写程序路径，不包含额外命令参数。路径应指向使用者控制的文件，运行时替换属于执行代码的配置。

PowerShell 设置示例：

```powershell
$env:SECOND_BRAIN_VAULT = 'C:/Data/MySecondBrain'
$env:SECOND_BRAIN_PYTHON = 'C:/Python311/python.exe'
npm run smoke
```

macOS / Linux 设置示例：

```sh
export SECOND_BRAIN_VAULT="$HOME/SecondBrain"
npm run smoke
```

在 MCP 宿主中通过服务器的 `env` 配置设置路径更可靠；桌面应用不一定继承刚打开的终端环境。

## Codex 的 MCP 配置

将下面片段按宿主的现有配置方式添加，并替换路径：

```toml
[mcp_servers.second_brain]
command = "node"
args = ["C:/Projects/second-brain/packages/mcp/index.js"]

[mcp_servers.second_brain.env]
SECOND_BRAIN_VAULT = "C:/Data/MySecondBrain"
```

通用 JSON 配置见 [examples/mcp.json](../examples/mcp.json)。配置示例仅描述服务器入口，不包含账户认证、工具放行或自动 Hook 注册。接入后用宿主列出工具，并分别执行一次 search 和 read，确认连接的是预期资料库。

## Hook 命令入口

| 事件 | 命令参数 | 行为 |
| --- | --- | --- |
| `SessionStart` | `node packages/mcp/hook.js --event SessionStart` | 输出核心上下文。 |
| `SubagentStart` | `node packages/mcp/hook.js --event SubagentStart` | 输出核心上下文。 |
| `UserPromptSubmit` | `node packages/mcp/hook.js --event UserPromptSubmit` | 检索相关知识并记录任务开始。 |
| `Stop` | `node packages/mcp/hook.js --event Stop` | 追加停止事件与未验证的回合结果。 |
| 用户输入日志 | `node hooks/second-brain-log.cjs --event userPromptSubmit` | 保存明确的输入事件。 |
| 最终回答日志 | `node hooks/second-brain-log.cjs --event stop` | 保存终态回答；有条件地读取宿主 transcript。 |

注册时将脚本参数改为绝对路径。Hook 从 stdin 接收 JSON；上下文 Hook 使用 `hookSpecificOutput.additionalContext` 返回内容。具体注册语法、可信命令声明和终态事件名称以宿主版本为准。

例子命令在 [examples/hook-commands.json](../examples/hook-commands.json)，它是接入说明数据。先在独立库验证命令，再增量合入宿主配置，保留原配置与撤回路径。Windows 接入需要由宿主隐藏子进程窗口；本仓库的测试调用使用 `windowsHide: true`，实际宿主行为仍需现场验证。

## 接入已有 Vault

最低结构包含 `AGENTS.md` 和数据目录。治理程序另外要求 `.obsidian/` 标记目录，以及 `06_Skills/KnowledgeIteration/health_gate_policy.json`。关键卡与每轮真实依赖必须明确登记，不能把示例策略当作使用者已经完成的复核。

先设置路径并运行 `smoke` 和只读健康检查，评估结果后再启用写入工具。读取默认覆盖 `02_Knowledge`、`03_Topics`；`01_Sources` 与会话复盘区需要显式选择搜索 scope。数据面内部的符号链接或 Junction 可能被路径保护拒绝，宜提供普通目录和明确的库根路径。

资料库版本控制由使用者管理，MCP 写入返回 `gitCommitRequired`，不会自行运行 Git。仓库的代码历史与资料库的私人历史各自维护。
