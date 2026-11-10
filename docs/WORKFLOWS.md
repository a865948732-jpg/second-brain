# 使用流程与可观察结果

所有示例从仓库根运行。先完成 [快速运行](../README.md#快速运行) 或设置 `SECOND_BRAIN_VAULT`。资料库写入需要当前任务的明确授权与版本保护；示例库用于体验这些接口。

## 查询与回答

1. 调用 `second_brain_search`，默认只查知识和专题；按任务选择少量候选。
2. 对准备引用的候选调用 `second_brain_read`，核对来源、认知、状态和复核日期。
3. 检查当前项目事实；与历史记忆冲突时保留冲突及证据，不把笔记自动当作授权。
4. 把有实际依据的可复用结论写为知识卡，或暂留候选；回读文件并定向保存版本。

查询本身是只读操作。仓库中的工具不会因为检索发生就自动写回答案。

## 明确收件与知识回写

在 MCP 客户端调用 `second_brain_capture`：

```json
{ "text": "请保留：检索候选要阅读全文后再引用。", "source": "manual", "title": "引用知识的检查步骤" }
```

第一次返回 `created=true` 和 `00_Inbox/WorkBuddy/...md`；同来源、同脱敏内容再次提交会返回已有路径。收件尚处于 pending，不能据此认为已完成知识编译。

复核后调用 `second_brain_upsert_card`：

```json
{
  "title": "引用知识前的证据检查",
  "type": "knowledge",
  "status": "active",
  "confidence": "高",
  "source": "本次人工复核的流程定义",
  "tags": ["检索", "证据"],
  "currentCognition": "引用历史卡片前核对正文、来源和当前项目事实。",
  "evidence": "流程定义经过本次复核；不表示外部研究结论。"
}
```

通过 read 回读 `02_Knowledge/引用知识前的证据检查.md`，按真实关联更新索引，再在资料库自己的 Git 中提交实际修改文件。MCP 返回 `gitCommitRequired`，没有自动提交或推送能力。

带真实冲突的卡使用 `status: disputed` 并列证据。新概念首次出现先放候选，二次出现后再判断是否需要独立知识页。这是示例工作流的治理规则，调用者负责实施。

## 任务经历与反思

任务开始时记录 goal、taskType、project、授权描述和检索回执；过程只保存必要的 action/result/failure/verification/checkpoint；结束时记录 outcome、实际验证、反思与可选 lessonCandidate。

```json
{
  "taskId": "example-task-001",
  "goal": "验证资料库检索",
  "taskType": "validation",
  "project": "example",
  "authority": "本次本地验证",
  "retrieval": { "query": "引用知识", "candidates": ["02_Knowledge/引用知识前的证据检查.md"] }
}
```

该对象用于 `second_brain_task_start`。结束时，未执行测试就记录 `verification.status="unverified"` 和实际停止原因；真实验证工件用 receipt 的 `artifactHash`、`runId`、`verifier` 绑定。不能把模型说“已完成”转换成通过回执。

随后用 `second_brain_recall_experience` 按问题、项目或错误签名检索。`eligible_for_review` 表示结构门槛达标的评审候选，仍需外部证据复核。可运行 [examples/mcp-client.mjs](../examples/mcp-client.mjs) 查看真实客户端的只读调用，或用 `npm run demo` 体验完整本地闭环。

## 蒸馏与敏感资格

预览当前增量候选：

```sh
python -B governance/self_growing_distillation.py --dry-run --lookback-days 7 --max-candidates 3
```

输出报告正文及末尾 JSON；`dry_run=true`，没有报告或状态写入。显式 `--vault "/absolute/path"` 可指定其他库。

需要保存候选与基线时，省略 `--dry-run`：

```sh
python -B governance/self_growing_distillation.py --lookback-days 7 --max-candidates 3
```

写入区为 `05_Review/自生长蒸馏`。`--bootstrap` 只建立当前基线；遇到新增或变化的可疑敏感来源会拒绝建立基线。程序不会自动把候选改为知识卡。

单文件资格检查或通过资格后读取：

```sh
python -B governance/self_growing_distillation.py --check-path "01_Sources/example.md"
python -B governance/self_growing_distillation.py --read-path "01_Sources/example.md"
```

文件需由使用者自行准备。这两种模式与 `--dry-run` / `--bootstrap` 互斥；资格失败不返回正文。`--check-all` 扫描数据面 Markdown，`--sync-review-registry` 同步按内容哈希管理的复核清单。只有经过真实审查的无效凭据示例才可放行，真实敏感内容应从输入中移除。

## 健康检查

获得结构化只读结果：

```sh
python -B integrations/workbuddy/run_second_brain_health_check.py --phase daily
python -B integrations/workbuddy/run_second_brain_health_check.py --phase weekly --dependency "02_Knowledge/引用知识前的证据检查.md"
```

这个包装器也可独立使用，不需要 WorkBuddy 数据库。`status=PASS` 需要 `parsed`、`consistent`、`no_write` 均为 true，`exit_code=0` 且 `hard_problem_count=0`。普通逾期保留 `maintenance_reminder_count` 和 `maintenance_status=DUE`；提醒不能当作复核通过。

直接查看 Markdown 报告：

```sh
pwsh -NoProfile -File governance/知识健康检查.ps1 -NoWrite -Phase daily
```

省略 `-NoWrite` 会向 `05_Review/Weekly` 新建健康报告。已有同名报告时默认使用时间后缀保留旧文件；`-Force` 可覆盖，使用者应先保留版本并明确确认目标。PowerShell 命令退出 0 本身不表示健康 PASS，应核对完整指标或使用结构化包装器。

## PowerShell 收件管线

显式文本收件：

```sh
pwsh -NoProfile -File scripts/inbox/Save-ClipboardToInbox.ps1 -Source manual -Text "一条需要保留的资料"
```

不传 `-Text` 时会读取调用时的剪贴板文本；需要本机剪贴板能力。`-Notify` 使用 Windows 本地通知窗口，仅在使用者需要时启用。PowerShell 收件独立命名文件；MCP capture 另外提供内容哈希去重。

按收件返回路径显式入队：

```sh
pwsh -NoProfile -File scripts/inbox/Invoke-InboxPipeline.ps1 -Action Enqueue -Path "00_Inbox/WorkBuddy/收件返回的文件名.md" -Decision knowledge -Reason "已确认值得进一步编译"
pwsh -NoProfile -File scripts/inbox/Invoke-InboxPipeline.ps1 -Action Process
```

Process 默认输出计划。核对目标后，使用 `-Action Process -Apply` 才执行移动。路由如下：

| decision | 目标与意义 |
| --- | --- |
| `knowledge` | `05_Review/摄取队列/待编译/knowledge`，等待知识编译。 |
| `topic` | `05_Review/摄取队列/待编译/topic`，等待专题整理。 |
| `source` | `05_Review/摄取队列/待人工入源`，等待使用者处理原始证据。 |
| `archive` | `05_Review/待归档/<日期>`，保留原文件。 |
| `ignore` | 文件保留原处，记录处理依据。 |

管线只接受 `00_Inbox` 中的指定文本文件，入队时绑定 SHA-256；文件变化、历史重复或目标已有内容时拒绝移动。Status 返回 pending/processed/failed 计数，第一次调用可能创建队列目录。Retry 需显式 QueueId。入队与路由成功不表示已经生成正式知识卡。

## Codex 日志与定向补回

已注册的日志 Hook 保存输入与最终回答；生命周期 Hook 保存任务事件；二者是不同记录。

```sh
node hooks/second-brain-transcript-sync.mjs --verbose
```

这是一次增量扫描，首次对已有文件建立尾部基线。需要持续运行时由使用者的宿主或显式调度器重复调用；脚本不会自行常驻。新文件按其记录处理，正常同步不会补回已有文件的全部历史。

补回必须指定 transcript 和目标北京时间日期：

```sh
node hooks/recover_codex_day.cjs --transcript "/absolute/path/to/codex-home/sessions/example.jsonl" --date 2026-11-10
```

先看只读预览，明确需要时加 `--apply`。补回使用原始记录的角色、身份和时间，不伪造历史业务成功。

## 开放任务对账

```sh
node scripts/reconcile_second_brain_ledger.mjs --dry-run --stale-hours 24 --max-tasks 100
```

审查后，显式 `--apply` 追加管理闭环；可重复 `--exclude-task-id` 保留仍在工作的任务。损坏的哈希链会停止，旧记录不会重写。对账结论是超时任务未验证的管理状态，与任务业务完成分别判断。
