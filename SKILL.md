---
name: orca-task-dispatcher
description: 用 CLI 生成并启动用户确认的任务分发。用于手动或定时将任务分配到仓库并启动 `/dev-spec-gen`。
---

# Orca 任务分发器

按“用户选择流程 → 任务获取 →（完整流程执行需求/GitNexus 调研；直接流程跳过调研）→ 外部联合决策 → 用户确认 → 工作区准备与开发会话启动”的顺序执行任务分发。

proposal（出具开发方案）流程必须拆分为两个独立节点：Jira 节点与调研节点均独立参考 `/dev-spec-gen` 公开规范；Jira 节点先规范真实 JQL、读取完整需求及附件并归档，调研节点再消费快照执行 GitNexus 跨项目只读调研。节点成功后才由 `launch` 查找任务稳定名称的同名工作区，不存在才创建，最终发送 `/dev-spec-gen 出具开发方案 <jira地址> <原始需求文本路径>`。

- 默认配置由托管的 `config/dispatcher.default.yaml` 提供；未显式传入 `--config` 时，若设置非空 `ORCA_DISPATCHER_CONFIG_DIR`，脚本自动读取该目录下的 `dispatcher.yaml` 作为用户覆盖层，否则使用默认 `config/dispatcher.yaml`；环境变量目录不存在或缺少该文件时仅使用托管默认配置。客户目录中的 `.env` 和其他文件不会自动读取；通过 `task-source --jql` 可临时覆盖本次 JQL，不写入配置。
- 流程不写死在代码里：配置顶层的 `stages:` 是独立可复用的节点，`flows:` 按名引用节点组成流程，且必须且只能有一个 `default: true` 的流程。新增流程只在配置里追加 `flows` 项（必要时追加 `stages` 节点），不改脚本；`task-source --flow`、`decide --dispatch-flow`、`launch` 只接受注册表中的流程，`state`/`recover`/`reset` 可用已删除的历史流程名操作旧状态。节点名与流程名限 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`；旧 `dispatch.skill.command_templates` 与 `task_source.flows` 自动映射并告警。
- 完整方案与流程模式由当前会话选择和执行，Dispatcher 不根据参考方案文本自动判断；两条流程最终都必须经过显式 `decide` 和安全 `launch`。
- 直接流程（`direct`）只做最小分发，使用 `development_jira_spec(jira 参考方案驱动流程开发`；必须同时向下游传递原始实际开发任务编号 `source_task_id`（缺省回退为 `task_id`），供实际开发任务按需读取子任务中的仓库方案；全自动执行，无需人员介入。direct、complete、proposal 在同一任务/租户下使用独立内部状态 identity，可并发分发；对外 `assignment_id` 保持兼容，worktree/terminal 可共享。
- 后续任务分发在当前会话内完成：当前会话直接执行任务查询、Jira 需求归档、调研、路由判断与制品暂存；这些准备阶段不调用 Orca、不创建 worktree，也不创建或打开新的 Agent 会话。工作区解析或创建、分支改名、制品迁入与开发会话启动全部由已确认的 `launch` 完成，顺序为 `orca worktree create`（不带 `--agent`）→ dev-spec-gen `--sync-only` 同步 → 制品迁入并校验 → `orca terminal create --command claude` → `orca terminal wait --for tui-idle` → `orca terminal send --enter --wait-submit`；启动后仅执行下述只读启动验收。
- 调研、路由或报告无法确认时只暂停当前任务，不阻塞其他任务；主流程不得进行全目录泛搜。
- 同一任务/租户下不同流程使用独立内部状态 identity；`state`、`recover`、`reset` 支持 `--dispatch-flow`，省略时多流程返回歧义，旧状态缺少流程字段按 `complete` 兼容；本次仅隔离状态，允许共享同一任务工作区，但每次分发都新开开发会话，不复用既有终端。

- 项目和分支确认后才暂存分发输入；工作区由 `launch` 按稳定名称解析或创建，开发会话复用报告并跳过重复调研。
- 具体命令、字段、报告格式和终端的实际参数以对应 CLI 的 `--help`、`task-source`、`decide` 输出及配置提示为准；只有 `decide` 确认的任务才进入 worktree 与 launch。单任务优先通过 CLI 参数调用 `decide`，direct 必须显式传 `--dispatch-flow direct --source-task-id <原始开发子任务编号>`；将返回的 `result.launch_input` 原样保存给 launch，禁止手工重建 JSON 导致流程字段丢失。

## 启动验收

1. 下发文本只使用所选流程的 `command_template` 渲染结果，不额外追加监督、心跳、汇报或追问要求。
2. 接收方是普通 Claude 开发会话（`orca terminal create --command claude`），分发不注入任何协议前导，也不接管它的后续判断。
3. 只按投递回执判定：`result.send.accepted` 证明任务文本被接受；`result.send.prompt.stages` 含 `turn_started` 才算观察到本轮起步。已接受但未观察到起步仍记为已分发，并保留「启动未确认」告警；回执里没有 `send` 对象、会话未在 120 秒内进入 `tui-idle`、或主机返回 `observation=unsupported` 时该任务落 `requires_manual_reset`。
4. 汇总“任务 / 流程 / 已分发 / 投递回执”后结束本轮：记录 `terminal_handle` 与 `send_request_id`，不等待开发完成、不读取会话输出、不进入持续监督循环、不向下游追问。`--wait-submit` 的观察静默不构成重发理由：任何情况下都不重复创建或再次投递。

本地配置文件的读取和修改遵循项目安全约束；不要绕过 CLI 或自行猜测配置和输入。