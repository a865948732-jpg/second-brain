# 第二大脑 · Second Brain

**让 AI 在后续任务中找回有来源的知识、过去的决策和真实执行经历。**

Second Brain 是一个本地优先的知识与记忆工具箱：用 Markdown 和 Obsidian 组织资料，用 MCP 为 AI 客户端提供检索、阅读、明确收件和知识卡回写，用追加式账本保存任务经过，再用治理脚本生成复盘候选和健康报告。它适合长期项目协作、个人知识管理和需要追溯依据的 AI 工作流。

代码、模板和示例采用 [MIT 许可证](LICENSE)。仓库提供通用实现与重新编写的示例库；使用者的资料库、会话日志、账本、数据库和账户配置由使用者在本机单独管理。

## 主要功能

| 功能 | 实际行为 |
| --- | --- |
| 知识检索与阅读 | 按标题、路径、正文关键词和知识状态排序，返回有界摘要与 Vault 相对路径，再读取完整卡片；支持中文。 |
| 核心记忆注入 | 从索引和核心卡片生成紧凑上下文；可通过 MCP 或支持命令 Hook 的宿主加载。 |
| 明确收件与知识回写 | 将用户选定文本存入 Inbox、脱敏并按内容去重；创建或更新带来源、置信度和证据时间线的知识卡。 |
| 任务经历记忆 | 保存任务开始、事件、结果和反思；用文件锁、序号、幂等标识和 SHA-256 哈希链校验记录完整性。 |
| 知识治理 | 扫描增量资料、隔离可疑敏感内容、生成蒸馏候选和健康指标；经验达到独立回归门槛后进入评审队列。 |
| 跨工具接入 | 提供 MCP、Codex 会话日志与 Hook、PowerShell 收件管线，以及可选 WorkBuddy 原生运行证据适配器。 |

“自生长”在这里有明确流程：收集证据 → 生成候选 → 真实复核 → 回写知识 → 保存版本。候选评分和格式有效的回执不能代替事实验证、用户授权或任务业务成功。模型调用、宿主 Hook 注册和定时调度由接入方提供。

## 快速运行

需要 **Node.js 22+**。完整治理和测试还需要 **Python 3.11+**、**PowerShell 7+（`pwsh`）**；Python 代码仅依赖标准库。Obsidian 是可选的资料浏览工具，终端演示可以独立运行。

```sh
git clone https://github.com/a865948732-jpg/second-brain.git
cd second-brain
npm ci --ignore-scripts --no-audit --no-fund
npm run init:vault
npm run demo
npm run smoke
```

- `init:vault` 在 `.local/vault` 创建通用资料库，包括数据目录、示例卡片与健康策略；目标目录已有内容时拒绝覆盖。
- `demo` 每次在新的 `.local/demo-*/vault` 中完成“收件去重 → 写卡 → 检索与回读 → 记录任务 → 召回与验链”，并输出实际断言结果。
- `smoke` 启动真实 stdio MCP 子进程，检查 **11 个工具、2 个资源**、核心上下文、检索和状态。它读取当前配置的 Vault。
- `.local/` 被 Git 忽略。代码使用仓库位置解析默认路径，可以从其他工作目录启动。

打开 Obsidian，选择“打开文件夹作为库”，指向 `.local/vault`，即可浏览示例。初始化生成空 `.obsidian` 目录作为治理检查的库标记，不包含个人界面或插件配置。

指定一个**新库**：

```sh
npm run init:vault -- --path "/absolute/path/to/new-vault"
```

使用**已有库**时，先确认它的目录结构、权限与版本备份，再按 [配置说明](docs/CONFIGURATION.md) 设置 `SECOND_BRAIN_VAULT`。初始化命令不用于向已有库覆盖模板。

## 接入 AI 客户端

在支持 stdio MCP 的客户端中注册以下入口，并替换两个绝对路径：

```json
{
  "mcpServers": {
    "second_brain": {
      "command": "node",
      "args": ["/absolute/path/to/second-brain/packages/mcp/index.js"],
      "env": { "SECOND_BRAIN_VAULT": "/absolute/path/to/your/vault" }
    }
  }
}
```

