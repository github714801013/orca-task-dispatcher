# 候选资格先于仓库映射兜底

2026-09-18，Dispatcher 收紧任务资格边界。原 proposal JQL 因 Jira 不支持的语法解析失败后，外部流程错误地用过宽基础 JQL 取回全部待开发任务，再把没有父子参考方案的任务交给仓库映射兜底，`decide` 只校验显式仓库/分支/快照而接受了错误分发。

- **Status**: accepted
- **Decision**: Jira 节点必须逐任务交接 `candidate_eligible`、`candidate_eligibility_reason`、`reference_plan_source`、参考方案字段和 `candidate_jql`。原始 JQL 与公开规范允许的等价转换再次解析均失败时，当前任务标记不完整并阻断；不得用更宽 JQL 扩大候选。proposal 只有任务或父任务参考方案非空且资格为真才可进入仓库映射；direct/complete 虽可不带参考方案，也必须有资格为真的交接证据。
- **Boundary**: 候选资格是进入 `decide`/`launch` 的硬门槛；仓库映射兜底只处理已合格任务的 repository、tenant、base_branch 不唯一，不能赋予资格。`decide` 不查询 Jira，但拒绝缺资格证据或资格为假的输入；`launch` 对直接手工输入执行同一硬校验。
- **Compatibility**: 代码内直接构造的 `Assignment` 保留测试兼容；所有 `decide` 输入必须提供完整资格证据，标准 `launch_input` 原样保留这些字段。旧 version=1 决策文件缺字段时需重新执行 Jira 候选筛选，不能靠兼容默认继续分发。
