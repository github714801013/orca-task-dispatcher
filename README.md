# Orca Task Dispatcher

基于 [Orca CLI](https://github.com/orca) 的开发任务分发 Skill。它负责为已确认的任务创建工作区，并在该工作区里开启一个独立 Claude 开发会话、投递一次任务文本，用本地状态文件避免重复分发；不等待下游开发任务完成。

## 前置条件

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- 已安装、可调用且正在运行的 `orca` CLI
- 可供任务获取提示词使用的任务查询工具
- 位于配置 `workspace.projects_root` 下、且已在 Orca 注册的 Git 仓库（未注册时 `launch` 会自动 `orca repo add`）
- 已设置好 Orca 的工作区根目录（新工作区落在 `<Orca workspaceDir>/<仓库名>/<工作区名>`）

## 安装与配置

安装依赖后，默认配置由仓库托管，用户只需创建本地覆盖文件：

```text
config/dispatcher.default.yaml  # 托管默认配置，不修改
config/dispatcher.yaml          # 用户覆盖配置，已忽略，不提交
```

用户覆盖按 mapping 递归合并；标量和 `null` 直接覆盖，列表整体替换。未显式传入 `--config` 时，若设置非空 `ORCA_DISPATCHER_CONFIG_DIR`，脚本自动读取其目录下的 `dispatcher.yaml` 作为用户覆盖层；显式 `--config` 优先，环境变量为空或未设置时继续使用默认 `config/dispatcher.yaml`，目录不存在或缺少该文件时仅使用托管默认配置。客户目录中的 `.env` 和其他文件不会自动读取。

后续任务分发在当前会话内完成：当前会话直接执行任务查询、Jira 需求归档、调研与路由判断，并把需求快照、附件与调研报告暂存到源仓库的 `.runtime/<task-id>/docs/engineering/` 下。工作区创建、制品迁移与开发会话启动全部由 `launch` 通过 Orca 完成，会话不创建、不预检工作区。

`task-source --jql` 传入的内容可能是伪 SQL 或伪 JQL。独立 Jira 节点必须先分析查询语法；识别为伪 JQL 时先转换为当前 Jira 实例支持的原生 JQL，再将转换结果交给 Jira 原生解析器校验，校验通过后才能执行。原生 JQL 也必须经过 Jira 原生解析器校验，任何解析、转换或校验失败都不得执行查询。

`task-source --flow proposal` 用于出具开发方案：独立 Jira 节点先按 dev-spec-gen 规范取得真实 JQL 并归档完整需求/附件，独立调研节点再按同一规范执行 GitNexus 只读调研；路由确认后由 `launch` 建工作区并开启开发会话，最终下发 `/dev-spec-gen 出具开发方案 {task_url} {requirement_snapshot_path}`，另附两行「用户任务编号：{source_task_id}」「当前用户名：{source_assignee}」。编号缺省时回退到 `task_id`；用户名优先取来源开发任务的 `source_assignee`，仅未归一化的任务回退到 `assignee`，不使用父需求负责人代替。缺少可靠用户名时省略该行，保留原命令与编号。Jira 节点输出 `jql_semantics=native_jql_then_parent_post_filter`、`parent_lookup` 和 `post_filter` 交接字段；父需求字段不可用原生 JQL join 伪造。

```bash
uv sync
cp config/dispatcher.example.yaml config/dispatcher.yaml
```

Windows 可在资源管理器中复制 `config/dispatcher.example.yaml` 并重命名为 `config/dispatcher.yaml`。客户配置位于其他目录时，设置 `ORCA_DISPATCHER_CONFIG_DIR` 为该目录；脚本会自动读取其中的 `dispatcher.yaml`，相对目录按当前工作目录解析。

随后在用户覆盖配置中填写：

- `workspace.projects_root` 和 `workspace.projects`；项目可配置 `description`、`tenants` 与项目级 `branch_priority`。同一 Jira 任务命中多个租户时，每个租户必须输出独立 assignment 和独立工作区；分支规则只在所属项目内生效
- `dispatch.terminal.read_retry_*`：Orca 只读查询的重试次数与间隔。启动命令固定为 `claude`，由 `launch` 通过 `orca terminal create --command claude` 显式启动，配置不再提供 `agent_commands` / `agent_extra_args`。
- `base_branch.options`
- `task_source.task_url_template`、`query`、`fetch_prompt`；`task_source.reference_plan_field` 填写 Jira 中“参考方案”字段的实际字段 ID（如 `customfield_12345`）或字段名，该映射只写在配置中，脚本不内置任何具体 Jira 字段 ID；字段未配置或值为空时可省略 `reference_plan`，不得伪造字段值
- 配置顶层的 `stages:` 与 `flows:` 流程注册表；提示词整体只按流程引用节点的 `command_template` 渲染，不硬编码在脚本中。任务上下文字段（`{title}`、`{description}`、`{assignee}`、`{tenant}`、`{assignment_id}`、`{reference_plan}`、`{gitnexus_report_path}`、`{requirement_snapshot_path}`、`{source_task_id}`、`{source_assignee}`）与 `{task_url}`、`{task_id}`、`{base_branch}` 合并进同一模板，字段值为空的行会被省略；有父产品需求时任务已归一化为产品需求本身。`{source_task_id}` 是来源开发子任务编号（缺省回退 `{task_id}`）、`{source_assignee}` 是其负责人（未归一化的任务回退到 `{assignee}`，归一化后缺少来源负责人时留空），proposal 模板用这两行下发用户任务编号与当前用户名

`config/dispatcher.yaml` 与根目录 `config.yml` 都是本地文件，已被忽略，**不要提交**。不要在配置或任务输入中保存令牌、密码、Cookie、内部域名、内部路径或运行状态。

## 启动命令与权限

启动命令由 Dispatcher 显式给出：`launch` 用 `orca terminal create --worktree id:<repoId>::<工作区路径> --title <工作区名> --command claude` 在工作区里开启一个普通 Claude 会话，再用 `orca terminal send --enter --wait-submit` 投递一次 `command_template` 的渲染结果。接收方是独立开发会话，Dispatcher 不注入监督协议，也不再拼接启动参数，因此配置里没有 `dispatch.agent_commands` / `dispatch.agent_extra_args`。需要跳过工具权限弹窗时，在 Orca 侧或该 Claude 会话自身的配置里处理。

分发不使用 `orca orchestration`：不需要绑定编排 Run，也不会写入 `.runtime/orchestration.json`（历史文件与既有 Run 保持不动）。

## 常用命令

所有命令从项目根目录运行：

```bash
uv run --project . python scripts/dispatcher.py --help
uv run --project . python scripts/dispatcher.py validate
uv run --project . python scripts/dispatcher.py repos
uv run --project . python scripts/dispatcher.py task-source --flow complete
uv run --project . python scripts/dispatcher.py task-source --flow direct --jql "project = DEMO AND status = ready"
uv run --project . python scripts/dispatcher.py decide --task-id CW-7622 --source-task-id CW-7624 --title "任务标题" --task-url "https://jira.example/CW-7622" --repository finance --base-branch origin/release_saas --dispatch-flow direct
uv run --project . python scripts/dispatcher.py decide --input decision.json
uv run --project . python scripts/dispatcher.py state
uv run --project . python scripts/dispatcher.py state --dispatch-flow direct
uv run --project . python scripts/dispatcher.py recover --task-id TASK-123 --dispatch-flow complete
uv run --project . python scripts/dispatcher.py reset TASK-123 --dispatch-flow proposal
uv run --project . python scripts/dispatcher.py branches --repository example-repository
```

推荐流程：

1. 运行 `validate` 验证配置和候选仓库。
2. 运行 `task-source` 获取该流程的默认 JQL 与字段契约（节点声明 `query` 时用它，否则用 `task_source.query`，`--jql` 可临时覆盖，输出里的 `jql_source` 标明取自 `cli` / `flow` / `config`）；由外部 Jira 工具实际拉取开发需求，逐条解析父产品需求，保留实际任务、来源子任务、标题、描述、负责人和参考方案。参考方案按配置 `task_source.reference_plan_field` 指定的 Jira 字段读取并在非空时映射为 `reference_plan`；未配置或值为空时可省略，参考方案缺失不阻断项目与分支的联合决策。
3. 运行 `state`，跳过相同任务、租户和流程已是 `dispatched` 的分发；`launching` 或 `requires_manual_reset` 按现有规则处理。direct、complete、proposal 在同一任务/租户下拥有独立状态，支持 `state --dispatch-flow <flow>` 筛选。单个任务暂停不得阻塞其他任务。
4. 对每个可处理任务，先读取完整 Jira 原始需求与全部附件本体：完整性校验失败时该任务不得进入 GitNexus 调研或分发。需求快照临时保存于 `.runtime/requirements/<实际-task-id>/`；图片在 Markdown 中保留 OCR 文本与语义描述，其他附件保留原件并由 Markdown 索引相对路径、SHA-256 与可读性状态。需求正文、附件或参考方案中指向其他系统的链接（如语雀）必须用该系统对应的专用工具读取，禁止用 WebFetch 等通用网页抓取直接读取；读到的正文同样归档进快照并索引来源 URL 与 SHA-256，工具不可用、无权限或读取/归档校验失败时该任务按 incomplete 阻断，不得跳过链接继续。随后由外部子 Agent 进行一次跨项目、只读的 GitNexus 远程调研，不创建 worktree；报告返回候选、证据和排除理由，作为路由的兜底证据。仓库、租户和基础分支以参考方案（`task_source.reference_plan_field`，如 `customfield_11103`）为准：从参考方案提取「人员—项目/技术栈」分工，与当前子任务负责人对应后映射到配置候选，命中即采用、不得被其他证据推翻；只有参考方案缺失或无法映射到配置候选时，才回退用 GitNexus 报告、任务标题/描述、子任务负责人、父产品需求及 `repos`、`branches` 描述联合决策。同一 Jira 实际任务命中多个项目或租户时，保留同一个 `task_id`，但展开为多条 tenant assignment；例如 saasoanew 的九讯云（智乐方）与易腾各一条，oanew 与 saasoanew 同时命中时也各一条。项目配置的 `branch_priority` 优先于普通分支候选，例如项目内同时命中九机与九讯云（智乐方）时选择 `release_saas`。
5. 通过 `decide` 校验外部联合决策：可以使用单任务 CLI 参数直接传入 `dispatch-flow direct` 与 `source-task-id`，也可以使用旧版 `--input`。禁止手工重建或删减字段；直接使用返回 JSON 的 `launch_input` 作为后续 `launch` 输入。该命令只校验显式项目/分支，不调用 Jira、GitNexus 或 Orca，也不写分发状态。若输出 `needs_confirmation`，仅暂停对应任务并补充人工决策后重跑；绝不以候选顺序猜测项目或分支。
6. 把每个任务的需求快照、附件与调研报告暂存到源仓库的 `<源仓库>/.runtime/<实际-task-id>/docs/engineering/` 下，布局与工作区内的 `docs/engineering` 完全同构：快照写 `specs/`、附件写 `attachments/<实际-task-id>/`、调研报告写 `research/`；`requirement_snapshot_path` 与 `gitnexus_report_path` 都指向该暂存目录下的绝对路径。
7. 将 `decide` 的 `launch_input` 中 selected 任务保存为 `tasks.json`，执行 `launch`：Dispatcher 会按任务依次确保源仓库已在 Orca 注册、`orca worktree create` 建工作区、把分支改名为 `<用户名>/<工作区名>`、复用 dev-spec-gen 的 worktree sync 同步未托管内容与 IDE 配置、把暂存制品迁入工作区并校验，最后用 `orca terminal create --command claude` 开启开发会话并投递一次任务文本。

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
      "worktree_slug": "fix-login",
      "requirement_snapshot_path": "/path/to/projects/example-repository/.runtime/TASK-123/docs/engineering/specs/2026-09-16-example-raw-requirements.md"
    }
  ]
}
```

使用：

```bash
uv run --project . python scripts/dispatcher.py launch --input tasks.json
```

工作区由 `launch` 按稳定名称创建或复用，输入不再接受 `worktree_path`。`worktree_slug` 是可选的 1–2 个英文小写 kebab 词，工作区名与初始分支名取 `<任务编号>-<slug>`（省略时为 `<任务编号>`）；最终目录落在 `<Orca workspaceDir>/<仓库名>/<工作区名>`，分支随后改名为 `<用户名>/<工作区名>`。`requirement_snapshot_path` 与 `gitnexus_report_path` 必须位于源仓库 `.runtime/<task-id>/docs/engineering/` 暂存目录下，`launch` 会把它们迁入工作区后按原有完整性规则校验（附件清单、大小与 SHA-256 一致、快照标记 complete）；流程节点声明 `requires_snapshot: false` 时不要求该字段。

同一 `task_id` 可通过不同 `tenant` 与 `tenant_slug` 形成独立 `assignment_id`（`<task_id>::<tenant_slug>`），从而分别创建工作区与运行状态；同一任务/租户下的 `direct`、`complete`、`proposal` 则共享对外 `assignment_id`，但使用独立内部状态 identity（`<task_id>::<tenant_slug>::flow::<dispatch_flow>`），可并发分发、共享同一稳定工作区。`state`、`recover`、`reset` 均支持 `--dispatch-flow <流程名>`；省略时仅在唯一流程匹配时兼容，多流程会报歧义。这三个只读/复位命令同样接受已从注册表删除的历史流程名（只按字符串匹配已有状态），而 `task-source --flow`、`decide --dispatch-flow`、`launch` 只接受注册表中的流程，未注册会直接报错并列出当前可用流程。旧状态缺少流程字段时仅按 `complete` 解释。`tenant_slug=legacy` 为旧单租户输入的保留值，指定租户时不得使用。多租户任务复位时必须使用 `reset <task_id> --tenant-slug <slug>`，以免误操作其他租户；`recover --task-id <task_id>` 遇到同一任务的多个租户状态时会报歧义，必须追加 `--tenant-slug <slug>` 精确恢复。

`decide` 输入可以是旧版 `version=1` 文件，也可以是单任务 CLI 参数；单任务模式必须显式提供 `task_id`、`title`、`task_url`、`repository`，direct 流程必须显式传 `--dispatch-flow direct`，并建议同时传 `--source-task-id`。成功返回的 `launch_input.tasks` 应原样保存并交给 `launch`，不得手工重建任务 JSON。`worktree_slug` 可选；流程引用节点声明 `requires_snapshot: true`（缺省）时还必须提供位于源仓库 `.runtime` 暂存目录下的完整 `requirement_snapshot_path`，快照缺失或完整性校验失败时该任务直接返回 `needs_confirmation`。`status=ready` 时，`launch_input.tasks` 是可直接交给 `launch` 的标准任务列表；`status=needs_confirmation` 时须仅处理返回的未决任务。

## 流程注册表

流程不写死在代码里：配置顶层的 `stages:` 定义独立可复用的节点，`flows:` 按名引用节点组成流程。新增流程只需改配置，不用改脚本。

```yaml
stages:                       # 有序数组，节点独立可复用，可被多个流程引用
  - name: complete_dispatch
    command_template: "/dev-spec-gen {task_url} ..."   # 必须以 /dev-spec-gen 开头
    # 以下均为可选：session_prompt 缺省回退 task_source.session_prompt.complete，
    # fetch_prompt 缺省回退 task_source.fetch_prompt，next_steps 仅用于提示，
    # requires_worktree / requires_snapshot 缺省为 true。
    session_prompt: |
      ...会话说明...
    fetch_prompt: |
      ...任务获取提示词，支持 {{query}} 与 {{reference_plan_field}}...
    next_steps:
      - "执行独立 Jira 节点并归档完整需求与附件"
    requires_worktree: true
    requires_snapshot: true
