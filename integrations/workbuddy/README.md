# WorkBuddy 原生证据适配器

这一目录提供运行身份解析、终态回执校验、原生 transcript 审计、FinalStop 观察，以及可选的独立证据 ingress。它适用于需要把“模型文字”“宿主结果栏”“原生最终消息”和“业务验收”分别核实的自动化接入。

代码来源于现有本地集成中的可复用实现，已经改为仓库相对导入和显式数据上下文。真实宿主的数据库、消息结构和 Hook 行为仍需按所用版本核对。当前仓库回归使用合成 SQLite 与 transcript 夹具。

## 文件与依赖顺序

```text
resolve_second_brain_run_context.py  → 当前运行类型、身份、北京时间目标日期
validate_automation_receipt.py      → 机器步骤与最终业务字段的格式校验
audit_second_brain_terminal.py      → 原生最终 assistant 与宿主输出的一致性
workbuddy_terminal_observer.py      → FinalStop 时的不可覆盖观察记录
workbuddy_terminal_ingress.py       → 可选独立存储、配置绑定及完成后的回读
run_second_brain_health_check.py    → 独立的只读健康检查包装器
```

Observer 复用 audit；audit 复用 receipt validator；ingress 复用 observer 和 `governance/self_growing_distillation.py` 的敏感检查。健康包装器直接调用仓库的 PowerShell 治理脚本，不依赖 WorkBuddy 数据库。

## 数据前提

默认上下文为 `<repo>/.local/workbuddy`。真实接入通过 `SECOND_BRAIN_WORKBUDDY_HOME` 显式指定，并设置每日/每周 automation ID。示例 ID 分别是 `automation-1000000000001` 和 `automation-1000000000002`。

适配器需要原生 `automation_runs`、`automation_runtime_state` 和运行上下文解析所用的 automation 定义字段，以及 projects 下的原生 JSONL transcript。字段与约束可在各 `test_*.py` 的最小数据库夹具中核对。

运行身份为 `automation-<数字>:<运行起点毫秒>`，session 为 UUID，request 为宿主实际请求标识。数据根不得包含符号链接或 Windows 重解析点。只读审计使用 SQLite `mode=ro` 和 `query_only`；不会修改原生运行表。

## 只读入口

```sh
python -B integrations/workbuddy/resolve_second_brain_run_context.py --automation-id automation-1000000000001 --db "/absolute/path/to/workbuddy.db"
python -B integrations/workbuddy/audit_second_brain_terminal.py --thread-id "automation-1000000000001:实际运行起点" --db "/absolute/path/to/workbuddy.db" --projects "/absolute/path/to/projects"
```

必须替换为实际运行 ID，不能把示例身份用于真实验收。原生审计会检查最后一条 assistant、session/request、最终内容类型、结果栏输出一致性及机器回执。它不会通过改写宿主 `runResult` 修复缺失证据。

已有观察记录后，可设置数据上下文并执行：

```sh
python -B integrations/workbuddy/workbuddy_terminal_observer.py --verify-thread "实际运行ID"
python -B integrations/workbuddy/workbuddy_terminal_ingress.py --verify-thread "实际运行ID"
```

Observer 与原生 audit 的退出含义：0 为已验证且声明业务 PASS；2 为已验证且业务 HOLD；3 为 audit/observer 发现运行仍在进行；1 为无法验证。Ingress 验证失败退出 1。退出 0 仍只覆盖模块列出的检查，不自动证明知识工件、提交边界或正式业务门通过。

## FinalStop 回调与独立 ingress

Observer 从 stdin 接收原生 `FinalStop` JSON，request 取 `CODEBUDDY_CONVERSATION_REQUEST_ID`；只对工作目录和活跃运行均匹配的事件保存 `terminal-observations`。异常只向 stderr 输出安全的错误类型。

Ingress 是有数据库写入的可选能力。接入方必须先在隔离副本验证兼容性，并在明确授权下准备 `second_brain_terminal_ingress_config`、`second_brain_terminal_ingress` 和不可变触发器。`create_tables` 与 `configure` 是供集成程序调用的函数，不是自动安装步骤。

默认绑定配置文件是数据上下文下的 `hooks.json`，只需提供已脱敏的 Hook 结构，例如 `{"hooks": {}}`。不要复制含凭据的完整宿主配置。配置绑定包括 publisher、observer、audit、validator、敏感检查实现和 Hook 结构哈希；代码变更后必须经过重新核对，配置漂移会停止写入。

Prospective ingress 只接受启用时间之后的新活跃运行。相同证据重复投递幂等；内容冲突、旧运行、变更的 transcript 或敏感回执会拒绝。写入自己的独立表后，不改宿主成功字段和正式门。

## 状态的准确含义

| 字段/状态 | 所能证明 |
| --- | --- |
| `evidence_status=CAPTURED` | 活跃运行的原生终态观察已保存。 |
| `final_stop_observation_verified=true` | 完成后观察记录与当前原生证据回读一致。 |
| `native_ingress_verified=true` | 独立存储与原生证据、观察记录和当前配置绑定一致。 |
| `declared_business_status` | 合法终态回执中声明的业务状态，尚需业务工件与提交验收。 |
| `official_gate_upgraded=false` | 独立 ingress 没有替调用者升级正式业务门。 |
| `complete_repair_proven=false` | 本适配器不以终态观察推断整个系统已完成修复。 |

真实自然排程、宿主热重载、知识/报告工件与生产数据库迁移均由接入者另外验收。仓库不会创建 WorkBuddy 任务或恢复其他工具的旧排程。
