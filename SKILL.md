---
name: orca-task-dispatcher
description: 用 CLI 生成并启动用户确认的任务分发。用于手动或定时将任务分配到仓库并启动 `/dev-spec-gen`。
---

# Orca 任务分发器

按“用户选择流程 → 任务获取 →（完整流程执行需求/GitNexus 调研；直接流程跳过调研）→ 外部联合决策 → 用户确认 → 工作区准备与开发会话启动”的顺序执行任务分发。

proposal（出具开发方案）流程必须拆分为两个独立节点：Jira 节点与调研节点均独立参考 `/dev-spec-gen` 公开规范；Jira 节点先规范真实 JQL、读取完整需求及附件并归档，调研节点再消费快照执行 GitNexus 跨项目只读调研。节点成功后才由 `launch` 查找任务稳定名称的同名工作区，不存在才创建，最终发送 `/dev-spec-gen 出具开发方案 <jira地址> <原始需求文本路径>`。

- 默认配置由托管的 `config/dispatcher.default.yaml` 提供；未显式传入 `--config` 时，若设置非空 `ORCA_DISPATCHER_CONFIG_DIR`，脚本自动读取该目录下的 `dispatcher.yaml` 作为用户覆盖层，否则使用默认 `config/dispatcher.yaml`；环境变量目录不存在或缺少该文件时仅使用托管默认配置。客户目录中的 `.env` 和其他文件不会自动读取；通过 `task-source --jql` 可临时覆盖本次 JQL，不写入配置。
- 流程不写死在代码里：配置顶层的 `stages:` 是独立可复用的节点，`flows:` 按名引用节点组成流程，且必须且只能有一个 `default: true` 的流程。新增流程只在配置里追加 `flows` 项（必要时追加 `stages` 节点），不改脚本；流程节点同时声明候选资格要求，proposal/direct/complete 均不得绕过候选资格。
- 完整方案与流程模式由当前会话选择和执行，Dispatcher 不根据参考方案文本自动判断；两条流程最终都必须经过显式 `decide` 和安全 `launch`。
- 直接流程（`direct`）只做最小分发，使用 `development_jira_spec(jira 参考方案驱动流程开发`；必须同时向下游传递原始实际开发任务编号 `source_task_id`（缺省回退为 `task_id`），供实际开发任务按需读取子任务中的仓库方案；工作区、状态和制品身份始终使用上游已归一化后的实际需求 `task_id`，不得把开发子任务编号写入 `task_id`，也不得由 Dispatcher 根据 `parent_task_id` 猜测替换；全自动执行，无需人员介入。direct、complete、proposal 在同一任务/租户下使用独立内部状态 identity，可并发分发；对外 `assignment_id` 保持兼容，worktree/terminal 可共享。
- 后续任务分发在当前会话内完成：当前会话直接执行任务查询、Jira 需求归档、调研、路由判断与制品暂存；这些准备阶段不调用 Orca、不创建 worktree，也不创建或打开新的 Agent 会话。工作区解析或创建、分支改名、制品迁入与开发会话启动全部由已确认的 `launch` 完成，顺序为 `orca worktree create`（不带 `--agent`）→ dev-spec-gen `--sync-only` 同步 → 制品迁入并校验 → `orca terminal create --command claude` → `orca terminal wait --for tui-idle` → `orca terminal send --enter --wait-submit`；启动后仅执行下述只读启动验收。
- 候选资格与仓库映射是两个阶段：Jira 原始 JQL 及等价转换再次解析都失败时必须停止当前任务，禁止用过宽基础 JQL 继续取候选；Jira 节点逐任务交接 `candidate_eligible`、原因、参考方案来源和 `candidate_jql`。proposal 要求任务或父任务参考方案非空；direct 可不带参考方案，但同样要求候选资格为 true。只有已通过资格但仓库映射仍不唯一时，GitNexus/标题/描述等证据才可作为映射兜底。
- 调研、路由或报告无法确认时只暂停当前任务，不阻塞其他任务；主流程不得进行全目录泛搜。
- 同一任务/租户下不同流程使用独立内部状态 identity；`state`、`recover`、`reset` 支持 `--dispatch-flow`，省略时多流程返回歧义；流程可共享工作区。工作区内活动 Claude 会话只有在 state 中记录的首次投递摘要与本次渲染文本一致时才判定为重复并跳过；摘要不同、活动会话无摘要或没有对应 state 时，新开 Claude 会话继续本流程，既有会话不关闭、不追加输入；相同摘要但首次投递回执未确认时只报告待人工核对，不视为分发成功，也不重发。共享活动工作区跳过初始化同步，制品仅补缺失、不覆盖旧会话内容。

- 项目和分支确认后才暂存分发输入；需要预先准备工作区时，只使用 `dispatcher.py worktree create --task-id ... --repository ... [--base-branch ...] [--worktree-slug ...]`。该命令无状态、严格幂等，只做 Orca 建区/复用、分支改名和 dev-spec-gen `--sync-only`，不迁移制品、不创建终端、不发送提示词；省略基础分支且配置未给默认值时，必须确认 Orca 仓库默认基础分支，无法确认则阻断；后续 launch 按稳定名称复用。
- 具体命令、字段、报告格式和终端的实际参数以对应 CLI 的 `--help`、`task-source`、`decide` 输出及配置提示为准；只有 `decide` 确认的任务才进入 worktree 与 launch。单任务优先通过 CLI 参数调用 `decide`，direct 必须显式传 `--dispatch-flow direct --source-task-id <原始开发子任务编号>`；将返回的 `result.launch_input` 原样保存给 launch，禁止手工重建 JSON 导致流程字段丢失。

## 启动验收

1. 下发文本只使用所选流程的 `command_template` 渲染结果，不额外追加监督、心跳、汇报或追问要求。
2. 接收方是普通 Claude 开发会话（`orca terminal create --command claude`），分发不注入任何协议前导，也不接管它的后续判断。
3. 只按投递回执判定：`result.send.accepted` 证明任务文本被接受；`result.send.prompt.stages` 含 `turn_started` 才算观察到本轮起步。已接受但未观察到起步仍记为已分发，并保留「启动未确认」告警；回执里没有 `send` 对象、会话未在 120 秒内进入 `tui-idle`、或主机返回 `observation=unsupported` 时该任务落 `requires_manual_reset`。
4. 汇总“任务 / 流程 / 已分发 / 投递回执”后结束本轮：记录 `terminal_handle` 与 `send_request_id`，不等待开发完成、不读取会话输出、不进入持续监督循环、不向下游追问。`--wait-submit` 的观察静默不构成重发理由；只有相同首次投递摘要才跳过重复会话，摘要不同则按正常流程新开终端并只投递一次。

本地配置文件的读取和修改遵循项目安全约束；不要绕过 CLI 或自行猜测配置和输入。