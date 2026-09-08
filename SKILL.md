---
name: orca-task-dispatcher
description: 用 CLI 生成并启动用户确认的任务分发。用于手动或定时将任务分配到仓库并启动 `/dev-spec-gen`。
---

# Orca 任务分发器

按“用户选择流程 → 任务获取 →（完整流程执行需求/GitNexus 调研；直接流程跳过调研）→ 外部联合决策 → worktree 准备 → 用户确认 → 开发会话启动”的顺序执行任务分发。

- 默认配置由托管的 `config/dispatcher.default.yaml` 提供，用户只在被忽略的 `config/dispatcher.yaml` 中填写差异；通过 `task-source --jql` 可临时覆盖本次 JQL，不写入配置。
- 完整方案与流程模式由用户/外部编排器选择，Dispatcher 不根据参考方案文本自动判断；两条流程最终都必须经过显式 `decide` 和安全 `launch`。
- 调研、路由或报告无法确认时只暂停当前任务，不阻塞其他任务；主流程不得进行全目录泛搜。
- 项目和分支确认后才准备 worktree 和分发输入；开发会话复用报告并跳过重复调研。
- 具体命令、字段、报告格式和布局差异以对应 CLI 的 `--help`、`task-source`、`decide` 输出及配置提示为准；只有 `decide` 确认的任务才进入 worktree 与 launch。

本地配置文件的读取和修改遵循项目安全约束；不要绕过 CLI 或自行猜测配置和输入。