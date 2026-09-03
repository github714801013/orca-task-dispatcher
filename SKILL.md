---
name: orca-task-dispatcher
description: 用 CLI 生成并启动用户确认的任务分发。用于手动或定时将任务分配到仓库并启动 `/dev-spec-gen`。
---

# Orca 任务分发器

`<root>` 是本文件所在目录。统一使用 `uv run --project <root> python <root>/scripts/dispatcher.py <subcommand>`；
每个子命令执行前先查看其 `--help`，并按 CLI 返回的提示准备输入。stdout 为单个 JSON；`ok=false` 或非零退出码时停止。任务获取提示词仅由 `task-source` 子命令输出。

## 硬性约束

- 本地配置文件（`config/dispatcher.yaml`）在执行本技能期间**禁止读取与修改**：任何场景（含配置异常排障）都不得读取或编辑该文件。配置信息一律通过本 CLI 子命令的输出获取；配置异常时仅依据 CLI 错误输出停止并如实转达用户，由用户自行排查文件。

## 使用顺序

1. 运行 `validate`、`repos`、`task-source`。
2. 原样执行 `task-source` 返回的 `fetch_prompt` 获取真实任务；任务获取失败、无结果或字段不足时如实停止。
3. 运行 `state`，跳过 `dispatched`；`launching` 必须先 `reset <task_id>`，不得自动重试。
4. 按任务的标题/描述与 `repos` 返回的仓库描述自动匹配仓库，仅允许 `repos` 返回的名称；再运行 `branches --repository <name>`，按任务描述与分支描述自动匹配分支，仅可选 `valid=true` 分支。描述缺失或无法唯一确定时，用 `AskUserQuestion` 展示带描述的候选项回退询问用户。
5. 按 `dispatcher.py launch --help` 的输入契约写入 `.runtime/current-run.json`。展示汇总后取得对本次 terminal 创建与发送的明确确认。
6. 运行 `launch --input <root>/.runtime/current-run.json`；不等待 `/dev-spec-gen` 完成。

需要注册 Automation 时，先运行对应 `orca automations ... --help`，再单独请求授权。