flows:                        # 有序数组，必须且只能有一个 default: true
  - name: complete
    stages: [complete_dispatch]
    default: true
```

- 节点名与流程名限 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`，重复名、未知节点引用、未知节点字段、`default` 缺失或不唯一、以及流程引用的节点全都未声明 `command_template`，都会在加载配置时报错。节点为独立定义，不支持在流程内做字段级覆盖。
- 省略流程时统一使用注册表里 `default: true` 的那个流程：`task-source` 不传 `--flow`、`decide` 与 `launch` 输入不传 `dispatch_flow` 都按此回退；显式传入的流程必须命中注册表。
- 一个流程引用多个节点时按顺序合成：`command_template` 取最后一个声明它的节点，`session_prompt` 取最后一个声明者，`fetch_prompt` 与 `query` 取第一个声明者，`next_steps` 顺序拼接，`requires_worktree` / `requires_snapshot` 需所有节点都为 `true` 才为 `true`。
- `requires_worktree: false` 的流程不要求任务级独立工作树：开发会话落在源仓库 checkout，制品留在 `.runtime` 暂存目录，不做迁移。
- 旧配置的 `dispatch.skill.command_templates` 与 `task_source.flows` 会自动映射为等价注册表并给出弃用告警，其中旧 `separate` 按 `complete` 处理。
- 注册表沿用既有的合并规则：用户覆盖层出现 `stages` 或 `flows` 时，整份列表替换托管默认的同名列表，不做按名合并。因此在用户层追加流程需同时完整重述被复用的节点；直接改托管默认配置则只需追加一项。用户层只写旧键时仍按旧结构映射，新旧同时出现以注册表为准。

