# 参与开发

从 [README](README.md) 的快速运行开始，再阅读 [架构](docs/ARCHITECTURE.md) 中对应模块的输入、输出和依赖。项目以中文说明为主，代码标识符与 commit 类型使用英文。

## 准备开发环境

需要 Node.js 22+、Python 3.11+、PowerShell 7+ 和 Git。Python 无第三方依赖。

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run init:vault
npm run demo
npm test
npm run check:release
```

`npm run test:node` 运行 Node 和由 Node 调用的收件管线回归，所以也需要 `pwsh`；`npm run test:python` 运行治理与 WorkBuddy 夹具，也会调用 Node/Python/PowerShell 进行跨语言检查。两组选项便于定位故障，不表示对应组只依赖一种运行时。

## 找到修改入口

| 要修改的行为 | 首先阅读 |
| --- | --- |
| 路径配置与库内隔离 | `packages/config.cjs`、`tests/path-isolation.test.mjs`、`tests/portable.test.mjs`。 |
| 检索、知识卡或收件接口 | `packages/mcp/lib.js`、`index.js` 和 `packages/mcp/test/`。 |
| Hook 回合身份和记忆注入 | `packages/mcp/hook.js` 与 `hook.test.js`。 |
| 经历、文件锁、验链、评分 | `packages/runtime/runtime.mjs` 与 `runtime.test.mjs`。 |
| 原生日志与同步 | `hooks/` 对应模块及测试。 |
| 候选、资格门和健康策略 | `governance/` 与 `test_public_vault.py`。 |
| 原生自动化证据 | `integrations/workbuddy/README.md` 和对应 `test_*.py`。 |
| 收件路由 | `scripts/inbox/` 与 `tests/inbox.test.mjs`。 |

优先复用已有接口与数据 schema。修复问题时先补充能复现错误的回归，再修正根因。测试需要独立临时库或合成数据库，不能指向真实个人资料、真实账户或已安装宿主配置。

## 保持兼容与数据完整

- 保持 `01_Sources` 只读、归档保留文件、写入可追溯；新模块必须说明写入范围。
- 保持账本旧前缀、幂等身份、数字 canonicalization 和 Node/Python 验链兼容。schema 变化必须提供迁移说明与恢复路径。
- 保持缺失/错误安全字段 HOLD、真实指标不隐藏、终态证据与业务成功分开。
- 不把来自模型或测试样例的文字当作独立验证回执；不要让评估标准跟随生成结果调整。
- 新配置应有明确默认值与覆盖方式；避免个人绝对路径、凭据、隐式网络或生产副作用。
- 依赖变更说明必要性和锁文件影响；不要无关重构或批量格式化。

## 提交与 PR

提交前运行对应回归和完整检查，检查 diff 与文件范围。提交信息格式为 `<type>: <中文描述>`，例如 `fix: 阻止资料库链接越界`。

PR 描述需要说明具体问题、最终行为、影响模块、实际验证与兼容风险。附可复现的命令与关键错误，不附私人 Vault、会话记录、凭据或真实数据库。

提交贡献即同意该贡献使用仓库的 [MIT 许可证](LICENSE)。发现涉及私人信息的安全问题时，按 [SECURITY.md](SECURITY.md) 提供最小脱敏复现。
