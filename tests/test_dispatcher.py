from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dispatcher


CONFIG = """\
workspace:
  projects_root: "{projects_root}"
  projects:
    mapped:
      path: "repo-a"
      base_branches: ["origin/release"]
base_branch:
  options: ["origin/default"]
  validate: false
task_source:
  type: "prompt"
  query: "status = 待开发"
  fetch_prompt: "按 {{{{query}}}} 查询任务"
  session_prompt:
    separate: "任务确认后直接创建或复用 worktree，并写入 worktree_path"
    split: "新建该任务的 worktree"
    recovery: "这是恢复会话"
  task_url_template: "https://jira.example/{{task_id}}"
  max_tasks: 12
interaction:
  repository_selection: "required"
  base_branch_selection: "optional"
  ask_batch_size: 4
dispatch:
  agent: "claude"
  agent_extra_args: ""
  skill:
    command_templates:
      separate: |
        /dev-spec-gen {{task_url}} base_branch={{base_branch}} 在当前任务的worktree中进行工作
        - 任务编号：{{task_id}}
        - 任务标题：“{{title}}”
        - 任务描述：“{{description}}”
        - 负责人：“{{assignee}}”
        - 租户：“{{tenant}}”
        - 分发标识：{{assignment_id}}
        - 参考方案：“{{reference_plan}}”
        - GitNexus 调研报告：{{gitnexus_report_path}}（复用该报告并跳过 GitNexus 调研节点）
        - 完整原始需求快照：{{requirement_snapshot_path}}（先读此快照，再按其相对路径读取正文与附件本体）
      split: |
        /dev-spec-gen {{task_url}} base_branch={{base_branch}} 新建worktree进行工作
        - 任务编号：{{task_id}}
        - 任务标题：“{{title}}”
        - 任务描述：“{{description}}”
        - 负责人：“{{assignee}}”
        - 租户：“{{tenant}}”
        - 分发标识：{{assignment_id}}
        - 参考方案：“{{reference_plan}}”
        - GitNexus 调研报告：{{gitnexus_report_path}}（复用该报告并跳过 GitNexus 调研节点）
        - 完整原始需求快照：{{requirement_snapshot_path}}（先读此快照，再按其相对路径读取正文与附件本体）
  layout:
    group_by: "repository"
    max_panes_per_tab: 4
    mode: "split"
  terminal:
    shell_commands:
      windows: "cmd.exe /d /k"
      posix: "sh -i"
    read_retry_attempts: 2
    read_retry_delay_ms: 1
    ready_retry_attempts: 3
    send_retry_attempts: 1
    ready_timeout_ms: 120000
  concurrency:
    max_agents: 12
dedup:
  enabled: true
  state_file: ".runtime/state.json"
"""


def write_config(root: Path, projects_root: Path, layout_mode: str = "split") -> dispatcher.Config:
    config_path = root / "config" / "dispatcher.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        CONFIG.replace('mode: "split"', f'mode: "{layout_mode}"').format(projects_root=projects_root.as_posix()),
        encoding="utf-8",
    )
    return dispatcher.load_config(config_path)


def assignment(
    task_id: str,
    repository: str,
    path: Path,
    branch: str | None = None,
    worktree_path: Path | None = None,
) -> dispatcher.Assignment:
    return dispatcher.Assignment(
        task=dispatcher.Task(task_id=task_id, title=task_id, task_url=f"https://jira.example/{task_id}"),
        repository=repository,
        repository_path=path,
        base_branch=branch,
        worktree_path=worktree_path,
    )