新增流程的最小改动（只改配置，不改代码）：

```yaml
flows:
  - name: direct
    stages: [direct_dispatch]
  - name: complete
    stages: [complete_dispatch]
    default: true
  - name: proposal
    stages: [proposal_dispatch]
  - name: review                        # 新增：直接复用已有节点
    stages: [complete_dispatch]
```

## 分发与状态

- `launch` 对每个任务按固定顺序执行：① 确保源仓库已在 Orca 注册（按路径在 `orca repo list` 结果里查找，未命中时 `orca repo add --path`）→ ② 解析任务稳定工作区（见下条）→ ③ 复用 dev-spec-gen 的 worktree sync（`--sync-only`，同步未托管内容与 IDE 配置）→ ④ 把 `.runtime` 下的暂存制品迁入工作区并校验 → ⑤ `orca terminal create --worktree id:<worktreeId> --title <工作区名> --command claude` → ⑥ `orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 120000` → ⑦ `orca terminal send --terminal <handle> --text "<command_template 渲染结果>" --enter --wait-submit 10`。制品迁入并校验完成之前不会开启开发会话；会话未在预算内进入空闲状态时只创建、不发送。
- 工作区名是稳定身份：创建时用 `orca worktree create --name <工作区名> --repo id:<repoId> --no-parent --comment orca-task-dispatcher:<assignment_id>`（不带 `--agent`，不在创建时启动任何会话），`--comment` 标记任务与租户归属。创建前先查询同一仓库的工作区：精确同名且仓库、基础分支、归属标记一致时复用工作区；若其中存在活动 Claude 会话，仅当该会话对应 state 里记录的 `initial_prompt_digest` 与本次渲染文本摘要一致才跳过本次物理会话，否则新开一个 Claude 会话继续本流程。分支仍是 Orca 初始名 `<工作区名>` 时视为创建未完成的中间态，复用路径会把它收敛为 `<用户名>/<工作区名>`；其他分支名一律停止。存在 `<工作区名>-<数字>` 自动后缀且其路径、基础分支、归属标记一致、没有活动 Claude 会话、Git 工作区干净时先删除该后缀；无法安全确认活动会话时保留并停止。Orca 返回的工作区名、路径或归属与任务不一致（含自动后缀）时该任务落 `requires_manual_reset`，绝不改名或二次创建。裸建区可能留下一个默认 shell 标签页（`agentIdentity` 为空），它不是开发会话，Dispatcher 不接管也不关闭它。
- 创建工作区时如果创建请求的传输回执丢失（`runtime_unavailable` / `runtime_timeout`，常见于大仓库检出超过 Orca 服务端 30 秒 socket 空闲上限），Dispatcher **不会重发创建**：它在同一次 `launch` 内按只读查询等待结果，周期 5 秒、总预算 600 秒（等待预算从进入等待时起算；等待前的只读查询本身已带配置化重试）。等待期间只接纳同一仓库下唯一精确同名、comment 与基础分支相符、检出已完成的工作区；出现自动后缀名、多个同名、身份不符、状态无法确认或后缀工作区无法证明安全时立即停止。活动 Claude 会话不会单独阻断原名工作区：确认工作区后按首次投递摘要决定是否重复会话。确认为原创建结果后继续分支改名、sync、制品迁入校验与开发会话投递，并在结果里保留 `创建回执未收到…未重复创建` 的 warning；到期仍未确认时该任务落 `requires_manual_reset`（原因 `worktree_creation_unconfirmed`），资源保持不动等待人工核对。
- 分发结果按投递回执判定，不看 CLI 顶层 `ok`：`result.send.accepted` 证明任务文本被接受，`result.send.prompt.stages` 含 `turn_started` 才算观察到本轮起步。接受且观察到起步记 `dispatched`（`dispatch_state=turn_started`）；只接受未起步记 `dispatched`（`dispatch_state=input_accepted`）并保留「未观察到起步」告警。回执缺少 `send` 对象、会话未在 120 秒内进入 `tui-idle`、投递被拒绝、或主机返回 `observation=unsupported`（旧 host 无耐久回执）时该任务落 `requires_manual_reset`，只报告不重发。
- 分发会话只负责[启动验收](SKILL.md#启动验收)：下发文本仍只有流程模板渲染结果，不额外追加监督提示词。`terminal_handle`、`initial_prompt_digest` 与 `send_request_id` 在发送阶段落盘，进程若在投递后中断，`recover` 能区分「未发送」与「发送结果未知」。验收后结束本轮，不等待开发完成、不读取会话输出、不进入持续监督循环。`--wait-submit` 的观察静默不构成重发理由。
- 活动会话判重使用最终渲染文本的 SHA-256（`initial_prompt_digest`），结合工作区 ID、活动终端句柄和已有投递回执核对，不读取终端画面猜测首条消息。摘要相同且已接受时返回 `skipped_duplicate_session`，为本流程保存独立的 `dispatched` 状态和 `duplicate_of_state_key`；摘要相同但首次投递结果未知时不重发，转人工核对。历史记录没有摘要时无法证明重复，允许新开终端。复用有活动会话的工作区时不再运行初始化 `sync-only`，制品仅补缺失文件、不覆盖已有文件，迁入后仍按本流程要求校验。
- 不存在布局配置与 pane 聚合：`dispatch.layout.*`、`dispatch.terminal.shell_commands` 已失效。配置中残留 `dispatch.skill.command_templates` 或 `task_source.flows` 时，会按旧结构自动映射为注册表并在 `validate` 结果的 `config.deprecation_warnings` 中列出弃用告警；其余未知字段照旧忽略。
- 启动命令固定为 `claude`，由 Dispatcher 通过 `orca terminal create --command claude` 显式启动；`dispatch.agent_commands`、`dispatch.agent_extra_args` 与为其预写 `skipDangerousModePermissionPrompt` 的兜底逻辑均已移除。
- 工作区位置由 Orca 决定（`<Orca workspaceDir>/<仓库名>/<工作区名>`），本项目不再有工作区根目录配置；旧配置里的 `workspace.worktrees_root`、项目级 `worktrees_root` 会被忽略。
- `recover` 只做只读核对：按状态里的 `terminal_handle` 调 `orca terminal show` 确认终端仍在、且属于记录的工作区，并且状态里已落盘 `send_accepted=true` 的投递证据；三者齐备才回写为 `recovered`。句柄缺失、终端读不到、终端不属于记录的工作区、或没有已落盘的接受证据时该任务落 `requires_manual_reset`。它不创建终端、不重发文本；带旧 `dispatch_id` 的历史记录只报告并转人工复位，`.runtime/orchestration.json` 与既有 Run 保持不动。

```bash
uv run --project . python scripts/dispatcher.py recover
uv run --project . python scripts/dispatcher.py recover --task-id TASK-123 --tenant-slug <slug> --dispatch-flow direct
uv run --project . python scripts/dispatcher.py reset TASK-123 --dispatch-flow complete
uv run --project . python scripts/dispatcher.py reset TASK-123 --force --dispatch-flow proposal
```

## 安全边界

- 仅将已确认的任务输入交给 `launch`；不要猜测任务 ID、标题、仓库或路径。
- 分支名和任务 ID 会被限制为安全字符；任务标题仅按数据处理，不作为 shell 命令执行。
- Orca 的创建与启动操作不自动重试；调用失败或状态写入失败时该任务落 `requires_manual_reset`，由人工确认后复位，因为无法确认副作用是否已经发生。工作区解析成功后会先写入状态（`worktree_path`、`worktree_id`），终端创建后再写入 `terminal_handle`，发送前写入 `sending`，便于人工核对实际资源与投递阶段。
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
