# 任务工作区 CLI 复用 launch 的稳定身份

2026-09-20，Dispatcher 增加 `worktree create` 子命令，让调用方在分发前通过结构化参数直接准备 Orca worktree，而不用手写 Orca/Git/dev-spec-gen 命令链。

- **Status**: accepted
- **Decision**: CLI 输入为 `task-id`、`repository`、可选 `base-branch` 与 `worktree-slug`；执行仓库和分支白名单校验、Orca 仓库注册、稳定名称创建/严格复用、分支改名、dev-spec-gen `--sync-only`。成功结果返回资源身份和 `reused`。
- **Identity**: 工作区名、comment、目标分支和 baseRef 与 launch 使用同一组函数。精确同名且 repo/comment/base/branch 一致才复用；冲突失败，不自动生成后缀。创建回执丢失只读确认，绝不重发。
- **Boundary**: 命令不写 Dispatcher task state（仅复用 `.runtime/launch.lock` 串行化有副作用的创建链路），不迁移需求制品，不创建/复用终端，不发送提示词。已有活动 Claude 会话不阻止返回已验证工作区；后续 launch 自己负责会话判重与任务分发。
- **Base branch**: 显式参数必须命中项目白名单并按配置验证；省略时使用 `base_branch.default`，默认为空时先读取并确认 Orca 仓库默认基础分支，无法确认则阻断，不按任意 baseRef 复用。
