---
name: orca-task-dispatcher
description: 用 CLI 生成并启动用户确认的任务分发。用于手动或定时将任务分配到仓库并启动 `/dev-spec-gen`。
---

# Orca 任务分发器

按“用户选择流程 → 任务获取 →（完整流程执行需求/GitNexus 调研；直接流程跳过调研）→ 外部联合决策 → worktree 准备 → 用户确认 → 开发会话启动”的顺序执行任务分发。

proposal（出具开发方案）流程必须拆分为两个独立节点：Jira 节点与调研节点均独立参考 `/dev-spec-gen` 公开规范；Jira 节点先规范真实 JQL、读取完整需求及附件并归档，调研节点再消费快照执行 GitNexus 跨项目只读调研。节点成功后才查找任一基础分支下同名 worktree，不存在才调用统一 worktree CLI，最终发送 `/dev-spec-gen 出具开发方案 <jira地址> <原始需求文本路径>`。

- 默认配置由托管的 `config/dispatcher.default.yaml` 提供，用户只在被忽略的 `config/dispatcher.yaml` 中填写差异；通过 `task-source --jql` 可临时覆盖本次 JQL，不写入配置。
- 完整方案与流程模式由当前会话选择和执行，Dispatcher 不根据参考方案文本自动判断；两条流程最终都必须经过显式 `decide` 和安全 `launch`。
- 直接流程（`direct`）只做最小分发，使用 `development_jira_spec(jira 参考方案驱动流程开发`；必须同时向下游传递原始实际开发任务编号 `source_task_id`（缺省回退为 `task_id`），供实际开发任务按需读取子任务中的仓库方案；全自动执行，无需人员介入。
- 后续任务分发在当前会话内完成：当前会话直接执行任务查询、Jira 需求归档、调研、路由判断和 worktree 准备，不通过 Orca 编排创建或打开新的 Agent 会话；仅在最终已确认的 launch 阶段，才使用 Orca 绑定 worktree 并启动目标 Claude terminal。
- 调研、路由或报告无法确认时只暂停当前任务，不阻塞其他任务；主流程不得进行全目录泛搜。

- 项目和分支确认后才准备 worktree 和分发输入；开发会话复用报告并跳过重复调研。
- 具体命令、字段、报告格式和布局差异以对应 CLI 的 `--help`、`task-source`、`decide` 输出及配置提示为准；只有 `decide` 确认的任务才进入 worktree 与 launch。

本地配置文件的读取和修改遵循项目安全约束；不要绕过 CLI 或自行猜测配置和输入。