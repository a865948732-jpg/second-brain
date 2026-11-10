# 验证、CI 与覆盖边界

## 日常验证命令

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run check:release
```

完整验证需要 Node.js 22+、Python 3.11+、PowerShell 7+。所有 Python 模块使用标准库，测试由 unittest 发现；Node 使用内置 `node:test`，不额外安装测试框架。

| 验证层 | 入口 | 覆盖 |
| --- | --- | --- |
| Node 与收件集成 | `npm run test:node` | 知识读写、MCP 子进程、Hook 身份、日志/同步、账本/文件锁/验链/评分、对账、中文路径、链接隔离、初始化与 PowerShell 收件路由。 |
| Python 治理 | `governance/test_*.py` | 敏感资格、复核清单、内容增量、独立经验回执门、健康指标、Node/Python canonicalization、公开模板与治理入口的真实联通。 |
| Python 原生证据 | `integrations/workbuddy/test_*.py` | 合成原生运行身份、终态回执、时钟边界、观察/ingress 不可变性、回读一致性与健康包装器。 |
| 文档与发布文件 | `npm run check:release` | 当前 Git 文件清单或未初始化目录的候选清单、可疑私人物料、凭据形态、个人硬编码路径、MIT/锁文件、JSON/YAML 和相对文档链接。 |
| 文档启动流程 | `init:vault` → `demo` → `smoke` | 新库初始化、本地写入闭环与实际 stdio MCP 客户端。 |

测试使用独立临时库和合成数据库。收件测试传入 `-Text`，无需访问真实剪贴板；默认演示保留新的 `.local/demo-*` 工件，不写使用者配置的已有 Vault。

## CI

[CI 配置](../.github/workflows/ci.yml) 在 push、PR 或手动触发时，分别为 Ubuntu 和 Windows 安装 Node 22、Python 3.11，使用 hosted runner 的 PowerShell，执行完整回归、发布检查和文档启动流程。

历史具体测试数量与耗时应以对应提交的 [Actions 结果](https://github.com/a865948732-jpg/second-brain/actions) 或本次终端输出为准。测试新增后数量会变化，不能把固定数字当作能力证明。

## 结果如何解释

- 单元/夹具回归证明所测输入与边界的行为，不证明真实私人资料库已健康。
- stdio MCP 子进程验证证明协议和入口能运行，不证明某个桌面宿主已经重载并调用。
- `demo` 的验证回执只覆盖该次合成演示；它不作为独立经验晋升或正式自动化通过证据。
- WorkBuddy 的合成数据库回归不证明当前安装版数据库、热重载、自然排程、知识提交或正式门已通过。
- 发布检查是静态辅助检测。它不访问真实凭据，也不能替代人工检查 Git diff 与数据来源。

## 排查失败

| 现象 | 检查 |
| --- | --- |
| `Second-brain vault is unavailable` | 先初始化新库或核对宿主 `SECOND_BRAIN_VAULT` 与目录权限。 |
| 初始化拒绝目标目录 | 已有内容会被保留；选择新目录或直接配置已有库。 |
| `不是预期的 Obsidian Vault` | 治理入口需要 `AGENTS.md` 与 `.obsidian` 标记目录；新库由初始化器提供。 |
| `pwsh` / `python` 无法启动 | 核对运行时版本、PATH 或相应可执行文件变量。 |
| 健康 HOLD | 保留真实指标，检查关键卡、依赖、日期、资格和哈希变化，不刷新日期制造通过。 |
| 哈希链校验失败 | 保留损坏工件并停止写入，从可信历史恢复；不要绕过验链。 |
| WorkBuddy 缺少原生终态/结构字段 | 核对宿主版本及数据契约；不重写历史成功字段制造一致。 |

报告失败时提供命令、退出码、关键原文、受影响模块和已完成的覆盖，内容必须脱敏。
