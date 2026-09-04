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

- `workspace.projects_root` 和 `workspace.projects`；项目与分支均可配置 `description`，供分发流程按任务描述自动匹配，纯字符串分支列表亦兼容
- `base_branch.options`
- `task_source.task_url_template`、`query`、`fetch_prompt`
- 需要时的 `dispatch.skill.command_templates`

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
uv run --project . python scripts/dispatcher.py state
uv run --project . python scripts/dispatcher.py branches --repository example-repository
```

推荐流程：

1. 运行 `validate` 验证配置和候选仓库。
2. 运行 `task-source` 获取任务查询提示词，并用当前环境中可用的工具查询真实任务。
3. 按任务标题/描述与 `repos`、`branches` 返回的仓库/分支描述自动匹配目标，描述缺失或无法唯一确定时回退询问用户。
4. `separate` 布局中，为每项任务准备有效的 linked worktree 路径。
5. 写入任务输入 JSON 并执行 `launch`。

所有 CLI 响应在标准输出中只写入一个 JSON 对象；`ok=false` 或非零退出码代表该步骤失败，应先处理失败再继续。

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

`worktree_path` 仅适用于 `separate` 布局，并且必须是源仓库已登记的 linked worktree；未提供有效路径时启动直接失败。`split` 布局不接受该字段，同一项目的任务在项目主仓库 tab 的 pane 中聚合。任务 URL 必须由配置中的 `task_url_template` 生成。`reference_plan` 为可选的参考方案文本，非空时会作为任务信息的一部分随开发请求发送给下游会话。

## 布局与状态

- `separate`：每项任务使用独立 linked worktree 和独立的 `<项目>.<任务编号>` tab；tab 直接绑定该 worktree，以 `--command claude` 启动会话，等待 TUI 就绪后发送开发请求。
- `split`：同一项目的任务在项目主仓库 `<项目>.tabN` 的 pane 中聚合，使用 shell 启动后由 Dispatcher 发送 `claude` 并等待就绪。
- 可配置 `dispatch.agent_extra_args`（如 `--dangerously-skip-permissions`）跳过工具权限弹窗，避免任务命令被权限确认阻塞。
- 任务 worktree 经 `repo add` 新注册后，首次 `terminal create` 若仅因等待 terminal handle 超时，Dispatcher 会等待注册生效并重试一次；其他创建失败不重试。
- 就绪等待（`tui-idle`，超时 `ready_timeout_ms`，默认 120s）后还会读取会话内容（terminal preview）确认任务实际运行，内容为空视为未运行并按 `ready_retry_attempts` 自动重试；命令发送超时按 `send_retry_attempts` 重发，重发可能导致命令被执行两次。重试预算耗尽才标记 `requires_manual_reset`。
- 终端收到任务且本地状态写入成功后，Dispatcher 会将对应 Orca worktree 卡片设为 `in-progress`。
- `dispatched` 任务会被跳过，避免重复发送。
- `launching` 或 `requires_manual_reset` 不会自动重试。确认终端与任务状态后，使用 `reset <task_id>` 清除本地状态；复位 `dispatched` 状态需要明确传入 `--force`。
- 使用 `recover` 仅恢复已分发任务的 Orca 会话；它不会对状态不确定的任务自动重发。

```bash
uv run --project . python scripts/dispatcher.py recover
uv run --project . python scripts/dispatcher.py reset TASK-123
uv run --project . python scripts/dispatcher.py reset TASK-123 --force
```

## 安全边界

- 仅将已确认的任务输入交给 `launch`；不要猜测任务 ID、标题、仓库或路径。
- 分支名和任务 ID 会被限制为安全字符；任务标题仅按数据处理，不作为 shell 命令执行。
- Orca 的创建操作不自动重试（仅对 terminal handle 等待超时特征重试一次），因为超时后无法确认副作用是否已经发生；发送操作仅按 `send_retry_attempts` 配置的次数重试。
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