def create_linked_worktree(repository_path: Path, worktree_path: Path) -> None:
    marker = repository_path / ".dispatcher-test"
    if not marker.exists():
        subprocess.run(("git", "init", str(repository_path)), check=True, capture_output=True, text=True, encoding="utf-8")
        for arguments in (
            ("git", "-C", str(repository_path), "config", "user.email", "dispatcher@example.invalid"),
            ("git", "-C", str(repository_path), "config", "user.name", "Dispatcher Test"),
            ("git", "-C", str(repository_path), "config", "commit.gpgsign", "false"),
        ):
            subprocess.run(arguments, check=True, capture_output=True, text=True, encoding="utf-8")
        marker.write_text("test\n", encoding="utf-8")
        for arguments in (
            ("git", "-C", str(repository_path), "add", ".dispatcher-test"),
            ("git", "-C", str(repository_path), "commit", "--no-gpg-sign", "-m", "test"),
        ):
            subprocess.run(arguments, check=True, capture_output=True, text=True, encoding="utf-8")
    subprocess.run(
        ("git", "-C", str(repository_path), "worktree", "add", "--detach", str(worktree_path)),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def create_requirement_snapshot(worktree_path: Path, task_id: str) -> Path:
    specs_root = worktree_path / "docs" / "engineering" / "specs"
    attachments_root = worktree_path / "docs" / "engineering" / "attachments" / task_id
    specs_root.mkdir(parents=True)
    attachments_root.mkdir(parents=True)
    attachment_path = attachments_root / "001-requirement.txt"
    attachment_path.write_text("附件内容\n", encoding="utf-8")
    attachment_bytes = attachment_path.read_bytes()
    manifest_path = attachments_root / "manifest.json"
    manifest_path.write_text(json.dumps({
        "task_id": task_id,
        "status": "complete",
        "attachments": [{
            "path": attachment_path.name,
            "size": len(attachment_bytes),
            "sha256": hashlib.sha256(attachment_bytes).hexdigest(),
        }],
    }), encoding="utf-8")
    snapshot_path = specs_root / "2026-09-07-test-raw-requirements.md"
    snapshot_path.write_text(
        "---\n"
        f"task_id: {task_id}\n"
        "snapshot_status: complete\n"
        f"attachment_manifest: ../attachments/{task_id}/manifest.json\n"
        "---\n"
        "# 完整需求\n",
        encoding="utf-8",
    )
    return snapshot_path


def make_snapshot_assignment(projects: Path) -> tuple[Path, Path, Path, dispatcher.Assignment]:
    repository_path = projects / "repo-a"
    worktree_path = projects / "repo-a-XSWL-1"
    create_linked_worktree(repository_path, worktree_path)
    snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
    item = dispatcher.Assignment(
        task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
        repository="mapped",
        repository_path=repository_path,
        base_branch=None,
        worktree_path=worktree_path,
        requirement_snapshot_path=snapshot_path,
    )
    return repository_path, worktree_path, snapshot_path, item


class FakeOrca:
    def __init__(
        self,
        repository_ids: dict[Path, str] | None = None,
        failure: str | None = None,
        create_timeout_count: int = 0,
        create_side_effect_timeout_count: int = 0,
        wait_timeout_count: int = 0,
        wait_timeout_after_claude_count: int = 0,
        send_timeout_count: int = 0,
        show_empty_count: int = 0,
        show_empty_after_claude_count: int = 0,
        authorization_prompt_count: int = 0,
        authorization_stays_visible: bool = False,
    ) -> None:
        self.operations: list[tuple[str, str]] = []
        self.repository_ids = {
            os.path.normcase(os.path.normpath(str(path.resolve()))): repository_id
            for path, repository_id in (repository_ids or {}).items()
        }
        self.failure = failure
        self._create_timeouts_left = create_timeout_count
        self._create_side_effect_timeouts_left = create_side_effect_timeout_count
        self._wait_timeouts_left = wait_timeout_count
        self._wait_timeouts_after_claude_left = wait_timeout_after_claude_count
        self._send_timeouts_left = send_timeout_count
        self._show_empty_left = show_empty_count
        self._show_empty_after_claude_left = show_empty_after_claude_count
        self._authorization_prompts_left = authorization_prompt_count
        self._authorization_stays_visible = authorization_stays_visible
        self.next_handle = 1
        self.snapshots: dict[str, dispatcher.TerminalSnapshot] = {}
        self.worktrees: dict[str, dispatcher.OrcaWorktree] = {}

    def status(self) -> None:
        self.operations.append(("status", ""))

    def repo_ids(self) -> dict[str, str]:
        self.operations.append(("repo-list", ""))
        return self.repository_ids

    def repo_add(self, repository: dispatcher.Repository) -> None:
        path = os.path.normcase(os.path.normpath(str(repository.path.resolve())))
        self.operations.append(("repo-add", path))
        self.repository_ids[path] = f"repo-{repository.name}"

    def worktree_resolve(self, path: Path) -> dispatcher.OrcaWorktree:
        resolved = path.resolve()
        self.operations.append(("worktree-resolve", resolved.as_posix()))
        self._raise_if("worktree-resolve")
        key = os.path.normcase(resolved.as_posix())
        existing = self.worktrees.get(key)
        if existing is not None:
            return existing
        worktree = dispatcher.OrcaWorktree(f"repo-{path.name}::{resolved.as_posix()}", resolved)
        self.worktrees[key] = worktree
        return worktree

    def worktree_list(self) -> tuple[dispatcher.OrcaWorktree, ...]:
        self.operations.append(("worktree-list", ""))
        self._raise_if("worktree-list")
        return tuple(self.worktrees.values())

    def worktree_set_in_progress(self, worktree_path: Path) -> None:
        self.operations.append(("worktree-status", worktree_path.resolve().as_posix()))
        self._raise_if("workspace-status")

    def terminal_create(
        self,
        worktree_selector: str,
        title: str,
        command: str,
    ) -> str:
        self.operations.append(("create", f"{worktree_selector}:{title}:{command}"))
        if self._create_timeouts_left > 0:
            self._create_timeouts_left -= 1
            raise dispatcher.DispatcherError(
                "orca_command_failed",
                "{'code': 'runtime_error', 'message': 'Timed out waiting for terminal handle after creation'}",
            )
        self._raise_if("create")
        if worktree_selector.startswith("id:"):
            worktree_id = worktree_selector.removeprefix("id:")
            worktree_path = Path(worktree_id.split("::", 1)[-1])
        else:
            worktree_path = Path(worktree_selector.removeprefix("path:"))
            worktree_id = f"repo-{worktree_path.name}::{worktree_path.resolve().as_posix()}"
        handle = self._handle()
        self.snapshots[handle] = dispatcher.TerminalSnapshot(
            handle=handle,
            worktree_id=worktree_id,
            worktree_path=worktree_path,
            tab_id=f"tab-{handle}",
            leaf_id=f"leaf-{handle}",
            title=title,
            connected=True,
            writable=True,
            agent_identity="claude" if command.split(maxsplit=1)[0] == "claude" else None,
            preview=(
                "No, exit\nYes, I accept"
                if command.split(maxsplit=1)[0] == "claude" and self._authorization_prompts_left > 0
                else "claude tui" if command.split(maxsplit=1)[0] == "claude" else "PS D:\\repo>"
            ),
        )
        if command.split(maxsplit=1)[0] == "claude" and self._authorization_prompts_left > 0:
            self._authorization_prompts_left -= 1
        if self._create_side_effect_timeouts_left > 0:
            self._create_side_effect_timeouts_left -= 1
            raise dispatcher.DispatcherError(
                "orca_command_failed",
                "{'code': 'runtime_error', 'message': 'Timed out waiting for terminal handle after creation'}",
            )
        return handle

    def terminal_split(self, handle: str, direction: str, command: str) -> str:
        self.operations.append((f"split-{direction}", f"{handle}:{command}"))
        self._raise_if("split")
        parent = self.snapshots[handle]
        created_handle = self._handle()
        self.snapshots[created_handle] = dispatcher.TerminalSnapshot(
            handle=created_handle,
            worktree_id=parent.worktree_id,
            worktree_path=parent.worktree_path,
            tab_id=parent.tab_id,
            leaf_id=f"leaf-{created_handle}",
            title="Terminal",
            connected=True,
            writable=True,
            agent_identity=None,
            preview="PS D:\\repo>",
        )
        return created_handle

    def terminal_rename(self, handle: str, title: str) -> None:
        self.operations.append(("rename", f"{handle}:{title}"))
        self._raise_if("rename")

    def terminal_show(self, handle: str) -> dispatcher.TerminalSnapshot:
        self.operations.append(("show", handle))
        self._raise_if("show")
        snapshot = self.snapshots[handle]
        if snapshot.agent_identity == "claude" and self._show_empty_after_claude_left > 0:
            self._show_empty_after_claude_left -= 1
            return dispatcher.TerminalSnapshot(**{**snapshot.__dict__, "preview": ""})
        if self._show_empty_left > 0:
            self._show_empty_left -= 1
            return dispatcher.TerminalSnapshot(**{**snapshot.__dict__, "preview": ""})
        return snapshot

    def terminal_list(self, repository: dispatcher.Repository) -> tuple[dispatcher.TerminalSnapshot, ...]:
        self.operations.append(("list", repository.path.as_posix()))
        self._raise_if("list")
        return tuple(snapshot for snapshot in self.snapshots.values() if snapshot.worktree_path == repository.path)

    def terminal_wait(self, handle: str, timeout_ms: int) -> None:
        self.operations.append(("wait", f"{handle}:{timeout_ms}"))
        self._raise_if("wait")
        snapshot = self.snapshots.get(handle)
        if snapshot is not None and snapshot.agent_identity == "claude":
            if self._wait_timeouts_after_claude_left > 0:
                self._wait_timeouts_after_claude_left -= 1
                raise dispatcher.DispatcherError("orca_not_ready", f"Claude terminal 未在 {timeout_ms}ms 内就绪：{handle}")
            return
        if self._wait_timeouts_left > 0:
            self._wait_timeouts_left -= 1
            raise dispatcher.DispatcherError("orca_not_ready", f"Claude terminal 未在 {timeout_ms}ms 内就绪：{handle}")

    def terminal_send(self, handle: str, text: str) -> None:
        self.operations.append(("send", text))
        self._raise_if("send")
        if self._send_timeouts_left > 0:
            self._send_timeouts_left -= 1
            raise dispatcher.DispatcherError("orca_timeout", "Orca CLI 调用超时：terminal send")
        if text == dispatcher.CLAUDE_AUTHORIZATION_ACCEPT and not self._authorization_stays_visible:
            snapshot = self.snapshots[handle]
            self.snapshots[handle] = dispatcher.TerminalSnapshot(
                **{**snapshot.__dict__, "preview": "claude tui"}
            )
        if text.startswith("claude"):
            snapshot = self.snapshots[handle]
            self.snapshots[handle] = dispatcher.TerminalSnapshot(
                **{**snapshot.__dict__, "agent_identity": "claude", "preview": "claude tui"}
            )

    def restore_handle(self, handle: str, restored_handle: str, agent_identity: str | None = "claude", preview: str = "") -> None:
        snapshot = self.snapshots.pop(handle)
        self.snapshots[restored_handle] = dispatcher.TerminalSnapshot(
            handle=restored_handle,
            worktree_id=snapshot.worktree_id,
            worktree_path=snapshot.worktree_path,
            tab_id=snapshot.tab_id,
            leaf_id=snapshot.leaf_id,
            title=snapshot.title,
            connected=True,
            writable=True,
            agent_identity=agent_identity,
            preview=preview,
        )

    def _handle(self) -> str:
        handle = f"term-{self.next_handle}"
        self.next_handle += 1
        return handle

    def _raise_if(self, operation: str) -> None:
        if self.failure == operation:
            raise dispatcher.DispatcherError("orca_command_failed", f"模拟 {operation} 失败")


class DispatcherTests(unittest.TestCase):
    def test_retry_read_retries_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            attempts = 0

            def operation() -> str:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise dispatcher.DispatcherError("orca_command_failed", "瞬时失败")
                return "ok"

            self.assertEqual(dispatcher.retry_read(config, operation), "ok")
            self.assertEqual(attempts, 2)

    def test_retry_read_does_not_exceed_configured_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            attempts = 0

            def operation() -> None:
                nonlocal attempts
                attempts += 1
                raise dispatcher.DispatcherError("orca_command_failed", "持续失败")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "持续失败"):
                dispatcher.retry_read(config, operation)
            self.assertEqual(attempts, config.read_retry_attempts)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)

            result = dispatcher.task_source_prompt(config)

            self.assertEqual(result, {
                "type": "prompt",
                "fetch_prompt": "按 status = 待开发 查询任务",
                "session_prompt": "新建该任务的 worktree",
                "task_url_template": "https://jira.example/{task_id}",
                "max_tasks": 12,
                "reference_plan_field": "customfield_11103",
                "query": "status = 待开发",
                "jql_source": "config",
                "flow": "complete",
                "next_steps": ["拉取完整需求与附件", "执行需求/GitNexus 调研", "创建或复用 worktree", "生成 version=1 decide 输入", "通过 decide 后 launch"],
            })

    def test_task_source_rejects_unknown_prompt_variable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace("{{{{query}}}}", "{{{{unknown}}}}").format(
                    projects_root=projects.as_posix()
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "未知变量"):
                dispatcher.task_source_prompt(dispatcher.load_config(config_path))

    def test_task_source_command_emits_rendered_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            write_config(root, projects)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = dispatcher.main(["--config", str(root / "config" / "dispatcher.yaml"), "task-source", "--flow", "complete"])

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["result"]["fetch_prompt"], "按 status = 待开发 查询任务")

    def test_task_source_reference_plan_field_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace(
                    'fetch_prompt: "按 {{{{query}}}} 查询任务"',
                    'reference_plan_field: "customfield_12345"\n  fetch_prompt: "按 {{{{query}}}} 查询任务；参考方案字段：{{{{reference_plan_field}}}}"',
                ).format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )
            config = dispatcher.load_config(config_path)

            prompt = dispatcher.task_source_prompt(config)

            self.assertIn("customfield_12345", prompt["fetch_prompt"])
            self.assertEqual(prompt["reference_plan_field"], "customfield_12345")

    def test_config_rejects_non_string_reference_plan_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace(
                    'fetch_prompt: "按 {{{{query}}}} 查询任务"',
                    'reference_plan_field: 123\n  fetch_prompt: "按 {{{{query}}}} 查询任务"',
                ).format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "reference_plan_field 必须是字符串或 null"):
                dispatcher.load_config(config_path)

    def test_example_task_source_requires_dispatch_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "dispatcher.example.yaml"
            config_path.write_text(
                (ROOT / "config" / "dispatcher.example.yaml").read_text(encoding="utf-8").replace(
                    "/path/to/projects",
                    Path(temporary).as_posix(),
                ),
                encoding="utf-8",
            )
            prompt = dispatcher.task_source_prompt(dispatcher.load_config(config_path))["fetch_prompt"]
            session_prompt = dispatcher.task_source_prompt(dispatcher.load_config(config_path))["session_prompt"]

        self.assertIn("task_id（事项唯一标识）、title（标题）和 assignee", prompt)
        self.assertIn("父任务解析", prompt)
        self.assertIn("source_task_id、source_assignee", prompt)
        self.assertIn("不得用父任务负责人覆盖 source_assignee", prompt)
        self.assertIn("人员—项目/技术栈", prompt)
        self.assertIn("参考方案为空、缺失、冲突或无法映射到配置候选时，不构成阻断", prompt)
        self.assertIn("仍可依据父产品需求、任务标题/描述、GitNexus 报告和项目/分支描述联合决策", prompt)
        self.assertIn("仅当这些证据仍不能唯一确定项目、租户或基础分支时，才请求用户确认", prompt)
        self.assertIn("可作为唯一锁定 oanew/master-new 的强路由证据", prompt)
        self.assertIn("一次跨项目只读远程 query", prompt)
        self.assertIn("不得先锁定仓库", prompt)
        self.assertIn("不得创建 worktree", prompt)
        self.assertIn("联合判断", prompt)
        self.assertIn("报告先暂存", prompt)

        self.assertIn("严禁修改", prompt)
        self.assertIn("参考方案字段：配置值为 customfield_11103", prompt)
        self.assertIn("可省略 reference_plan", prompt)
        self.assertIn("branch_priority", prompt)
        self.assertNotIn("created", prompt)
        self.assertIn("worktree_path", session_prompt)
        self.assertIn("GitNexus 调研报告必须先从 gitnexus_report_path", session_prompt)
        self.assertIn("完整技能正文", session_prompt)
        self.assertIn("worktree.py --help", session_prompt)
        self.assertIn("orca worktree create", session_prompt)
        self.assertIn("status=success", session_prompt)
        self.assertNotIn("jira.9ji.com", prompt)

    def test_requirement_snapshot_is_optional_for_legacy_direct_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                tenant="legacy",
                tenant_slug="legacy",
                worktree_path=worktree_path,
            )

            dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_root = worktree_path / "docs" / "engineering" / "specs"
            snapshot_root.mkdir(parents=True)
            target = snapshot_root / "2026-09-07-test-raw-requirements.md"
            target.write_text("# 完整需求\nsnapshot_status: complete\n附件完整性: complete\n", encoding="utf-8")
            link = snapshot_root / "link.md"
            link.symlink_to(target)
            config = write_config(root, projects, layout_mode="separate")
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                worktree_path=worktree_path,
                requirement_snapshot_path=link,
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "现有普通文件"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_path_must_be_in_task_worktree_specs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
            config = write_config(root, projects, layout_mode="separate")
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1", "描述正文由快照承载"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                worktree_path=worktree_path,
                requirement_snapshot_path=snapshot_path,
            )

            dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})
            command = dispatcher.command_for(config, item, "separate")
            self.assertIn("完整原始需求快照", command)
            self.assertNotIn("描述正文由快照承载", command)

    def test_requirement_snapshot_rejects_incomplete_snapshot_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path, _, snapshot_path, item = make_snapshot_assignment(projects)
            snapshot_path.write_text(
                snapshot_path.read_text(encoding="utf-8").replace("snapshot_status: complete", "snapshot_status: incomplete"),
                encoding="utf-8",
            )
            config = write_config(root, projects, layout_mode="separate")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "未标记为完整"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_rejects_incomplete_manifest_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path, worktree_path, _, item = make_snapshot_assignment(projects)
            manifest_path = worktree_path / "docs" / "engineering" / "attachments" / "XSWL-1" / "manifest.json"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8").replace('"status": "complete"', '"status": "incomplete"'),
                encoding="utf-8",
            )
            config = write_config(root, projects, layout_mode="separate")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "附件清单未标记为完整"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_rejects_attachment_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path, worktree_path, _, item = make_snapshot_assignment(projects)
            attachment_path = worktree_path / "docs" / "engineering" / "attachments" / "XSWL-1" / "001-requirement.txt"
            attachment_path.write_text("被篡改的附件内容\n", encoding="utf-8")
            config = write_config(root, projects, layout_mode="separate")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "附件完整性校验失败"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_rejects_attachment_size_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path, worktree_path, _, item = make_snapshot_assignment(projects)
            manifest_path = worktree_path / "docs" / "engineering" / "attachments" / "XSWL-1" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["attachments"][0]["size"] -= 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            config = write_config(root, projects, layout_mode="separate")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "附件完整性校验失败"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_rejects_missing_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path, worktree_path, _, item = make_snapshot_assignment(projects)
            (worktree_path / "docs" / "engineering" / "attachments" / "XSWL-1" / "001-requirement.txt").unlink()
            config = write_config(root, projects, layout_mode="separate")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "附件文件不存在或越出归档目录"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_rejects_manifest_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path, _, snapshot_path, item = make_snapshot_assignment(projects)
            snapshot_path.write_text(
                snapshot_path.read_text(encoding="utf-8").replace(
                    "attachment_manifest: ../attachments/XSWL-1/manifest.json",
                    "attachment_manifest: ../../../../manifest.json",
                ),
                encoding="utf-8",
            )
            config = write_config(root, projects, layout_mode="separate")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "附件清单必须位于任务附件目录"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_rejects_symlink_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path, worktree_path, _, item = make_snapshot_assignment(projects)
            attachments_root = worktree_path / "docs" / "engineering" / "attachments" / "XSWL-1"
            target = attachments_root / "001-requirement.txt"
            (attachments_root / "002-link.txt").symlink_to(target)
            manifest_path = attachments_root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["attachments"].append({
                "path": "002-link.txt",
                "size": target.stat().st_size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            })
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            config = write_config(root, projects, layout_mode="separate")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "附件文件必须是现有普通文件"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_decide_blocks_tenant_task_without_complete_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace('base_branches: ["origin/release"]', '''tenants:
        九机:
          slug: "jiuji"
      base_branches: ["origin/release"]''').format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )
            config = dispatcher.load_config(config_path)
            worktree_path = projects / "repo-a-XSWL-2-jiuji"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-2")
            snapshot_path.write_text(
                snapshot_path.read_text(encoding="utf-8").replace("snapshot_status: complete", "snapshot_status: incomplete"),
                encoding="utf-8",
            )
            path = root / "decision.json"
            path.write_text(json.dumps({"version": 1, "tasks": [
                {
                    "task_id": "XSWL-1", "title": "测试", "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped", "tenant": "九机", "tenant_slug": "jiuji",
                    "base_branch": "origin/release",
                },
                {
                    "task_id": "XSWL-2", "title": "测试", "task_url": "https://jira.example/XSWL-2",
                    "repository": "mapped", "tenant": "九机", "tenant_slug": "jiuji",
                    "base_branch": "origin/release",
                    "worktree_path": worktree_path.as_posix(),
                    "requirement_snapshot_path": snapshot_path.as_posix(),
                },
            ]}), encoding="utf-8")

            result = dispatcher.decide(config, path)

            self.assertEqual(result["status"], "needs_confirmation")
            self.assertIn("worktree_path", result["tasks"][0]["reason"])
            self.assertIn("未标记为完整", result["tasks"][1]["reason"])

    def test_project_branch_priority_prefers_saas_for_project_tenants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace('base_branches: ["origin/release"]', '''tenants:
        九机:
          slug: "jiuji"
        九讯云:
          slug: "jiuxun"
      branch_priority:
        - when_all: ["九机", "九讯云"]
          branch: "origin/release_saas"
      base_branches:
        origin/release: "九机"
        origin/release_saas: "九讯云"''').format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )
            config = dispatcher.load_config(config_path)
            worktree_jiuji = projects / "repo-a-XSWL-1-jiuji"
            worktree_jiuxun = projects / "repo-a-XSWL-1-jiuxun"
            create_linked_worktree(repository_path, worktree_jiuji)
            create_linked_worktree(repository_path, worktree_jiuxun)
            snapshot_jiuji = create_requirement_snapshot(worktree_jiuji, "XSWL-1")
            snapshot_jiuxun = create_requirement_snapshot(worktree_jiuxun, "XSWL-1")
            path = root / "decision.json"
            path.write_text(json.dumps({"version": 1, "tasks": [
                {
                    "task_id": "XSWL-1", "title": "测试", "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped", "tenant": "九机", "tenant_slug": "jiuji",
                    "worktree_path": worktree_jiuji.as_posix(),
                    "requirement_snapshot_path": snapshot_jiuji.as_posix(),
                },
                {
                    "task_id": "XSWL-1", "title": "测试", "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped", "tenant": "九讯云", "tenant_slug": "jiuxun",
                    "worktree_path": worktree_jiuxun.as_posix(),
                    "requirement_snapshot_path": snapshot_jiuxun.as_posix(),
                },
            ]}), encoding="utf-8")

            result = dispatcher.decide(config, path)

            self.assertEqual(result["status"], "ready")
            self.assertEqual([item["base_branch"] for item in result["tasks"]], ["origin/release_saas", "origin/release_saas"])

    def test_read_assignments_accepts_same_task_for_distinct_tenants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps({"tasks": [
                {
                    "task_id": "XSWL-1", "title": "测试", "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped", "repository_path": "D:/repo-a", "base_branch": None,
                    "tenant": "租户甲", "tenant_slug": "tenant-a",
                },
                {
                    "task_id": "XSWL-1", "title": "测试", "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped", "repository_path": "D:/repo-a", "base_branch": None,
                    "tenant": "租户乙", "tenant_slug": "tenant-b",
                },
            ]}), encoding="utf-8")

            assignments = dispatcher.read_assignments(path)

            self.assertEqual([item.assignment_id for item in assignments], ["XSWL-1::tenant-a", "XSWL-1::tenant-b"])

    def test_read_assignments_accepts_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps({"tasks": [{
                "task_id": "XSWL-1",
                "title": "测试",
                "task_url": "https://jira.example/XSWL-1",
                "repository": "mapped",
                "repository_path": "D:/repo-a",
                "base_branch": None,
            }]}), encoding="utf-8")

            assignments = dispatcher.read_assignments(path)

            self.assertEqual([item.task.task_id for item in assignments], ["XSWL-1"])

    def test_decide_normalizes_explicit_project_without_runtime_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            path = root / "decision.json"
            path.write_text(json.dumps({"version": 1, "tasks": [{
                "task_id": "XSWL-1",
                "title": "实际产品需求",
                "description": "任务描述",
                "task_url": "https://jira.example/XSWL-1",
                "repository": "mapped",
                "base_branch": "origin/release",
                "assignee": "当前负责人",
                "source_task_id": "XSWL-2",
                "source_assignee": "当前负责人",
                "parent_task_id": "XSWL-1",
                "parent_assignee": "父任务负责人",
                "reference_plan": "参考方案",
            }]}), encoding="utf-8")

            result = dispatcher.decide(config, path)

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["tasks"][0]["repository_path"], repository_path.resolve().as_posix())
            self.assertEqual(result["tasks"][0]["source_task_id"], "XSWL-2")
            self.assertEqual(result["launch_input"], {"tasks": result["tasks"]})
            self.assertFalse((root / ".runtime" / "state.json").exists())

    def test_decide_leaves_unresolved_task_for_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            path = root / "decision.json"
            path.write_text(json.dumps({"version": 1, "tasks": [{
                "task_id": "XSWL-1",
                "title": "待确认",
                "task_url": "https://jira.example/XSWL-1",
            }]}), encoding="utf-8")

            result = dispatcher.decide(config, path)

            self.assertEqual(result["status"], "needs_confirmation")
            self.assertEqual(result["tasks"][0]["status"], "needs_confirmation")

    def test_decide_preserves_selected_tasks_when_another_task_needs_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            path = root / "decision.json"
            path.write_text(json.dumps({"version": 1, "tasks": [
                {
                    "task_id": "XSWL-1",
                    "title": "已确认",
                    "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped",
                    "base_branch": "origin/release",
                },
                {
                    "task_id": "XSWL-2",
                    "title": "待确认",
                    "task_url": "https://jira.example/XSWL-2",
                },
            ]}), encoding="utf-8")

            result = dispatcher.decide(config, path)

            self.assertEqual(result["status"], "needs_confirmation")
            self.assertEqual(result["tasks"][0]["status"], "selected")
            self.assertEqual(result["launch_input"]["tasks"][0]["task_id"], "XSWL-1")
            self.assertEqual(result["tasks"][1]["status"], "needs_confirmation")

    def test_decide_rejects_unknown_task_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decision.json"
            path.write_text(json.dumps({"version": 1, "tasks": [{
                "task_id": "XSWL-1",
                "title": "测试",
                "task_url": "https://jira.example/XSWL-1",
                "unexpected": True,
            }]}), encoding="utf-8")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "决策任务包含未知字段"):
                dispatcher.read_decision_input(path)

    def test_read_assignments_rejects_legacy_assignments(self) -> None:
        for payload in (
            {"assignments": []},
            {"tasks": [], "assignments": []},
        ):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "input.json"
                path.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(dispatcher.DispatcherError, "仅支持 tasks"):
                    dispatcher.read_assignments(path)

    def test_read_assignments_rejects_non_list_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps({"tasks": {}}), encoding="utf-8")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "tasks 列表"):
                dispatcher.read_assignments(path)

    def test_read_assignments_rejects_unknown_top_level_field(self) -> None:
        for payload in (
            {"tasks": [], "unexpected": True},
            {"tasks": [], "results": []},
            {"tasks": [], "updated_at": "2026-09-01T00:00:00+00:00"},
        ):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "input.json"
                path.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(dispatcher.DispatcherError, "未知顶层字段"):
                    dispatcher.read_assignments(path)

    def test_task_rejects_shell_metacharacter_in_id(self) -> None:
        with self.assertRaisesRegex(dispatcher.DispatcherError, "task_id 格式"):
            dispatcher.Task.from_dict({
                "task_id": "XSWL-1;whoami",
                "title": "测试",
                "task_url": "https://jira.example/XSWL-1;whoami",
            })

    def test_assignment_rejects_url_not_generated_from_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/other"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "task_url 必须由 task_url_template 生成"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_task_url_template_requires_task_id_placeholder(self) -> None:
        with self.assertRaisesRegex(dispatcher.DispatcherError, r"必须且只能包含一个 \{task_id\}"):
            dispatcher.task_url_for("https://jira.example/static", "XSWL-1")

    def test_detect_claude_authorization_prompt_requires_exact_nearby_options(self) -> None:
        self.assertTrue(dispatcher.detect_claude_authorization_prompt("\x1b[32m> No, exit\x1b[0m\r\n❯ Yes, I accept"))
        self.assertFalse(dispatcher.detect_claude_authorization_prompt("Yes, I accept"))
        self.assertFalse(dispatcher.detect_claude_authorization_prompt("No, exit\n继续\n继续\n继续\nYes, I accept"))
        self.assertFalse(dispatcher.detect_claude_authorization_prompt("No, exit\nYes, I Accept"))

    def test_separate_launch_accepts_exact_claude_authorization_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({worktree_path: "repo-worktree"}, authorization_prompt_count=1)
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            self.assertEqual(result["results"][0]["status"], "dispatched")
            self.assertEqual(sends[0], dispatcher.CLAUDE_AUTHORIZATION_ACCEPT)
            self.assertTrue(sends[1].startswith("/dev-spec-gen"))

    def test_authorization_prompt_that_persists_requires_manual_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(
                {worktree_path: "repo-worktree"},
                authorization_prompt_count=1,
                authorization_stays_visible=True,
            )
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertEqual(sends, [dispatcher.CLAUDE_AUTHORIZATION_ACCEPT])

    def test_task_source_jql_override_is_not_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)

            result = dispatcher.task_source_prompt(config, "project = X AND status = ready")

            self.assertEqual(result["query"], "project = X AND status = ready")
            self.assertEqual(result["jql_source"], "cli")
            self.assertIn("project = X AND status = ready", result["fetch_prompt"])
            self.assertEqual(config.task_source_query, "status = 待开发")

    def test_task_source_rejects_blank_jql_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "jql 必须是非空字符串"):
                dispatcher.task_source_prompt(config, "   ")

    def test_merge_config_values_recursively_replaces_lists_and_null(self) -> None:
        merged = dispatcher.merge_config_values(
            {"nested": {"keep": 1, "replace": 1}, "items": [1, 2], "value": "default"},
            {"nested": {"replace": 2}, "items": [], "value": None},
        )

        self.assertEqual(merged, {"nested": {"keep": 1, "replace": 2}, "items": [], "value": None})

    def test_task_source_cli_accepts_jql_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = dispatcher.main([
                    "--config", str(root / "config" / "dispatcher.yaml"),
                    "task-source", "--flow", "complete", "--jql", "issuetype = 开发需求",
                ])

            self.assertEqual(code, 0)
            result = json.loads(stdout.getvalue())["result"]
            self.assertEqual(result["query"], "issuetype = 开发需求")
            self.assertEqual(result["jql_source"], "cli")

    def test_configured_project_accepts_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            projects.mkdir()
            repository_path = root / "external" / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace('path: "repo-a"', f'path: "{repository_path.as_posix()}"').format(
                    projects_root=projects.as_posix()
                ),
                encoding="utf-8",
            )
            config = dispatcher.load_config(config_path)

            repositories = dispatcher.discover_repositories(config)

            self.assertEqual(repositories[0].path, repository_path.resolve())

    def test_configured_project_rejects_relative_parent_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace('path: "repo-a"', 'path: "../repo-a"').format(
                    projects_root=projects.as_posix()
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "不能越出 projects_root"):
                dispatcher.discover_repositories(dispatcher.load_config(config_path))

    def test_command_rejects_unsafe_base_branch(self) -> None:
        config = dispatcher.Config(
            root=Path("."), projects_root=Path("."), projects={}, branch_options=(), validate_branch=False,
            max_tasks=1, max_agents=1, max_panes=1, ready_timeout_ms=1000, read_retry_attempts=1, read_retry_delay_ms=0, ready_retry_attempts=0, send_retry_attempts=0, shell_command="cmd.exe /d /k", agent_extra_args="", state_file=Path("state.json"),
            task_url_template="https://jira.example/{task_id}", task_source_type="prompt",
            task_source_query="", fetch_prompt="", agent_command="claude",
            command_templates={"separate": "/dev-spec-gen {task_url} base_branch={base_branch}", "split": "/dev-spec-gen {task_url}"},
        )
        item = dispatcher.Assignment(
            task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
            repository="repo",
            repository_path=Path("repo"),
            base_branch="main & whoami",
        )

        with self.assertRaisesRegex(dispatcher.DispatcherError, "base_branch 包含不支持的命令字符"):
            dispatcher.command_for(config, item, "separate")

    def test_command_template_accepts_task_context_placeholders(self) -> None:
        dispatcher.validate_command_template("/dev-spec-gen {task_url} {title} {requirement_snapshot_path}")

    def test_command_template_rejects_unknown_placeholder(self) -> None:
        with self.assertRaisesRegex(dispatcher.DispatcherError, "不支持占位符：unknown"):
            dispatcher.validate_command_template("/dev-spec-gen {task_url} {unknown}")

    def test_command_template_must_start_with_dev_spec_gen(self) -> None:
        with self.assertRaisesRegex(dispatcher.DispatcherError, "必须以 /dev-spec-gen 开头"):
            dispatcher.validate_command_template("任务编号：{task_id}\n/dev-spec-gen {task_url}")

    def test_command_omits_source_and_parent_task_info(self) -> None:
        config = dispatcher.Config(
            root=Path("."), projects_root=Path("."), projects={}, branch_options=(), validate_branch=False,
            max_tasks=1, max_agents=1, max_panes=1, ready_timeout_ms=1000, read_retry_attempts=1, read_retry_delay_ms=0, ready_retry_attempts=0, send_retry_attempts=0, shell_command="cmd.exe /d /k", agent_extra_args="", state_file=Path("state.json"),
            task_url_template="https://jira.example/{task_id}", task_source_type="prompt",
            task_source_query="", fetch_prompt="", agent_command="claude",
            command_templates={"separate": "/dev-spec-gen {task_url}\n- 任务编号：{task_id}\n- 任务标题：“{title}”", "split": "/dev-spec-gen {task_url}"},
        )
        item = dispatcher.Assignment(
            task=dispatcher.Task("XSWL-1", "产品需求标题", "https://jira.example/XSWL-1"),
            repository="repo",
            repository_path=Path("repo"),
            base_branch=None,
            source_task_id="XSWL-28372",
            source_assignee="谢熊坤",
            parent_task_id="XSWL-1",
            parent_assignee="李飞",
        )

        command = dispatcher.command_for(config, item, "separate")

        self.assertIn("- 任务编号：XSWL-1", command)
        self.assertIn("产品需求标题", command)
        self.assertNotIn("XSWL-28372", command)
        self.assertNotIn("谢熊坤", command)
        self.assertNotIn("李飞", command)
        self.assertNotIn("来源", command)
        self.assertNotIn("父", command)

    def test_command_rejects_invalid_template(self) -> None:
        config = dispatcher.Config(
            root=Path("."), projects_root=Path("."), projects={}, branch_options=(), validate_branch=False,
            max_tasks=1, max_agents=1, max_panes=1, ready_timeout_ms=1000, read_retry_attempts=1, read_retry_delay_ms=0, ready_retry_attempts=0, send_retry_attempts=0, shell_command="cmd.exe /d /k", agent_extra_args="", state_file=Path("state.json"),
            task_url_template="https://jira.example/{task_id}", task_source_type="prompt",
            task_source_query="", fetch_prompt="", agent_command="claude",
            command_templates={"separate": "/dev-spec-gen {task_url", "split": "/dev-spec-gen {task_url}"},
        )

        with self.assertRaisesRegex(dispatcher.DispatcherError, "模板格式不合法"):
            dispatcher.validate_command_template(config.command_templates["separate"])

    def test_discovery_uses_only_explicit_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            for name in ("repo-a", "repo-b"):
                (projects / name / ".git").mkdir(parents=True)
            (projects / "ignored" / ".git").mkdir(parents=True)
            config = write_config(root, projects)

            repositories = dispatcher.discover_repositories(config)

            self.assertEqual([repository.name for repository in repositories], ["mapped"])
            self.assertEqual(config.branches_for("mapped"), ("origin/release",))
            self.assertEqual(config.branches_for("repo-b"), ("origin/default",))

    def test_project_lookup_returns_only_explicit_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            arguments = dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"), "project", "--name", "mapped"
            ])

            result = dispatcher.execute(arguments)

            self.assertEqual(result["repository"]["name"], "mapped")
            self.assertEqual(result["base_branches"], [{"name": "origin/release", "description": None}])

    def test_project_lookup_recurses_but_excludes_linked_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "nested" / "custom-service" / ".git").mkdir(parents=True)
            linked_worktree = projects / "nested" / "custom-worktree"
            linked_worktree.mkdir(parents=True)
            (linked_worktree / ".git").write_text("gitdir: /outside", encoding="utf-8")
            config = write_config(root, projects)
            arguments = dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"), "project", "--name", "custom"
            ])

            result = dispatcher.execute(arguments)

            self.assertEqual(result["match_mode"], "recursive")
            self.assertEqual(result["repository"]["name"], "custom-service")

    def test_branches_supports_single_recursive_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            (projects / "nested" / "custom-service" / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            arguments = dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"), "branches", "--repository", "custom-service"
            ])

            result = dispatcher.execute(arguments)

            self.assertEqual(result["repository"]["name"], "custom-service")
            self.assertEqual(result["branches"], [{"name": "origin/default", "description": None, "valid": None}])

    def test_descriptions_from_config_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace(
                    'base_branches: ["origin/release"]',
                    'description: "示例业务系统"\n      base_branches:\n        origin/release: "九机业务线"',
                ).format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )
            config = dispatcher.load_config(config_path)

            self.assertEqual(config.projects["mapped"].description, "示例业务系统")
            self.assertEqual(config.branch_map_for("mapped"), {"origin/release": "九机业务线"})
            self.assertEqual(config.branches_for("mapped"), ("origin/release",))

            repos_result = dispatcher.execute(dispatcher.build_parser().parse_args([
                "--config", str(config_path), "repos",
            ]))
            self.assertEqual(repos_result["repositories"][0]["description"], "示例业务系统")

            project_result = dispatcher.execute(dispatcher.build_parser().parse_args([
                "--config", str(config_path), "project", "--name", "mapped",
            ]))
            self.assertEqual(project_result["repository"]["description"], "示例业务系统")
            self.assertEqual(
                project_result["base_branches"],
                [{"name": "origin/release", "description": "九机业务线"}],
            )

    def test_branch_mapping_rejects_non_text_description(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace(
                    'base_branches: ["origin/release"]',
                    "base_branches:\n        origin/release: 42",
                ).format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "必须是非空字符串"):
                dispatcher.load_config(config_path)

    def test_snapshot_rejects_invalid_task_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            dispatcher.atomic_write_json(store.state_file, {"version": 1, "tasks": {"XSWL-1": "invalid"}})

            with self.assertRaisesRegex(dispatcher.DispatcherError, "任务记录格式不受支持"):
                store.snapshot()

    def test_resumable_shell_requires_recognizable_prompt(self) -> None:
        snapshot = dispatcher.TerminalSnapshot(
            handle="term-1", worktree_id="repo::repo", worktree_path=Path("repo"), tab_id="tab-1", leaf_id="leaf-1",
            title="Terminal", connected=True, writable=True, agent_identity=None, preview="error#",
        )

        self.assertFalse(dispatcher.is_resumable_shell(snapshot))

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".runtime" / "state.json"
            store = dispatcher.StateStore(path)
            item = assignment("XSWL-1", "repo", Path(temporary) / "repo")

            repository = dispatcher.Repository("repo", item.repository_path)
            plan = dispatcher.TerminalPlan(repository, (item,), "repo.XSWL-1", "separate")
            snapshot = dispatcher.TerminalSnapshot(
                handle="term-1",
                worktree_id=f"repo::{item.repository_path.resolve().as_posix()}",
                worktree_path=item.repository_path,
                tab_id="tab-1",
                leaf_id="leaf-1",
                title="repo.XSWL-1",
                connected=True,
                writable=True,
                agent_identity="claude",
                preview="",
            )
            record = dispatcher.TerminalRecord(item, "term-1", "repo.XSWL-1", "separate", 0, snapshot)

            store.mark_launching(item, plan, 0)
            self.assertEqual(store.status("XSWL-1"), "launching")
            self.assertTrue(store.reset("XSWL-1", force_unlock=False))
            self.assertIsNone(store.status("XSWL-1"))
            store.mark_launching(item, plan, 0)
            store.mark_dispatched(record)
            self.assertEqual(store.status("XSWL-1"), "dispatched")
            with self.assertRaisesRegex(dispatcher.DispatcherError, "仅允许复位 launching"):
                store.reset("XSWL-1", force_unlock=False)
            self.assertTrue(store.reset("XSWL-1", force_unlock=False, force=True))
            self.assertIsNone(store.status("XSWL-1"))

    def test_reset_parser_supports_force(self) -> None:
        arguments = dispatcher.build_parser().parse_args(["reset", "XSWL-1", "--force"])
        self.assertTrue(arguments.force)
        arguments = dispatcher.build_parser().parse_args(["reset", "XSWL-1"])
        self.assertFalse(arguments.force)

    def test_planner_keeps_repositories_separate_and_limits_panes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = dispatcher.Repository("first", root / "first")
            second = dispatcher.Repository("second", root / "second")
            assignments = tuple(
                [assignment(f"A-{index}", "first", first.path) for index in range(5)]
                + [assignment("B-1", "second", second.path)]
            )

            plans = dispatcher.build_terminal_plans(assignments, {"first": first, "second": second}, 4)

            self.assertEqual([[item.task.task_id for item in plan.assignments] for plan in plans], [
                ["A-0", "A-1", "A-2", "A-3"],
                ["A-4"],
                ["B-1"],
            ])

    def test_launcher_creates_all_panes_before_waiting_and_sending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            assignments = tuple(assignment(f"XSWL-{index}", "mapped", repository_path) for index in range(4))

            result = dispatcher.launch(config, assignments, store, fake_orca, force_unlock=False)

            self.assertEqual([item[0] for item in fake_orca.operations], [
                "status", "repo-list", "create", "show", "split-horizontal", "show", "split-vertical", "show",
                "split-vertical", "show", "wait", "show", "send", "wait", "show", "wait", "show", "send",
                "wait", "show", "wait", "show", "send", "wait", "show", "wait", "show", "send", "wait", "show",
                "send", "worktree-status", "send", "worktree-status",
                "send", "worktree-status", "send", "worktree-status",
            ])
            self.assertEqual([item["status"] for item in result["results"]], ["dispatched"] * 4)
            self.assertEqual([plan["task_ids"] for plan in result["plans"]], [[f"XSWL-{index}" for index in range(4)]])
            self.assertTrue(all(store.status(f"XSWL-{index}") == "dispatched" for index in range(4)))
            current_run = json.loads(store.current_run_file.read_text(encoding="utf-8"))
            self.assertEqual([item["task_id"] for item in current_run["tasks"]], [f"XSWL-{index}" for index in range(4)])
            self.assertNotIn("assignments", current_run)

    def test_launcher_auto_registers_missing_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca()
            item = assignment("XSWL-1", "mapped", repository_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            expected_path = os.path.normcase(os.path.normpath(str(repository_path.resolve())))
            self.assertEqual([item[0] for item in fake_orca.operations], [
                "status", "repo-list", "repo-add", "repo-list", "create", "show",
                "wait", "show", "send", "wait", "show", "send", "worktree-status",
            ])
            self.assertIn(("repo-add", expected_path), fake_orca.operations)
            self.assertEqual(result["results"][0]["status"], "dispatched")

    def test_launch_supports_selected_recursive_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            repository_path = projects / "nested" / "custom-service"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-custom"})
            item = assignment("XSWL-1", "custom-service", repository_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            self.assertIn("create", [operation for operation, _ in fake_orca.operations])

    def test_base_branch_only_appears_in_dev_spec_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = assignment("XSWL-1", "mapped", repository_path, "origin/release")

            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            create = next(value for operation, value in fake_orca.operations if operation == "create")
            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            task_command = next(value for value in sends if value.startswith("/dev-spec-gen"))
            self.assertNotIn("origin/release", create)
            self.assertIn("base_branch=origin/release", task_command)

    def test_reference_plan_sent_as_part_of_task_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试任务", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                reference_plan="参考方案内容",
            )

            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            task_command = next(value for value in sends if value.startswith("/dev-spec-gen"))
            self.assertIn("- 参考方案：“参考方案内容”", task_command)

    def test_reference_plan_omitted_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = assignment("XSWL-1", "mapped", repository_path)

            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            task_command = next(value for value in sends if value.startswith("/dev-spec-gen"))
            self.assertNotIn("参考方案", task_command)

    def test_assignment_rejects_blank_reference_plan(self) -> None:
        with self.assertRaisesRegex(dispatcher.DispatcherError, "reference_plan 必须是字符串或 null"):
            dispatcher.Assignment.from_dict({
                "task_id": "XSWL-1",
                "title": "测试任务",
                "task_url": "https://jira.example/XSWL-1",
                "repository": "mapped",
                "repository_path": "C:/repo",
                "base_branch": None,
                "reference_plan": "   ",
            })

    def test_task_rejects_non_text_description(self) -> None:
        with self.assertRaisesRegex(dispatcher.DispatcherError, "description 必须是字符串"):
            dispatcher.Task.from_dict({
                "task_id": "XSWL-1",
                "title": "测试任务",
                "description": {"text": "不是字符串"},
                "task_url": "https://jira.example/XSWL-1",
            })

    def test_task_context_includes_project_research_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            report_path = worktree_path / "docs" / "engineering" / "research" / "XSWL-1-gitnexus.md"
            report_path.parent.mkdir(parents=True)
            report_path.write_text("# 调研报告\n", encoding="utf-8")
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({worktree_path: "repo-worktree"})
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试任务", "https://jira.example/XSWL-1", "任务描述"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                worktree_path=worktree_path,
                assignee="测试负责人",
                gitnexus_report_path=report_path,
            )

            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            task_command = next(value for operation, value in fake_orca.operations if operation == "send" and value.startswith("/dev-spec-gen"))
            self.assertIn("- 任务描述：“任务描述”", task_command)
            self.assertIn("- 负责人：“测试负责人”", task_command)
            self.assertIn(f"- GitNexus 调研报告：{report_path.resolve().as_posix()}（复用该报告并跳过 GitNexus 调研节点）", task_command)
            state = dispatcher.read_json_object(config.state_file, {})["tasks"]["XSWL-1"]
            self.assertEqual(state["description"], "任务描述")
            self.assertEqual(state["assignee"], "测试负责人")
            self.assertEqual(state["gitnexus_report_path"], report_path.as_posix())

    def test_assignment_rejects_report_outside_task_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            outside_report = root / "report.md"
            outside_report.write_text("# 调研报告\n", encoding="utf-8")
            config = write_config(root, projects, layout_mode="separate")
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试任务", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                worktree_path=worktree_path,
                gitnexus_report_path=outside_report,
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "gitnexus_report_path 必须是任务 worktree research 目录中的现有文件"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_assignment_rejects_relative_paths(self) -> None:
        for field in ("repository_path", "worktree_path", "gitnexus_report_path"):
            with self.subTest(field=field), self.assertRaisesRegex(dispatcher.DispatcherError, f"{field} 必须是绝对路径"):
                dispatcher.Assignment.from_dict({
                    "task_id": "XSWL-1",
                    "title": "测试任务",
                    "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped",
                    "repository_path": "C:/repo" if field != "repository_path" else "repo",
                    "base_branch": None,
                    "worktree_path": "C:/worktree" if field != "worktree_path" else "worktree",
                    "gitnexus_report_path": "C:/worktree/docs/engineering/research/report.md" if field != "gitnexus_report_path" else "report.md",
                })

    def test_terminal_failures_require_manual_reset(self) -> None:
        for failure, task_count, affected_task_id in (
            ("create", 1, "XSWL-0"),
            ("wait", 1, "XSWL-0"),
            ("send", 1, "XSWL-0"),
            ("show", 1, "XSWL-0"),
            ("split", 2, "XSWL-1"),
        ):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                projects = root / "projects"
                repository_path = projects / "repo-a"
                (repository_path / ".git").mkdir(parents=True)
                config = write_config(root, projects)
                store = dispatcher.StateStore(config.state_file)
                fake_orca = FakeOrca({repository_path: "repo-mapped"}, failure=failure)
                items = tuple(assignment(f"XSWL-{index}", "mapped", repository_path) for index in range(task_count))

                result = dispatcher.launch(config, items, store, fake_orca, force_unlock=False)

                statuses = {item["task_id"]: item["status"] for item in result["results"]}
                self.assertEqual(statuses[affected_task_id], "requires_manual_reset")
                self.assertEqual(store.status(affected_task_id), "requires_manual_reset")

    def test_launch_retries_ready_wait_after_timeout(self) -> None:
        for wait_timeout_count, after_claude_count, expected_waits, expected_claude_sends in (
            (2, 0, 4, 1),  # 纯等待重试：超时耗尽后重新等待成功，不重发命令
            (2, 2, 6, 2),  # 第二段就绪超时：重发 agent 命令后重新等待成功
        ):
            with self.subTest(wait_timeout_count=wait_timeout_count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                projects = root / "projects"
                repository_path = projects / "repo-a"
                (repository_path / ".git").mkdir(parents=True)
                config = write_config(root, projects)
                store = dispatcher.StateStore(config.state_file)
                fake_orca = FakeOrca(
                    {repository_path: "repo-mapped"},
                    wait_timeout_count=wait_timeout_count,
                    wait_timeout_after_claude_count=after_claude_count,
                )
                item = assignment("XSWL-1", "mapped", repository_path)

                result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

                self.assertEqual(result["results"][0]["status"], "dispatched")
                waits = [value for operation, value in fake_orca.operations if operation == "wait"]
                self.assertEqual(len(waits), expected_waits)
                sends = [value for operation, value in fake_orca.operations if operation == "send"]
                self.assertEqual(sends.count("claude"), expected_claude_sends)

    def test_launch_retries_send_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, send_timeout_count=1)
            item = assignment("XSWL-1", "mapped", repository_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            self.assertEqual(sends[:2], ["claude", "claude"])

    def test_launch_retries_when_session_has_no_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, show_empty_count=1)
            item = assignment("XSWL-1", "mapped", repository_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            waits = [value for operation, value in fake_orca.operations if operation == "wait"]
            self.assertEqual(len(waits), 2)

    def test_launch_resends_agent_when_claude_session_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, show_empty_after_claude_count=1)
            item = assignment("XSWL-1", "mapped", repository_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            self.assertEqual(sends.count("claude"), 2)

    def test_launch_fails_when_session_stays_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            # 创建验证 1 次 + 每轮就绪检测(ready_retry_attempts+1=4 轮)均返回空会话
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, show_empty_count=5)
            item = assignment("XSWL-1", "mapped", repository_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")

    def test_ready_wait_timeout_increments_across_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            # 前 5 次 wait 失败：120s×2、240s×2 耗尽后 360s 轮第一次失败第二次成功
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, wait_timeout_count=5)
            item = assignment("XSWL-1", "mapped", repository_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            waits = [value for operation, value in fake_orca.operations if operation == "wait"]
            self.assertEqual(waits, [
                "term-1:120000", "term-1:120000",
                "term-1:240000", "term-1:240000",
                "term-1:360000", "term-1:360000",
                "term-1:120000",
            ])

    def test_separate_layout_creates_independent_task_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_paths = tuple(projects / f"repo-a-XSWL-{index}" for index in range(2))
            create_linked_worktree(repository_path, worktree_paths[0])
            create_linked_worktree(repository_path, worktree_paths[1])
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca()
            items = tuple(
                assignment(f"XSWL-{index}", "mapped", repository_path, worktree_path=worktree_paths[index])
                for index in range(2)
            )

            result = dispatcher.launch(config, items, store, fake_orca, force_unlock=False)

            creates = [value for operation, value in fake_orca.operations if operation == "create"]
            self.assertEqual(creates, [
                f"id:repo-{worktree_paths[0].name}::{worktree_paths[0].resolve().as_posix()}:{worktree_paths[0].name}:claude",
                f"id:repo-{worktree_paths[1].name}::{worktree_paths[1].resolve().as_posix()}:{worktree_paths[1].name}:claude",
            ])
            self.assertFalse(any(operation.startswith("split-") for operation, _ in fake_orca.operations))
            self.assertFalse(any(value == "claude" for operation, value in fake_orca.operations if operation == "send"))
            state = store.snapshot()["tasks"]
            self.assertEqual(state["XSWL-0"]["layout"], "separate")
            self.assertEqual(state["XSWL-0"]["tab_title"], worktree_paths[0].name)
            self.assertEqual(state["XSWL-0"]["repository_path"], worktree_paths[0].resolve().as_posix())
            self.assertEqual(state["XSWL-0"]["source_repository_path"], repository_path.resolve().as_posix())
            self.assertEqual(state["XSWL-0"]["worktree_path"], worktree_paths[0].resolve().as_posix())
            self.assertEqual([item["status"] for item in result["results"]], ["dispatched", "dispatched"])
            self.assertEqual(
                [value for operation, value in fake_orca.operations if operation == "worktree-status"],
                [worktree_paths[0].resolve().as_posix(), worktree_paths[1].resolve().as_posix()],
            )

    def test_separate_layout_retries_terminal_create_after_handle_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(create_timeout_count=1)
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            self.assertIn("terminal_create attempt=1/4", stderr.getvalue())
            self.assertIn("terminal_create handle_timeout", stderr.getvalue())
            self.assertIn("terminal_create no_existing", stderr.getvalue())
            self.assertIn("terminal_create attempt=2/4", stderr.getvalue())
            creates = [value for operation, value in fake_orca.operations if operation == "create"]
            self.assertEqual(len(creates), 2)
            self.assertEqual(creates[0], creates[1])
            self.assertTrue(creates[0].startswith(f"id:repo-{worktree_path.name}::"))
            self.assertTrue(creates[0].endswith(f":{worktree_path.name}:claude"))

    def test_terminal_create_reclaims_handle_after_side_effect_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(create_side_effect_timeout_count=1)
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "create"), 1)
            self.assertEqual(store.snapshot()["tasks"]["XSWL-1"]["terminal_handle"], "term-1")
            self.assertEqual(sum(value.startswith("/dev-spec-gen") for operation, value in fake_orca.operations if operation == "send"), 1)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(create_timeout_count=3)
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "create"), 4)

    def test_terminal_handle_timeout_accepts_nested_error_text(self) -> None:
        error = dispatcher.DispatcherError(
            "orca_command_failed",
            '{"error":{"message":"Terminal handle creation timeout"}}',
        )

        self.assertTrue(dispatcher.is_terminal_handle_timeout(error))

    def test_terminal_create_stops_after_three_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(create_timeout_count=4)
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "create"), 4)

    def test_separate_layout_does_not_retry_other_create_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(failure="create")
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertEqual(
                sum(1 for operation, _ in fake_orca.operations if operation == "create"),
                1,
            )

    def test_workspace_status_failure_keeps_dispatched_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, failure="workspace-status")

            result = dispatcher.launch(
                config,
                (assignment("XSWL-1", "mapped", repository_path),),
                store,
                fake_orca,
                force_unlock=False,
            )

            task_result = result["results"][0]
            self.assertEqual(task_result["status"], "dispatched")
            self.assertIn("workspace_status_error", task_result)
            self.assertEqual(store.status("XSWL-1"), "dispatched")
            self.assertEqual(
                [value for operation, value in fake_orca.operations if operation == "worktree-status"],
                [repository_path.resolve().as_posix()],
            )
            self.assertIn("workspace_status_failed", store.history_file.read_text(encoding="utf-8"))

    def test_read_assignments_accepts_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps({"tasks": [{
                "task_id": "XSWL-1",
                "title": "测试",
                "task_url": "https://jira.example/XSWL-1",
                "repository": "mapped",
                "repository_path": "D:/repo-a",
                "base_branch": None,
                "worktree_path": "D:/repo-a-task",
            }]}), encoding="utf-8")

            assignments = dispatcher.read_assignments(path)

            self.assertEqual(assignments[0].worktree_path, Path("D:/repo-a-task"))

    def test_split_layout_groups_project_tasks_in_source_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects, layout_mode="split")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            items = tuple(assignment(f"XSWL-{index}", "mapped", repository_path) for index in range(2))

            dispatcher.launch(config, items, store, fake_orca, force_unlock=False)

            create = next(value for operation, value in fake_orca.operations if operation == "create")
            self.assertEqual(
                create,
                f"path:{repository_path.resolve().as_posix()}:mapped.tab1:cmd.exe /d /k",
            )
            self.assertIn(("split-horizontal", "term-1:cmd.exe /d /k"), fake_orca.operations)
            self.assertFalse(any(operation == "rename" for operation, _ in fake_orca.operations))
            state = store.snapshot()["tasks"]
            self.assertEqual(state["XSWL-0"]["tab_title"], "mapped.tab1")
            self.assertEqual(state["XSWL-1"]["tab_title"], "mapped.tab1")
            self.assertEqual(state["XSWL-0"]["tab_id"], state["XSWL-1"]["tab_id"])

    def test_recover_reuses_restored_claude_pane_without_resending_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)
            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)
            fake_orca.restore_handle("term-1", "term-restored")
            send_count = sum(1 for operation, _ in fake_orca.operations if operation == "send")

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual(result["results"], [{
                "task_id": "XSWL-1",
                "status": "native_recovered",
                "terminal_handle": "term-restored",
            }])
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "send"), send_count)
            self.assertEqual(store.snapshot()["tasks"]["XSWL-1"]["terminal_handle"], "term-restored")
            self.assertEqual(
                [value for operation, value in fake_orca.operations if operation == "worktree-status"],
                [worktree_path.resolve().as_posix(), worktree_path.resolve().as_posix()],
            )

    def test_recover_marks_nonwritable_claude_pane_for_manual_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca()
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)
            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)
            snapshot = fake_orca.snapshots["term-1"]
            fake_orca.snapshots["term-1"] = dispatcher.TerminalSnapshot(
                **{**snapshot.__dict__, "writable": False}
            )

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual(result["results"], [{"task_id": "XSWL-1", "status": "requires_manual_reset"}])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)
            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)
            fake_orca.restore_handle("term-1", "term-restored", agent_identity=None, preview="PS D:\\repo>")

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "recovered")
            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            self.assertIn("claude --continue", sends)
            self.assertTrue(any("这是恢复会话" in value for value in sends))
            self.assertEqual(store.status("XSWL-1"), "dispatched")

    def test_recover_recreates_missing_separate_pane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects, layout_mode="separate")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = assignment("XSWL-1", "mapped", repository_path, worktree_path=worktree_path)
            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)
            fake_orca.snapshots.clear()

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "recovered")
            self.assertEqual(result["results"][0]["terminal_handle"], "term-2")
            creates = [value for operation, value in fake_orca.operations if operation == "create"]
            self.assertEqual(
                creates[-1],
                f"path:{worktree_path.resolve().as_posix()}:{worktree_path.name}:claude",
            )
            self.assertTrue(any("这是恢复会话" in value for operation, value in fake_orca.operations if operation == "send"))

    def test_recover_requires_tenant_slug_for_tenant_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects, layout_mode="split")
            store = dispatcher.StateStore(config.state_file)
            dispatcher.atomic_write_json(config.state_file, {
                "version": 1,
                "tasks": {
                    "XSWL-1::jiuji": {"repository": "mapped", "status": "launching", "layout": "separate"},
                },
            })
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            with self.assertRaisesRegex(dispatcher.DispatcherError, "--tenant-slug"):
                dispatcher.recover(config, store, fake_orca, task_id="XSWL-1", force_unlock=False)
            result = dispatcher.recover(config, store, fake_orca, task_id="XSWL-1", tenant_slug="jiuxunyun", force_unlock=False)

            self.assertEqual(result["results"], [])
            self.assertEqual(store.status("XSWL-1", "jiuji"), "launching")

    def test_recover_tenant_task_with_slug_recreates_pane_with_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1-jiuji"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG
                .replace('mode: "split"', 'mode: "separate"')
                .replace('base_branches: ["origin/release"]', '''tenants:
        九机:
          slug: "jiuji"
      base_branches: ["origin/release"]''')
                .format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )
            config = dispatcher.load_config(config_path)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                tenant="九机",
                tenant_slug="jiuji",
                worktree_path=worktree_path,
                requirement_snapshot_path=snapshot_path,
            )
            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)
            fake_orca.snapshots.clear()

            result = dispatcher.recover(config, store, fake_orca, task_id="XSWL-1", tenant_slug="jiuji", force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "recovered")
            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            self.assertTrue(any("这是恢复会话" in value for value in sends))
            self.assertTrue(any("完整原始需求快照" in value for value in sends))

    def test_recover_recreates_missing_first_split_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects, layout_mode="split")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            items = tuple(assignment(f"XSWL-{index}", "mapped", repository_path) for index in range(2))
            dispatcher.launch(config, items, store, fake_orca, force_unlock=False)
            fake_orca.snapshots.pop("term-2")

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual([item["status"] for item in result["results"]], ["native_recovered", "recovered"])
            self.assertIn(("split-horizontal", "term-1:cmd.exe /d /k"), fake_orca.operations)
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "create"), 1)

    def test_recover_recreates_missing_later_split_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects, layout_mode="split")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            items = tuple(assignment(f"XSWL-{index}", "mapped", repository_path) for index in range(3))
            dispatcher.launch(config, items, store, fake_orca, force_unlock=False)
            fake_orca.snapshots.pop("term-3")

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual([item["status"] for item in result["results"]], ["native_recovered", "native_recovered", "recovered"])
            self.assertIn(("split-vertical", "term-2:cmd.exe /d /k"), fake_orca.operations)
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "create"), 1)

    def test_recover_stops_when_required_split_parent_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects, layout_mode="split")
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            items = tuple(assignment(f"XSWL-{index}", "mapped", repository_path) for index in range(3))
            dispatcher.launch(config, items, store, fake_orca, force_unlock=False)
            fake_orca.snapshots.pop("term-2")
            fake_orca.snapshots.pop("term-3")
            split_count = sum(1 for operation, _ in fake_orca.operations if operation == "split-vertical")

            result = dispatcher.recover(config, store, fake_orca, task_id="XSWL-2", force_unlock=False)

            self.assertEqual(result["results"], [{"task_id": "XSWL-2", "status": "requires_manual_reset"}])
            self.assertEqual(store.status("XSWL-2"), "requires_manual_reset")
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "split-vertical"), split_count)

    def test_recover_marks_legacy_dispatched_state_for_manual_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            dispatcher.atomic_write_json(config.state_file, {
                "version": 1,
                "tasks": {"XSWL-1": {"repository": "mapped", "status": "dispatched"}},
            })
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual(result["results"], [{"task_id": "XSWL-1", "status": "requires_manual_reset"}])
            self.assertEqual(store.status("XSWL-1"), "requires_manual_reset")

    def test_orca_terminal_send_preserves_slash_command_from_msys_conversion(self) -> None:
        captured: dict[str, object] = {}
        original_run = dispatcher.subprocess.run

        def capture_run(*args: object, **kwargs: object) -> SimpleNamespace:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "result": {}}), stderr="")

        original_environment = dispatcher.os.environ.copy()
        dispatcher.os.environ["DISPATCHER_TEST_ENV"] = "keep"
        dispatcher.subprocess.run = capture_run
        try:
            text = "/dev-spec-gen https://jira.example/XSWL-1"
            dispatcher.OrcaClient().terminal_send("term-1", text)
        finally:
            dispatcher.subprocess.run = original_run
            dispatcher.os.environ.clear()
            dispatcher.os.environ.update(original_environment)

        command = captured["args"][0]
        environment = captured["kwargs"]["env"]
        assert isinstance(command, list)
        assert isinstance(environment, dict)
        self.assertEqual(command[command.index("--text") + 1], text)
        self.assertIn("--enter", command)
        self.assertNotIn("--interrupt", command)
        self.assertEqual(environment["MSYS_NO_PATHCONV"], "1")
        self.assertEqual(environment["MSYS2_ARG_CONV_EXCL"], "*")
        self.assertEqual(environment["DISPATCHER_TEST_ENV"], "keep")

    def test_orca_handle_accepts_create_and_split_envelopes(self) -> None:
        self.assertEqual(dispatcher.OrcaClient._handle({"terminal": {"handle": "term-create"}}), "term-create")
        self.assertEqual(dispatcher.OrcaClient._handle({"split": {"handle": "term-split"}}), "term-split")

    def test_terminal_wait_rejects_unsatisfied_nonzero_response(self) -> None:
        original_run = dispatcher.subprocess.run
        dispatcher.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"ok": True, "result": {"wait": {"satisfied": False}}}),
            stderr="",
        )
        try:
            with self.assertRaisesRegex(dispatcher.DispatcherError, "未在 1000ms 内就绪"):
                dispatcher.OrcaClient().terminal_wait("term-1", 1000)
        finally:
            dispatcher.subprocess.run = original_run

    def test_orca_nonzero_response_stops_non_wait_command(self) -> None:
        original_run = dispatcher.subprocess.run
        dispatcher.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"ok": True, "result": {}}),
            stderr="模拟失败",
        )
        try:
            with self.assertRaisesRegex(dispatcher.DispatcherError, "模拟失败"):
                dispatcher.OrcaClient().status()
        finally:
            dispatcher.subprocess.run = original_run

    def test_orca_os_error_is_normalized(self) -> None:
        original_run = dispatcher.subprocess.run

        def unavailable(*_args: object, **_kwargs: object) -> None:
            raise FileNotFoundError("orca")

        dispatcher.subprocess.run = unavailable
        try:
            with self.assertRaises(dispatcher.DispatcherError) as error:
                dispatcher.OrcaClient().status()
            self.assertEqual(error.exception.code, "orca_unavailable")
        finally:
            dispatcher.subprocess.run = original_run

    def test_process_is_running_treats_invalid_windows_pid_as_stale(self) -> None:
        original_kill = dispatcher.os.kill

        def invalid_pid(_: int, __: int) -> None:
            error = OSError("参数错误")
            error.winerror = 87
            raise error

        dispatcher.os.kill = invalid_pid
        try:
            self.assertFalse(dispatcher.process_is_running(999999999))
        finally:
            dispatcher.os.kill = original_kill

    def test_force_unlock_rejects_active_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            store.runtime_dir.mkdir(parents=True)
            store.lock_file.write_text(json.dumps({"pid": os.getpid(), "token": "active"}), encoding="utf-8")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "不能强制解锁"):
                with store.launch_lock(force_unlock=True):
                    pass

    def test_invalid_arguments_emit_single_json(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = dispatcher.main(["branches"])
        payload = json.loads(stdout.getvalue())
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["error"]["code"], "invalid_usage")


if __name__ == "__main__":
    unittest.main()
