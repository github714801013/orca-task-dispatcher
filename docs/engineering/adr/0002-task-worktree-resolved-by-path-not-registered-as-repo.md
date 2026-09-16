# 任务工作区按路径解析：不再注册成独立 Orca repo

2026-09-15，orca-task-dispatcher 调整外部 worktree 的解析方式：不再对未命中的任务工作区执行 `orca repo add --path <worktree 路径>`，改为先 `orca worktree list` 按路径匹配，未命中则用 `orca worktree show --worktree path:<路径>` 解析；两者都失败时，确认源仓库已注册后重试一次 `worktree show`。

- **Status**: accepted
- **Considered Options**: 保留 `repo add --path <worktree>`（它能立刻拿到一个稳定的 worktree id，但把任务工作区从源仓库割裂成独立 repo——实测 `oanew-XSWL-28412-…` 一类条目的 repoId 与源仓库 `oanew` 不同即由此产生；`orca repo` 子命令只有 add/list/show/set-base-ref/search-refs，没有 rm，历史污染无法通过 CLI 清理）——放弃；只保留 `worktree list` 匹配（该仓库 external worktree 可见性为 hide 时，工作区根本不出现在列表里，会让本可解析的任务全部落 `failed_worktree`）——放弃。
- **Consequences**: 隐藏 worktree 的解析依赖 `orca worktree show`（实测对 hide 仓库的外部 worktree 仍可用，且返回的 id 归属源仓库）；终端选择器仍是 `id:<源仓库 repoId>::<路径>`，因此不受可见性影响。历史遗留的假 repo 只能不再新增，无法通过 CLI 回收。可见性本身没有 CLI 设置入口，本项目只解析、不修改。
