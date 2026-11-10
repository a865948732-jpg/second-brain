# 来源与公开整理范围

本仓库从现有本地第二大脑系统中提取可复用代码，以独立 Git 历史公开。MIT 许可证适用于本仓库代码、说明与通用模板。

## 纳入的实现

| 原模块角色 | 当前位置 |
| --- | --- |
| Second Brain MCP 与生命周期 Hook | `packages/mcp` |
| SecondBrainRuntime 经历记忆 | `packages/runtime` |
| Codex 日志、增量同步与日级补回 | `hooks` |
| KnowledgeIteration 的候选、敏感资格与健康门 | `governance` |
| WorkBuddy 原生身份、回执、终态观察与独立 ingress | `integrations/workbuddy` |
| 知识摄取管线与账本对账 | `scripts/inbox`、`scripts/reconcile_second_brain_ledger.mjs` |

整理时保留了这些模块的已有回归，并加入新库、路径隔离和收件管线的集成回归。原始文件与哈希清单在整理环境中保留，未包含在公开仓库。

## 为独立运行进行的调整

- 将散布的实现复制到独立代码目录，使用仓库相对导入和统一 Node 配置。
- 把个人 Vault、用户主目录及已安装脚本路径改成可配置入口；默认运行数据放入 `.local/`。
- 把示例任务身份改为通用值，真实 WorkBuddy 接入必须显式配置。
- 使用标准 `fileURLToPath` 处理带空格/中文及不同操作系统的脚本路径。
- 补充初始化保护、库内链接隔离、UTF-8 输出、可运行示例、发布检查、CI 与开发文档。
- 示例卡片正文重新编写，沿用核心上下文接口所引用的三个通用文件名，不复制私人知识。

公开副本没有导入原 Vault 的 Git 历史、知识正文、Sources、附件、聊天日志、运行账本、真实数据库或账户配置。本仓库的提交与测试不能替代原系统的宿主或正式自动化验收。

## 方法与依赖的归属

Markdown/Obsidian 是数据组织与浏览方式；MCP 是工具接入协议；候选评分、状态门与具体目录约定属于本项目的工程设计。文档中的“自生长”指可观察的候选、复核、回写和版本流程，不表示系统具有意识或自主生成可靠知识。

主依赖的版本与许可证见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。依赖以锁文件安装，不把 `node_modules` 提交到仓库。
