# Orca Task Dispatcher

基于 [Orca CLI](https://github.com/orca) 的开发任务分发 Skill。它负责把已确认的任务发送到 Orca terminal 中的 Claude 会话，并用本地状态文件避免重复分发；不等待下游开发任务完成。

## 前置条件

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- 已安装、可调用且正在运行的 `orca` CLI
- 可供任务获取提示词使用的任务查询工具
- 位于配置 `workspace.projects_root` 下的 Git 仓库

## 安装与配置

安装依赖后，复制公开样例为本地配置：

```bash
uv sync
cp config/dispatcher.example.yaml config/dispatcher.yaml
```

Windows 可在资源管理器中复制 `config/dispatcher.example.yaml` 并重命名为 `config/dispatcher.yaml`。

随后编辑本地配置，至少替换：

- `workspace.projects_root` 和 `workspace.projects`；项目可配置 `description`、`tenants` 与项目级 `branch_priority`。同一 Jira 任务命中多个租户时，每个租户必须输出独立 assignment 和 worktree；分支规则只在所属项目内生效
- `base_branch.options`
- `task_source.task_url_template`、`query`、`fetch_prompt`；`task_source.reference_plan_field` 填写 Jira 中“参考方案”字段的实际字段 ID（如 `customfield_12345`）或字段名，该映射只写在配置中，脚本不内置任何具体 Jira 字段 ID；字段未配置或值为空时可省略 `reference_plan`，不得伪造字段值
- 需要时的 `dispatch.skill.command_templates`；提示词整体只按该模板渲染，不硬编码在脚本中。任务上下文字段（`{title}`、`{description}`、`{assignee}`、`{tenant}`、`{assignment_id}`、`{reference_plan}`、`{gitnexus_report_path}`、`{requirement_snapshot_path}`）与 `{task_url}`、`{task_id}`、`{base_branch}` 合并进同一模板，字段值为空的行会被省略；有父产品需求时任务已归一化为产品需求本身，来源开发任务信息不下发

`config/dispatcher.yaml` 与根目录 `config.yml` 都是本地文件，已被忽略，**不要提交**。不要在配置或任务输入中保存令牌、密码、Cookie、内部域名、内部路径或运行状态。

## 权限与信任目录

Dispatcher 以 `claude` 启动任务会话，可通过 `dispatch.agent_extra_args` 配置为 `--dangerously-skip-permissions` 跳过工具权限弹窗，避免任务命令被权限确认阻塞。

首次使用前，需要把项目根目录加入 Claude Code 的信任目录，否则创建 worktree 等命令可能因权限确认无法送达：

1. 在根目录会话中执行 `/permissions`（或 `/trust`）将项目根目录授权为信任目录；
2. 或在 `settings.json` 中为相应目录配置权限。

## 常用命令

所有命令从项目根目录运行：

```bash
uv run --project . python scripts/dispatcher.py --help
uv run --project . python scripts/dispatcher.py validate
uv run --project . python scripts/dispatcher.py repos
uv run --project . python scripts/dispatcher.py task-source
uv run --project . python scripts/dispatcher.py decide --input decision.json
uv run --project . python scripts/dispatcher.py state
uv run --project . python scripts/dispatcher.py branches --repository example-repository
```

推荐流程：

1. 运行 `validate` 验证配置和候选仓库。
2. 运行 `task-source` 获取固定 JQL 与字段契约；由外部 Jira 工具实际拉取开发需求，逐条解析父产品需求，保留实际任务、来源子任务、标题、描述、负责人和参考方案。参考方案按配置 `task_source.reference_plan_field` 指定的 Jira 字段读取并在非空时映射为 `reference_plan`；未配置或值为空时可省略，参考方案缺失不阻断项目与分支的联合决策。
3. 运行 `state`，跳过 `dispatched`；`launching` 或 `requires_manual_reset` 按现有规则处理。单个任务暂停不得阻塞其他任务。
4. 对每个可处理任务，先读取完整 Jira 原始需求与全部附件本体：完整性校验失败时该任务不得进入 GitNexus 调研或分发。需求快照临时保存于 `.runtime/requirements/<实际-task-id>/`；图片在 Markdown 中保留 OCR 文本与语义描述，其他附件保留原件并由 Markdown 索引相对路径、SHA-256 与可读性状态。随后由外部子 Agent 进行一次跨项目、只读的 GitNexus 远程调研，不创建 worktree；报告返回候选、证据和排除理由。再将报告与实际任务标题/描述、子任务负责人、父产品需求、参考方案人员分工及 `repos`、`branches` 描述联合决策仓库、租户和基础分支。同一 Jira 实际任务命中多个项目或租户时，保留同一个 `task_id`，但展开为多条 tenant assignment；例如 saasoanew 的九讯云（智乐方）与易腾各一条，oanew 与 saasoanew 同时命中时也各一条。项目配置的 `branch_priority` 优先于普通分支候选，例如项目内同时命中九机与九讯云（智乐方）时选择 `release_saas`。
5. 将外部已决策任务写为 `version: 1` 的决策 JSON，运行 `decide --input decision.json`。该命令只校验显式项目/分支是否属于当前配置并输出规范化任务，不调用 Jira、GitNexus 或 Orca，也不写分发状态。若输出 `needs_confirmation`，仅暂停对应任务并补充人工决策后重跑；绝不以候选顺序猜测项目或分支。
6. 将每个归档复制到最终 tenant worktree 的 `docs/engineering/specs/<日期>-<业务板块>-raw-requirements.md` 与 `docs/engineering/attachments/<实际-task-id>/`；将最终快照绝对路径写入 `requirement_snapshot_path`。GitNexus 调研报告先暂存于 `.runtime/research/<实际-task-id>-gitnexus.md`，同样在项目和分支锁定后迁移到最终 worktree。
7. 将 `decide` 的 `launch_input` 中 selected 任务保存为 `tasks.json`；下游开发会话读取并复用报告，跳过已完成的 GitNexus 调研节点。展示汇总并取得 terminal 创建/发送确认后执行 `launch`。

GitNexus 调研发生在“实际任务归一化、state 校验”之后和项目/分支锁定、worktree 创建之前。调研或报告失败只暂停当前任务，其他明确任务继续。Dispatcher 本身只校验最终确认输入、状态和终端，不执行 Jira 查询或 GitNexus 语义匹配。

## 启动任务

`launch` 输入文件顶层只能有 `tasks` 列表：

```json
{
  "tasks": [
    {
      "task_id": "TASK-123",
      "title": "示例开发任务",
      "task_url": "https://example.invalid/tasks/TASK-123",
      "repository": "example-repository",
      "repository_path": "/path/to/projects/example-repository",
      "base_branch": "main",
      "worktree_path": "/path/to/projects/example-repository-task-123"
    }
  ]
}
```

使用：

```bash
uv run --project . python scripts/dispatcher.py launch --input tasks.json
```

`worktree_path` 仅适用于 `separate` 布局，并且必须是源仓库已登记的 linked worktree；任务工作区预检未通过或无法确认有效路径时，不创建终端、不进入启动，这不等同于任务或分发失败，等待补充有效路径或人工处理。`split` 布局的旧单租户任务不接受该字段，同一项目的任务在项目主仓库 tab 的 pane 中聚合；但指定了 `tenant` 的租户 assignment 无论哪种布局都必须提供各自 worktree_path。任务 URL 必须由配置中的 `task_url_template` 生成。`description`、`assignee`、`reference_plan`、`source_task_id`、`source_assignee`、`parent_task_id` 和 `parent_assignee` 均为可选任务上下文，随状态保存但不会全部下发：提示词只按 `dispatch.skill.command_templates` 渲染，所有字段值压缩为单行并去除反引号，字段值为空的行省略；有父产品需求时任务已归一化为产品需求本身，来源开发任务与父任务重复信息不下发。`gitnexus_report_path` 仅适用于 `separate`，必须指向任务 worktree 内 `docs/engineering/research/` 下已存在的报告；`requirement_snapshot_path` 指向该任务 worktree 内 `docs/engineering/specs/` 下文件名以 `-raw-requirements.md` 结尾的原始需求 Markdown，launch 前会校验其元数据标记为 complete、附件清单位于 `docs/engineering/attachments/<task_id>/` 且每个附件的大小与 SHA-256 一致；快照缺失、不完整或校验失败会阻断该任务。下游必须先读取快照，再按其中相对路径读取附件本体；存在快照时任务描述不内联进命令，只传递快照路径。

同一 `task_id` 可通过不同 `tenant` 与 `tenant_slug` 形成独立 `assignment_id`（`<task_id>::<tenant_slug>`），从而分别创建 worktree、终端和运行状态；`tenant_slug=legacy` 为旧单租户输入的保留值，指定租户时不得使用。多租户任务复位时必须使用 `reset <task_id> --tenant-slug <slug>`，以免误操作其他租户；`recover --task-id <task_id>` 遇到同一任务的多个租户状态时会报歧义，必须追加 `--tenant-slug <slug>` 精确恢复。

`decide` 输入的顶层必须是 `{ "version": 1, "tasks": [...] }`。任务必须有 `task_id`、`title`、`task_url`，并由外部编排器在调研和联合判断后显式填入 `repository`、`base_branch`；它只做配置白名单与分支可用性校验，既不请求 Jira/GitNexus，也不根据标题或描述自动选择。指定 `tenant` 的租户任务还必须提供各自 `worktree_path` 与完整的 `requirement_snapshot_path`，快照缺失或完整性校验失败时该任务直接返回 `needs_confirmation`。`status=ready` 时，`launch_input.tasks` 是可供 worktree 准备后交给 `launch` 的标准任务列表；`status=needs_confirmation` 时须仅处理返回的未决任务。

## 布局与状态

- `separate`：每项任务使用独立 linked worktree 和与该 worktree 目录名一致的唯一 terminal 标题；tab 直接绑定该 worktree，以 `--command claude` 启动会话，等待 TUI 就绪后发送开发请求。终端句柄超时时，先按 worktree 路径和标题查找唯一已有终端，仅确认不存在时才重建；多匹配或查询失败会进入 `requires_manual_reset`。
- `split`：同一项目、同一租户的任务可在一个 tab 的 pane 中聚合；每条租户 assignment 仍必须绑定各自的 linked worktree，不能因 task_id 相同而共享工作树。
- 可配置 `dispatch.agent_extra_args`（如 `--dangerously-skip-permissions`）跳过工具权限弹窗，避免任务命令被权限确认阻塞。
- 任务 worktree 经 `repo add` 新注册后，首次 `terminal create` 若仅因等待 terminal handle 超时，Dispatcher 会先查找 worktree 路径和唯一标题均匹配的终端；确认不存在时才最多重建三次。
- 就绪等待（`tui-idle`，首轮超时 `ready_timeout_ms`，默认 120s，重试逐轮递增至 360s 上限）后还会读取会话内容（terminal preview）确认任务实际运行，内容为空视为未运行并按 `ready_retry_attempts` 自动重试；命令发送超时按 `send_retry_attempts` 重发，重发可能导致命令被执行两次。重试预算耗尽才标记 `requires_manual_reset`。
- 终端收到任务且本地状态写入成功后，Dispatcher 会将对应 Orca worktree 卡片设为 `in-progress`。
- `dispatched` 任务会被跳过，避免重复发送。
- `launching` 或 `requires_manual_reset` 不会在普通 `launch` 中自动重试。separate 的 `launching` 任务可由 `recover` 按 worktree 路径和唯一标题查找并安全接管；split 的 `launching` 仍需人工复位。确认终端与任务状态后，使用 `reset <task_id>` 清除本地状态；复位 `dispatched` 状态需要明确传入 `--force`。
- 使用 `recover` 可恢复已分发任务，以及进程中断后尚未发送任务的 separate `launching` 会话；它不会对 split `launching` 或其他状态不确定的任务自动重发。

```bash
uv run --project . python scripts/dispatcher.py recover
uv run --project . python scripts/dispatcher.py recover --task-id TASK-123 --tenant-slug <slug>
uv run --project . python scripts/dispatcher.py reset TASK-123
uv run --project . python scripts/dispatcher.py reset TASK-123 --force
```

## 安全边界

- 仅将已确认的任务输入交给 `launch`；不要猜测任务 ID、标题、仓库或路径。
- 分支名和任务 ID 会被限制为安全字符；任务标题仅按数据处理，不作为 shell 命令执行。
- Orca 的创建操作不自动重试（仅对 separate 布局的 terminal handle 等待超时先查找唯一已有终端，确认不存在时才自动重建最多三次）；多匹配、查询失败和 split 布局的句柄不确定均需人工复位，因为无法确认副作用是否已经发生。
- Git Bash/MSYS 环境下，Dispatcher 会仅对 Orca CLI 子进程关闭路径参数转换，保证 slash command 和 URL 原样送达终端。

## 开发与验证

```bash
uv run --project . python tests/test_dispatcher.py
uv run --project . python scripts/dispatcher.py --config config/dispatcher.example.yaml validate
git diff --check
```

样例中的项目路径是占位值，直接执行 `validate` 前需替换成存在的本地 Git 仓库路径。

## 许可证

本项目采用 [MIT License](LICENSE)。