Windows 路径可写为 `C:/Projects/second-brain/...`。Codex TOML 示例、Hook 入口和权限设置见 [接入配置](docs/CONFIGURATION.md) 与 [工作流](docs/WORKFLOWS.md)。`npm start` 启动的是 stdio 协议进程，等待客户端消息时终端不会显示聊天界面。

典型调用顺序：

```text
second_brain_search(query="当前问题")
  → second_brain_read(path="搜索返回的相对路径")
  → 结合当前证据回答或执行
  → 在明确授权范围内 capture / upsert_card / task_finish
```

只读工具和写入工具分别标注在 [MCP 接口表](docs/ARCHITECTURE.md#mcp-接口)。接入方应使用宿主提供的工具授权机制；协议中的提示字段不能代替权限控制。

## 项目结构

```text
second-brain/
├── packages/
│   ├── config.cjs             # 默认路径、环境变量和 Vault 路径约束
│   ├── mcp/                   # MCP 服务、知识读写、上下文与生命周期 Hook
│   └── runtime/               # 经历账本、文件锁、召回和候选评分
├── hooks/                     # Codex 日志、增量同步与定向补回
├── governance/                # 蒸馏候选、敏感资格门和分层健康检查
├── integrations/workbuddy/    # 可选的原生运行身份、终态回执和 ingress 验证
├── scripts/
│   ├── inbox/                 # PowerShell 显式收件与队列路由
│   └── *.mjs                  # 初始化、演示、验证、发布检查与账本对账
├── templates/vault/           # 通用 Vault 骨架和健康策略
├── examples/                  # MCP 配置及可运行接口调用示例
├── tests/                     # 可移植性、路径隔离和收件管线的集成回归
├── docs/                      # 架构、配置、流程、验证和来源说明
└── .github/                   # Windows / Ubuntu CI 与问题模板
```

资料库内部使用以下分层：

| 目录 | 职责 |
| --- | --- |
| `00_Inbox` | 明确选定、尚待分类的收件。 |
| `01_Sources` | 原始证据；应用工具只读。 |
| `02_Knowledge` | 带来源的知识、决策、习惯和项目卡片。 |
| `03_Topics` | 专题与长期问题。 |
| `04_Outputs` | 文章、报告及其他成果。 |
| `05_Review` | 每日/每周复盘、候选队列、运行记录和待归档。 |
| `06_Skills` | 可复用模板、流程与显式健康策略。 |
| `99_Attachments` | 图片、PDF、音视频等附件，由使用者管理。 |

每个生产模块的输入、输出、依赖和核心逻辑见 [架构说明](docs/ARCHITECTURE.md)。可按“先了解目录 → 阅读接口 → 跟随一条实际流程”的顺序进入代码。

## 验证与开发

```sh
npm test
npm run check:release
```

`npm test` 顺序运行 Node 回归、Python 治理回归和 WorkBuddy 夹具回归。测试使用独立临时资料库与数据库；PowerShell 收件测试通过显式 `-Text` 输入。`check:release` 检查提交候选中的私人文件形态、凭据形态、个人硬编码路径、许可证、配置与文档链接。

[验证说明](docs/VALIDATION.md) 区分本地测试、真实 MCP 子进程和宿主运行的覆盖范围；[贡献指南](CONTRIBUTING.md) 说明开发入口与提交要求。代码以 JavaScript、Python 和 PowerShell 源文件直接运行，无需额外的项目编译步骤。

## 实际使用边界

检索采用有界关键词评分；大型资料库需要进一步评估覆盖率与召回效果。账本每次读取和追加都会验证完整哈希链，当前上限为 100000 条；哈希链提供完整性检查，其信任仍取决于本机存储与历史版本。脱敏规则识别常见形态，使用者仍应在输入前移除私人信息。

Hook 的日志记录、治理候选、WorkBuddy 原生证据和最终业务结果分别验收。WorkBuddy 接入依赖特定数据库与终态消息结构，参见 [适配器说明](integrations/workbuddy/README.md)。仓库中的自动化相关代码不会自行创建计划任务。

资料写入后，根据实际修改路径更新索引并在资料库自己的 Git 仓库中定向提交。归档保留历史文件，原始证据保持只读。详见 [数据与安全说明](SECURITY.md) 和 [来源与整理范围](docs/PROVENANCE.md)。
