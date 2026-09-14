# 01 — 配置目录环境变量覆盖默认配置

**What to build:** 在未显式传入 `--config` 时，Dispatcher 自动读取 `ORCA_DISPATCHER_CONFIG_DIR`，从该目录加载 `dispatcher.yaml` 作为客户用户覆盖配置，并继续与托管默认配置合并；显式 `--config` 保持最高优先级，未配置环境变量时保持现有默认行为。

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] 有效的 `ORCA_DISPATCHER_CONFIG_DIR` 能加载目录内 `dispatcher.yaml` 并与托管默认配置按现有规则合并
- [ ] 显式 `--config` 优先于环境变量目录；环境变量未设置、为空、目录不存在或缺少配置文件时保持现有回退行为
- [ ] 无效 YAML 返回明确的配置错误；不读取 `.env` 或客户目录中的其他文件
- [ ] 单元测试覆盖配置优先级、默认回退、路径边界、配置错误和默认层合并
