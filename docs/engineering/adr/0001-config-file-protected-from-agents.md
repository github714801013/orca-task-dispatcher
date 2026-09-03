# 配置文件禁读禁改约束：分发流程禁止 Agent 读取/修改本地配置

2026-09-03，为 orca-task-dispatcher 增加约束：执行分发流程的主 Agent 禁止读取或修改本地配置文件（`config/dispatcher.yaml`），配置信息一律通过 dispatcher.py CLI 子命令输出获取。约束以 SKILL.md 显式规则落地；CLI 本身已只读加载配置，不增加技术强制机制。

- **Status**: accepted
- **Considered Options**: 技术强制（启动时记录配置指纹、流程结束校验）——放弃，配置修改只发生在用户手里，Agent 守规则即可，指纹校验属过度设计；排障例外（配置错误时允许读取定位）——放弃，整体读取正是要禁止的行为，排障一律依据 CLI 错误输出。
- **Consequences**: 配置异常时主 Agent 无法自行读取文件定位，需将 CLI 错误原样转达给用户，由用户排查；未来若 CLI 子命令信息不足，优先扩展 CLI 而非放开读取。该约束只约束执行分发流程的主 Agent，不约束下游 worktree 内的开发 Agent，也不约束用户本人。
