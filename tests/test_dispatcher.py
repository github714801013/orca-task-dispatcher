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
from unittest import mock
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
    complete: "任务确认后直接创建或复用 worktree，并写入 worktree_path"
    recovery: "这是恢复会话"
  task_url_template: "https://jira.example/{{task_id}}"
  max_tasks: 12
interaction:
  repository_selection: "required"
  base_branch_selection: "optional"
  ask_batch_size: 4
dispatch:
  agent: "claude"
  agent_commands:
    windows_pwsh: "claude"
    windows_powershell: "claude"
    macos: "claude"
    linux: "claude"
  agent_extra_args: ""
  skill:
    command_templates:
      direct: "/dev-spec-gen {{task_url}} development_jira_spec(jira 参考方案驱动流程开发；当前实际开发任务：{{source_task_id}}；全自动执行，无需人员介入"
      complete: |
        /dev-spec-gen {{task_url}} 当前工作区当前分支 标准开发流程；base_branch={{base_branch}}；在当前任务的worktree中进行工作
        - 任务编号：{{task_id}}
        - 任务标题：“{{title}}”
        - 任务描述：“{{description}}”
        - 负责人：“{{assignee}}”
        - 租户：“{{tenant}}”
        - 分发标识：{{assignment_id}}
        - 参考方案：“{{reference_plan}}”
        - GitNexus 调研报告：{{gitnexus_report_path}}（复用该报告并跳过 GitNexus 调研节点）
        - 完整原始需求快照：{{requirement_snapshot_path}}（先读此快照，再按其相对路径读取正文与附件本体）
      proposal: "/dev-spec-gen 出具开发方案 {{task_url}} {{requirement_snapshot_path}}"
  terminal:
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


def write_config(root: Path, projects_root: Path) -> dispatcher.Config:
    config_path = root / "config" / "dispatcher.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        CONFIG.format(projects_root=projects_root.as_posix()),
        encoding="utf-8",
    )
    return dispatcher.load_config(config_path)


def worktree_assignment(
    projects: Path,
    repository_path: Path,
    task_id: str,
    *,
    branch: str | None = None,
    dispatch_flow: str = "complete",
    tenant: str = "legacy",
    tenant_slug: str = "legacy",
    worktree_suffix: str = "",
) -> dispatcher.Assignment:
    """创建任务工作树与需求快照，返回可直接分发的 assignment。"""
    worktree_path = projects / f"{repository_path.name}-{task_id}{worktree_suffix}"
    create_linked_worktree(repository_path, worktree_path)
    snapshot_path = create_requirement_snapshot(worktree_path, task_id)
    return dispatcher.Assignment(
        task=dispatcher.Task(task_id, task_id, f"https://jira.example/{task_id}"),
        repository="mapped",
        repository_path=repository_path,
        base_branch=branch,
        tenant=tenant,
        tenant_slug=tenant_slug,
        worktree_path=worktree_path,
        requirement_snapshot_path=snapshot_path,
        dispatch_flow=dispatch_flow,
    )


def flow_worktree_assignment(
    projects: Path,
    repository_path: Path,
    task_id: str,
    dispatch_flow: str,
) -> dispatcher.Assignment:
    """同一任务的不同流程各自使用独立 linked worktree 与需求快照。"""
    return worktree_assignment(
        projects,
        repository_path,
        task_id,
        dispatch_flow=dispatch_flow,
        worktree_suffix=f"-{dispatch_flow}",
    )


REGISTRY_CONFIG = """\
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
    complete: "任务确认后直接创建或复用 worktree，并写入 worktree_path"
    recovery: "这是恢复会话"
  task_url_template: "https://jira.example/{{task_id}}"
  max_tasks: 12
stages:
  - name: shared_dispatch
    command_template: "/dev-spec-gen {{task_url}} shared；base_branch={{base_branch}}"
    next_steps: ["准备 worktree", "启动开发会话"]
  - name: light_dispatch
    command_template: "/dev-spec-gen {{task_url}} light；base_branch={{base_branch}}"
    next_steps: ["轻量处理"]
    requires_worktree: false
    requires_snapshot: false
flows:
  - name: standard
    stages: [shared_dispatch]
    default: true
  - name: lightweight
    stages: [light_dispatch]
  - name: combo
    stages: [shared_dispatch, light_dispatch]
dispatch:
  agent: "claude"
  agent_commands:
    windows_pwsh: "claude"
    windows_powershell: "claude"
    macos: "claude"
    linux: "claude"
  agent_extra_args: ""
  terminal:
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


def write_registry_config(root: Path, projects_root: Path, text: str = REGISTRY_CONFIG) -> dispatcher.Config:
    config_path = root / "config" / "dispatcher.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(text.format(projects_root=projects_root.as_posix()), encoding="utf-8")
    return dispatcher.load_config(config_path)


def command_config(template: str, *, flow: str = "complete") -> dispatcher.Config:
    """构造只含一个流程的配置，用于命令渲染测试。"""
    stage = dispatcher.FlowStage(name="test_stage", command_template=template)
    flow_definition = dispatcher.DispatchFlow(
        name=flow,
        stage_names=("test_stage",),
        is_default=True,
        command_template=template,
        session_prompt=None,
        fetch_prompt=None,
        next_steps=(),
        requires_worktree=True,
        requires_snapshot=True,
    )
    return dispatcher.Config(
        root=Path("."), projects_root=Path("."), projects={}, branch_options=(), validate_branch=False,
        max_tasks=1, max_agents=1, ready_timeout_ms=1000, read_retry_attempts=1, read_retry_delay_ms=0, ready_retry_attempts=0, send_retry_attempts=0, agent_extra_args="", state_file=Path("state.json"),
        task_url_template="https://jira.example/{task_id}", task_source_type="prompt",
        task_source_query="", fetch_prompt="", agent_command="claude",
        stages={stage.name: stage},
        flows={flow: flow_definition},
        default_flow=flow,
    )


def assignment(
    task_id: str,
    repository: str,
    path: Path,
    branch: str | None = None,
    worktree_path: Path | None = None,
    dispatch_flow: str = "complete",
    requirement_snapshot_path: Path | None = None,
) -> dispatcher.Assignment:
    return dispatcher.Assignment(
        task=dispatcher.Task(task_id=task_id, title=task_id, task_url=f"https://jira.example/{task_id}"),
        repository=repository,
        repository_path=path,
        base_branch=branch,
        worktree_path=worktree_path,
        dispatch_flow=dispatch_flow,
        requirement_snapshot_path=requirement_snapshot_path,
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
        if snapshot is not None and snapshot.agent_identity == "claude" and self._wait_timeouts_after_claude_left > 0:
            self._wait_timeouts_after_claude_left -= 1
            raise dispatcher.DispatcherError("orca_not_ready", f"Claude terminal 未在 {timeout_ms}ms 内就绪：{handle}")
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
                "session_prompt": "任务确认后直接创建或复用 worktree，并写入 worktree_path",
                "task_url_template": "https://jira.example/{task_id}",
                "max_tasks": 12,
                "reference_plan_field": "customfield_11103",
                "query": "status = 待开发",
                "jql_source": "config",
                "flow": "complete",
                "dispatch_flow": "complete",
                "proposal_command": None,
                "jql_semantics": "native_jql_then_parent_post_filter",
                "parent_lookup": {"relation": "parent", "field": "customfield_11103", "required": True},
                "post_filter": "child.reference_plan 非空 OR parent.reference_plan 非空",
                "next_steps": ["执行独立 Jira 节点并归档完整需求与附件", "执行独立 GitNexus 调研节点", "创建或复用 worktree", "生成 version=1 decide 输入", "通过 decide 后 launch"],
            })

    def test_task_source_proposal_uses_independent_jira_and_research_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)

            result = dispatcher.task_source_prompt(config, flow="proposal")

            self.assertEqual(result["dispatch_flow"], "proposal")
            self.assertEqual(result["proposal_command"], "/dev-spec-gen 出具开发方案 {task_url} {requirement_snapshot_path}")
            self.assertEqual(result["jql_semantics"], "native_jql_then_parent_post_filter")
            self.assertEqual(result["parent_lookup"]["relation"], "parent")
            self.assertIn("parent.reference_plan", result["post_filter"])
            self.assertIn("独立 Jira 节点", " ".join(result["next_steps"]))
            self.assertIn("独立 GitNexus", " ".join(result["next_steps"]))
            self.assertIn("独立 GitNexus 调研节点", " ".join(result["next_steps"]))

    def test_proposal_command_only_contains_jira_url_and_snapshot_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            repository_path.mkdir(parents=True)
            config = write_config(root, projects)
            snapshot_path = worktree_path / "docs" / "engineering" / "specs" / "2026-09-09-test-raw-requirements.md"
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "不应下发的标题", "https://jira.example/XSWL-1", "不应下发的正文"),
                repository="repo-a",
                repository_path=repository_path,
                base_branch="main",
                worktree_path=worktree_path,
                requirement_snapshot_path=snapshot_path,
                dispatch_flow="proposal",
            )

            command = dispatcher.command_for(config, item)

            self.assertEqual(command, f"/dev-spec-gen 出具开发方案 https://jira.example/XSWL-1 {snapshot_path.resolve().as_posix()}")
            self.assertNotIn("不应下发", command)

    def test_assignment_rejects_malformed_dispatch_flow(self) -> None:
        with self.assertRaisesRegex(dispatcher.DispatcherError, "只能包含字母、数字、点、下划线和连字符"):
            dispatcher.Assignment.from_dict({
                "task_id": "XSWL-1",
                "title": "测试",
                "task_url": "https://jira.example/XSWL-1",
                "repository": "repo-a",
                "repository_path": "D:/repo-a",
                "base_branch": "main",
                "dispatch_flow": "unknown::flow",
            }, "complete")

    def test_unregistered_dispatch_flow_is_rejected_by_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "dispatch_flow 未注册"):
                config.flow_for("unknown")
            with self.assertRaisesRegex(dispatcher.DispatcherError, "flow 未注册"):
                dispatcher.task_source_prompt(config, flow="unknown")

    def test_direct_command_preserves_source_task_and_falls_back_to_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository = projects / "repo-a"
            (repository / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            for source, expected in (("CHILD-2", "CHILD-2"), (None, "PARENT-1")):
                with self.subTest(source=source):
                    item = dispatcher.Assignment(
                        task=dispatcher.Task("PARENT-1", "测试", "https://jira.example/PARENT-1"),
                        repository="mapped", repository_path=repository, base_branch=None,
                        source_task_id=source, dispatch_flow="direct",
                    )
                    command = dispatcher.command_for(config, item)
                    self.assertIn("https://jira.example/PARENT-1", command)
                    self.assertIn(f"当前实际开发任务：{expected}", command)
                    self.assertNotIn("当前实际开发任务", dispatcher.command_for(
                        config, assignment("PARENT-1", "mapped", repository),
                    ))

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

    def test_config_directory_environment_loads_dispatcher_yaml_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_dir = root / "customer-config"
            config_dir.mkdir()
            config_path = config_dir / "dispatcher.yaml"
            config_path.write_text(
                CONFIG.format(projects_root=projects.as_posix()), encoding="utf-8"
            )
            (config_dir / ".env").write_text("[", encoding="utf-8")

            with mock.patch.dict(os.environ, {"ORCA_DISPATCHER_CONFIG_DIR": str(config_dir)}, clear=False):
                arguments = dispatcher.build_parser().parse_args(["validate"])
                result = dispatcher.execute(arguments)

            self.assertEqual(Path(result["config"]["projects_root"]), projects.resolve())

    def test_environment_config_merges_with_managed_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / "managed.yaml"
            config_dir = root / "customer-config"
            projects = root / "projects"
            projects.mkdir()
            config_dir.mkdir()
            managed.write_text(CONFIG.replace('query: "status = 待开发"', 'query: "managed"').format(
                projects_root=projects.as_posix()
            ), encoding="utf-8")
            (config_dir / "dispatcher.yaml").write_text(
                "task_source:\n  query: customer\n", encoding="utf-8"
            )

            with mock.patch.dict(os.environ, {"ORCA_DISPATCHER_CONFIG_DIR": str(config_dir)}, clear=False), \
                 mock.patch.object(dispatcher, "config_default_path_from_script", return_value=managed):
                config = dispatcher.load_config(None)

            self.assertEqual(config.task_source_query, "customer")
            self.assertEqual(config.max_tasks, 12)

    def test_relative_config_directory_uses_current_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_dir = root / "customer-config"
            config_dir.mkdir()
            (config_dir / "dispatcher.yaml").write_text(
                CONFIG.format(projects_root=projects.as_posix()), encoding="utf-8"
            )
            previous_directory = Path.cwd()
            try:
                os.chdir(root)
                with mock.patch.dict(os.environ, {"ORCA_DISPATCHER_CONFIG_DIR": "customer-config"}, clear=False):
                    config = dispatcher.load_config(None)
            finally:
                os.chdir(previous_directory)

            self.assertEqual(config.projects_root, projects.resolve())

    def test_explicit_config_precedes_config_directory_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_projects = root / "env-projects"
            explicit_projects = root / "explicit-projects"
            (env_projects / "repo-a" / ".git").mkdir(parents=True)
            (explicit_projects / "repo-a" / ".git").mkdir(parents=True)
            env_config = root / "env-config" / "dispatcher.yaml"
            explicit_config = root / "explicit-config" / "dispatcher.yaml"
            for path, projects in ((env_config, env_projects), (explicit_config, explicit_projects)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(CONFIG.format(projects_root=projects.as_posix()), encoding="utf-8")

            with mock.patch.dict(os.environ, {"ORCA_DISPATCHER_CONFIG_DIR": str(env_config.parent)}, clear=False):
                arguments = dispatcher.build_parser().parse_args(["--config", str(explicit_config), "validate"])
                result = dispatcher.execute(arguments)

            self.assertEqual(Path(result["config"]["projects_root"]), explicit_projects.resolve())

    def test_empty_or_unset_environment_uses_default_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / "managed.yaml"
            user = root / "config" / "dispatcher.yaml"
            projects = root / "projects"
            projects.mkdir()
            for path, query in ((managed, "managed"), (user, "user")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    CONFIG.replace('query: "status = 待开发"', f'query: "{query}"').format(
                        projects_root=projects.as_posix()
                    ),
                    encoding="utf-8",
                )

            for value in (None, "", " \t "):
                with self.subTest(value=value), mock.patch.dict(os.environ, clear=False):
                    if value is None:
                        os.environ.pop("ORCA_DISPATCHER_CONFIG_DIR", None)
                    else:
                        os.environ["ORCA_DISPATCHER_CONFIG_DIR"] = value
                    with mock.patch.object(dispatcher, "config_default_path_from_script", return_value=managed), \
                         mock.patch.object(dispatcher, "config_path_from_script", return_value=user):
                        config = dispatcher.load_config(None)
                self.assertEqual(config.task_source_query, "user")

    def test_missing_environment_directory_uses_managed_default_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "skill"
            managed = skill_root / "config" / "dispatcher.default.yaml"
            user = skill_root / "config" / "dispatcher.yaml"
            projects = root / "projects"
            projects.mkdir()
            for path, query in ((managed, "managed"), (user, "user")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    CONFIG.replace('query: "status = 待开发"', f'query: "{query}"').format(
                        projects_root=projects.as_posix()
                    ),
                    encoding="utf-8",
                )
            missing_dir = root / "missing"
            empty_dir = root / "empty"
            empty_dir.mkdir()

            for environment_dir in (missing_dir, empty_dir):
                with self.subTest(environment_dir=environment_dir), \
                     mock.patch.dict(os.environ, {"ORCA_DISPATCHER_CONFIG_DIR": str(environment_dir)}, clear=False), \
                     mock.patch.object(dispatcher, "config_default_path_from_script", return_value=managed), \
                     mock.patch.object(dispatcher, "config_path_from_script", return_value=user):
                    config = dispatcher.load_config(None)

                self.assertEqual(config.task_source_query, "managed")
                self.assertEqual(config.root, skill_root.resolve())
                self.assertEqual(config.state_file, (skill_root / ".runtime" / "state.json").resolve())

    def test_environment_file_value_falls_back_to_managed_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "skill"
            managed = skill_root / "config" / "dispatcher.default.yaml"
            user = skill_root / "config" / "dispatcher.yaml"
            projects = root / "projects"
            projects.mkdir()
            for path, query in ((managed, "managed"), (user, "user")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    CONFIG.replace('query: "status = 待开发"', f'query: "{query}"').format(
                        projects_root=projects.as_posix()
                    ),
                    encoding="utf-8",
                )
            environment_value = root / "not-a-directory"
            environment_value.write_text("placeholder", encoding="utf-8")

            with mock.patch.dict(os.environ, {"ORCA_DISPATCHER_CONFIG_DIR": str(environment_value)}, clear=False), \
                 mock.patch.object(dispatcher, "config_default_path_from_script", return_value=managed), \
                 mock.patch.object(dispatcher, "config_path_from_script", return_value=user):
                config = dispatcher.load_config(None)

            self.assertEqual(config.task_source_query, "managed")
            self.assertEqual(config.root, skill_root.resolve())

    def test_invalid_environment_config_returns_invalid_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / "customer-config"
            config_dir.mkdir()
            secret = "D:/customer/private"
            (config_dir / "dispatcher.yaml").write_text(
                f'customer_secret: "{secret}', encoding="utf-8"
            )

            with mock.patch.dict(os.environ, {"ORCA_DISPATCHER_CONFIG_DIR": str(config_dir)}, clear=False):
                with self.assertRaises(dispatcher.DispatcherError) as raised:
                    dispatcher.load_config(None)

            self.assertEqual(raised.exception.code, "invalid_config")
            self.assertNotIn(str(config_dir), raised.exception.message)
            self.assertNotIn(secret, raised.exception.message)
            self.assertIn("YAML 格式错误", raised.exception.message)

    def test_invalid_environment_config_encoding_returns_invalid_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / "customer-config"
            config_dir.mkdir()
            (config_dir / "dispatcher.yaml").write_bytes(b"workspace: \xff")

            with mock.patch.dict(os.environ, {"ORCA_DISPATCHER_CONFIG_DIR": str(config_dir)}, clear=False):
                with self.assertRaises(dispatcher.DispatcherError) as raised:
                    dispatcher.load_config(None)

            self.assertEqual(raised.exception.code, "invalid_config")
            self.assertIn("配置文件编码错误", raised.exception.message)
            self.assertNotIn(str(config_dir), raised.exception.message)

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

        self.assertIn("用户提供的内容可能是伪 SQL 或伪 JQL", prompt)
        self.assertIn("交给 Jira 原生解析器校验", prompt)
        self.assertIn("通过校验后才执行查询", prompt)
        self.assertIn("解析、转换或原生校验失败时停止", prompt)
        self.assertIn("task_id（事项唯一标识）、title（标题）和 assignee", prompt)
        self.assertIn("父任务解析", prompt)
        self.assertIn("source_task_id、source_assignee", prompt)
        self.assertIn("不得用父任务负责人覆盖 source_assignee", prompt)
        self.assertIn("人员—项目/技术栈", prompt)
        self.assertIn("项目与分支定位必须以参考方案（customfield_11103）为依据", prompt)
        self.assertIn("就必须采用该结果，不得被 GitNexus 报告、任务标题/描述或其他证据推翻", prompt)
        self.assertIn("仅当参考方案缺失、冲突或无法映射到配置候选时，才回退到用父产品需求、任务标题/描述、GitNexus 报告和项目/分支描述联合决策", prompt)
        self.assertIn("回退后仍不能唯一确定项目、租户或基础分支时才请求用户确认", prompt)
        self.assertIn("只在参考方案缺失或无法映射时作为兜底证据", prompt)
        self.assertIn("因此按参考方案锁定 oanew/master-new", prompt)
        self.assertIn("一次跨项目只读远程 query", prompt)
        self.assertIn("不得先锁定仓库", prompt)
        self.assertIn("不得创建 worktree", prompt)
        self.assertIn("联合判断", prompt)
        self.assertIn("报告先暂存", prompt)

        self.assertIn("严禁修改", prompt)
        self.assertIn("参考方案字段：配置值为 customfield_11103", prompt)
        self.assertIn("按下方规则回退到其他证据联合决策", prompt)
        self.assertIn("branch_priority", prompt)
        self.assertNotIn("created", prompt)
        self.assertIn("当前会话完成", session_prompt)
        self.assertIn("不通过 Orca 编排创建或打开新的 Agent 会话", session_prompt)
        self.assertIn("仅最终 launch 阶段使用 Orca", session_prompt)
        self.assertIn("worktree_path", session_prompt)
        self.assertIn("GitNexus 调研报告必须先从 gitnexus_report_path", session_prompt)
        self.assertIn("工具边界：需要打开 Claude terminal、并发分发或恢复会话时使用 Orca", session_prompt)
        self.assertIn("只需要创建 worktree 或运行 dev-spec-gen CLI 时不使用 Orca", session_prompt)
        self.assertIn("先用 dev-spec-gen 创建/复用 worktree，再用 Orca 绑定该 worktree 启动 terminal", session_prompt)
        self.assertIn("不得在调用 worktree CLI 前询问、要求用户提供或自行猜测 worktree_path", session_prompt)
        self.assertIn("Dispatch capability is invalid", session_prompt)
        self.assertIn("不等于本地 dev-spec-gen worktree CLI 失败", session_prompt)
        self.assertIn("只有 dev-spec-gen 技能缺失、CLI 执行失败", session_prompt)
        self.assertIn("重试时创建新的有效 Orca dispatch", session_prompt)
        self.assertIn("orca worktree create", session_prompt)
        self.assertIn("status=success", session_prompt)
        self.assertNotIn("jira.9ji.com", prompt)

    def test_requirement_snapshot_is_required_for_direct_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            config = write_config(root, projects)
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                tenant="legacy",
                tenant_slug="legacy",
                worktree_path=worktree_path,
                dispatch_flow="direct",
            )
            repositories = {"mapped": dispatcher.Repository("mapped", repository_path)}

            with self.assertRaisesRegex(dispatcher.DispatcherError, "流程 direct 要求提供 requirement_snapshot_path"):
                dispatcher.validate_assignment(config, item, repositories)

            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
            dispatcher.validate_assignment(
                config,
                dispatcher.Assignment(
                    task=item.task,
                    repository=item.repository,
                    repository_path=repository_path,
                    base_branch=None,
                    tenant="legacy",
                    tenant_slug="legacy",
                    worktree_path=worktree_path,
                    requirement_snapshot_path=snapshot_path,
                    dispatch_flow="direct",
                ),
                repositories,
            )

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
            config = write_config(root, projects)
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
            config = write_config(root, projects)
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1", "描述正文由快照承载"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                worktree_path=worktree_path,
                requirement_snapshot_path=snapshot_path,
            )

            dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})
            command = dispatcher.command_for(config, item)
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
            config = write_config(root, projects)

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
            config = write_config(root, projects)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "附件清单未标记为完整"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_rejects_attachment_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path, worktree_path, _, item = make_snapshot_assignment(projects)
            attachment_path = worktree_path / "docs" / "engineering" / "attachments" / "XSWL-1" / "001-requirement.txt"
            attachment_path.write_text("被篡改的附件内容\n", encoding="utf-8")
            config = write_config(root, projects)

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
            config = write_config(root, projects)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "附件完整性校验失败"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_rejects_missing_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path, worktree_path, _, item = make_snapshot_assignment(projects)
            (worktree_path / "docs" / "engineering" / "attachments" / "XSWL-1" / "001-requirement.txt").unlink()
            config = write_config(root, projects)

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
            config = write_config(root, projects)

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
            config = write_config(root, projects)

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

            assignments = dispatcher.read_assignments(path, "complete")

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

            assignments = dispatcher.read_assignments(path, "complete")

            self.assertEqual([item.task.task_id for item in assignments], ["XSWL-1"])

    def test_decide_cli_preserves_direct_flow_and_source_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository = projects / "repo-a"
            worktree_path = projects / "repo-a-CW-7622"
            create_linked_worktree(repository, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "CW-7622")
            write_config(root, projects)
            arguments = dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"), "decide",
                "--task-id", "CW-7622", "--source-task-id", "CW-7624",
                "--title", "测试任务", "--task-url", "https://jira.example/CW-7622", "--repository", "mapped",
                "--base-branch", "origin/release", "--dispatch-flow", "direct",
                "--worktree-path", worktree_path.resolve().as_posix(),
                "--requirement-snapshot-path", snapshot_path.resolve().as_posix(),
            ])
            result = dispatcher.execute(arguments)
            selected = result["launch_input"]["tasks"][0]
            self.assertEqual(selected["dispatch_flow"], "direct")
            self.assertEqual(selected["source_task_id"], "CW-7624")
            self.assertEqual(selected["worktree_path"], worktree_path.resolve().as_posix())
            self.assertEqual(selected["requirement_snapshot_path"], snapshot_path.resolve().as_posix())
            self.assertEqual(result["tasks"], result["launch_input"]["tasks"])
            self.assertFalse((root / ".runtime" / "state.json").exists())

    def test_decide_cli_rejects_input_and_task_arguments_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "projects").mkdir()
            write_config(root, root / "projects")
            arguments = dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"), "decide",
                "--input", str(root / "decision.json"), "--dispatch-flow", "complete",
            ])
            with self.assertRaises(dispatcher.DispatcherError) as raised:
                dispatcher.execute(arguments)
            self.assertIn("不能与任务参数混用", raised.exception.message)

    def test_decide_normalizes_explicit_project_without_runtime_side_effect(self) -> None:

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
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
                "worktree_path": worktree_path.resolve().as_posix(),
                "requirement_snapshot_path": snapshot_path.resolve().as_posix(),
            }]}), encoding="utf-8")

            result = dispatcher.decide(config, path)

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["tasks"][0]["repository_path"], repository_path.resolve().as_posix())
            self.assertEqual(result["tasks"][0]["worktree_path"], worktree_path.resolve().as_posix())
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
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
            config = write_config(root, projects)
            path = root / "decision.json"
            path.write_text(json.dumps({"version": 1, "tasks": [
                {
                    "task_id": "XSWL-1",
                    "title": "已确认",
                    "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped",
                    "base_branch": "origin/release",
                    "worktree_path": worktree_path.as_posix(),
                    "requirement_snapshot_path": snapshot_path.as_posix(),
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
                dispatcher.read_decision_input(path, "complete")

    def test_read_assignments_rejects_legacy_assignments(self) -> None:
        for payload in (
            {"assignments": []},
            {"tasks": [], "assignments": []},
        ):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "input.json"
                path.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(dispatcher.DispatcherError, "仅支持 tasks"):
                    dispatcher.read_assignments(path, "complete")

    def test_read_assignments_rejects_non_list_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps({"tasks": {}}), encoding="utf-8")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "tasks 列表"):
                dispatcher.read_assignments(path, "complete")

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
                    dispatcher.read_assignments(path, "complete")

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

    def test_independent_terminal_launch_accepts_exact_claude_authorization_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, authorization_prompt_count=1)

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
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(
                {repository_path: "repo-mapped"},
                authorization_prompt_count=1,
                authorization_stays_visible=True,
            )

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
        config = command_config("/dev-spec-gen {task_url} base_branch={base_branch}")
        item = dispatcher.Assignment(
            task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
            repository="repo",
            repository_path=Path("repo"),
            base_branch="main & whoami",
        )

        with self.assertRaisesRegex(dispatcher.DispatcherError, "base_branch 包含不支持的命令字符"):
            dispatcher.command_for(config, item)

    def test_command_template_accepts_task_context_placeholders(self) -> None:
        dispatcher.validate_command_template("/dev-spec-gen {task_url} {title} {requirement_snapshot_path}")

    def test_command_template_rejects_unknown_placeholder(self) -> None:
        with self.assertRaisesRegex(dispatcher.DispatcherError, "不支持占位符：unknown"):
            dispatcher.validate_command_template("/dev-spec-gen {task_url} {unknown}")

    def test_command_template_must_start_with_dev_spec_gen(self) -> None:
        with self.assertRaisesRegex(dispatcher.DispatcherError, "必须以 /dev-spec-gen 开头"):
            dispatcher.validate_command_template("任务编号：{task_id}\n/dev-spec-gen {task_url}")

    def test_command_omits_source_and_parent_task_info(self) -> None:
        config = command_config("/dev-spec-gen {task_url}\n- 任务编号：{task_id}\n- 任务标题：“{title}”")
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

        command = dispatcher.command_for(config, item)

        self.assertIn("- 任务编号：XSWL-1", command)
        self.assertIn("产品需求标题", command)
        self.assertNotIn("XSWL-28372", command)
        self.assertNotIn("谢熊坤", command)
        self.assertNotIn("李飞", command)
        self.assertNotIn("来源", command)
        self.assertNotIn("父", command)

    def test_command_rejects_invalid_template(self) -> None:
        config = command_config("/dev-spec-gen {task_url")

        with self.assertRaisesRegex(dispatcher.DispatcherError, "模板格式不合法"):
            dispatcher.validate_command_template(config.flows["complete"].command_template)

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

    def test_legacy_layout_config_is_ignored_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.format(projects_root=projects.as_posix()).replace(
                    "  terminal:\n",
                    (
                        "  layout:\n"
                        "    group_by: repository\n"
                        "    max_panes_per_tab: 4\n"
                        "    mode: separate\n"
                        "  terminal:\n"
                        "    shell_commands:\n"
                        "      windows: \"cmd.exe /d /k\"\n"
                        "      posix: \"sh -i\"\n"
                    ),
                ),
                encoding="utf-8",
            )

            config = dispatcher.load_config(config_path)
            result = dispatcher.execute(dispatcher.build_parser().parse_args([
                "--config", str(config_path), "validate",
            ]))

            self.assertTrue(any("dispatch.layout" in warning for warning in config.deprecation_warnings))
            self.assertTrue(any("shell_commands" in warning for warning in config.deprecation_warnings))
            self.assertTrue(any("dispatch.layout" in warning for warning in result["config"]["deprecation_warnings"]))
            self.assertEqual(sorted(config.flows), ["complete", "direct", "proposal"])

    def test_legacy_split_section_in_session_prompt_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace(
                    '    recovery: "这是恢复会话"',
                    '    split: "旧会话说明"\n    recovery: "这是恢复会话"',
                ).format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )

            config = dispatcher.load_config(config_path)

            self.assertTrue(any("session_prompt.split" in warning for warning in config.deprecation_warnings))
            self.assertIn("worktree_path", config.session_prompt)

    def test_legacy_separate_template_is_reused_as_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            projects = temporary_root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config_path = temporary_root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace("      complete: |", "      separate: |").replace(
                    "    complete: \"任务确认后直接创建或复用 worktree，并写入 worktree_path\"",
                    "    separate: \"任务确认后直接创建或复用 worktree，并写入 worktree_path\"",
                ).format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )

            config = dispatcher.load_config(config_path)

            self.assertEqual(config.session_prompt, "任务确认后直接创建或复用 worktree，并写入 worktree_path")
            self.assertIn("当前工作区当前分支 标准开发流程", config.flows["complete"].command_template)
            self.assertTrue(any("separate" in warning for warning in config.deprecation_warnings))

    def test_registry_flow_is_usable_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_registry_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            prompt = dispatcher.task_source_prompt(config, flow="lightweight")
            self.assertEqual(prompt["flow"], "lightweight")
            self.assertEqual(prompt["next_steps"], ["轻量处理"])

            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch="origin/release",
                dispatch_flow="lightweight",
            )
            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            self.assertIn("/dev-spec-gen https://jira.example/XSWL-1 light；base_branch=origin/release", sends)
            self.assertEqual(
                store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1", "legacy", "lightweight")]["worktree_path"],
                repository_path.resolve().as_posix(),
            )

    def test_omitted_dispatch_flow_uses_registry_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_registry_config(root, projects)
            self.assertEqual(config.default_flow, "standard")
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")

            tasks_path = root / "tasks.json"
            tasks_path.write_text(json.dumps({"tasks": [{
                "task_id": "XSWL-1",
                "title": "测试",
                "task_url": "https://jira.example/XSWL-1",
                "repository": "mapped",
                "repository_path": repository_path.as_posix(),
                "base_branch": "origin/release",
                "worktree_path": worktree_path.as_posix(),
            }]}), encoding="utf-8")

            assignments = dispatcher.read_assignments(tasks_path, config.default_flow)

            self.assertEqual([item.dispatch_flow for item in assignments], ["standard"])

            decision_path = root / "decision.json"
            decision_path.write_text(json.dumps({"version": 1, "tasks": [{
                "task_id": "XSWL-1",
                "title": "测试",
                "task_url": "https://jira.example/XSWL-1",
                "repository": "mapped",
                "base_branch": "origin/release",
                "worktree_path": worktree_path.as_posix(),
                "requirement_snapshot_path": snapshot_path.as_posix(),
            }]}), encoding="utf-8")

            result = dispatcher.decide(config, decision_path)

            self.assertEqual(result["status"], "ready")
            self.assertEqual([item["dispatch_flow"] for item in result["tasks"]], ["standard"])

    def test_registry_node_can_be_reused_by_multiple_flows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_registry_config(root, projects)

            standard = config.flows["standard"]
            combined = config.flows["combo"]

            self.assertEqual(standard.stage_names, ("shared_dispatch",))
            self.assertEqual(combined.stage_names, ("shared_dispatch", "light_dispatch"))
            self.assertEqual(combined.next_steps, ("准备 worktree", "启动开发会话", "轻量处理"))
            self.assertEqual(combined.command_template, config.flows["lightweight"].command_template)
            self.assertFalse(combined.requires_worktree)

    def test_registry_default_flow_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "只能有一个 default"):
                write_registry_config(
                    root,
                    projects,
                    REGISTRY_CONFIG.replace("    stages: [light_dispatch]", "    stages: [light_dispatch]\n    default: true"),
                )

    def test_registry_requires_default_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "必须且只能有一个 default"):
                write_registry_config(
                    root,
                    projects,
                    REGISTRY_CONFIG.replace("    default: true\n", ""),
                )

    def test_registry_rejects_unknown_node_and_missing_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "引用了未定义节点：missing_dispatch"):
                write_registry_config(
                    root,
                    projects,
                    REGISTRY_CONFIG.replace("    stages: [shared_dispatch]\n    default: true", "    stages: [missing_dispatch]\n    default: true"),
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "都未声明 command_template"):
                write_registry_config(
                    root,
                    projects,
                    REGISTRY_CONFIG.replace(
                        '    command_template: "/dev-spec-gen {{task_url}} shared；base_branch={{base_branch}}"',
                        '    next_steps: ["无模板"]',
                    ).replace('    next_steps: ["准备 worktree", "启动开发会话"]\n', ""),
                )

    def test_registry_rejects_duplicate_and_malformed_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "重复流程名：combo"):
                write_registry_config(
                    root,
                    projects,
                    REGISTRY_CONFIG.replace("  - name: standard", "  - name: combo"),
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "只能包含字母、数字、点、下划线和连字符"):
                write_registry_config(
                    root,
                    projects,
                    REGISTRY_CONFIG.replace("  - name: lightweight", '  - name: "bad::flow"'),
                )

    def test_state_and_reset_accept_unregistered_flow_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            (repository_path / ".git").mkdir(parents=True)
            config = write_registry_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            retired_key = dispatcher.state_key_for("XSWL-1", "legacy", "retired")
            dispatcher.atomic_write_json(config.state_file, {
                "version": 1,
                "tasks": {
                    retired_key: {
                        "task_id": "XSWL-1",
                        "tenant_slug": "legacy",
                        "assignment_id": "XSWL-1",
                        "status": "launching",
                        "dispatch_flow": "retired",
                    },
                },
            })
            arguments = dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"),
                "state", "--dispatch-flow", "retired",
            ])
            result = dispatcher.execute(arguments)

            self.assertEqual(set(result["state"]["tasks"]), {retired_key})

            reset_result = dispatcher.execute(dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"),
                "reset", "XSWL-1", "--dispatch-flow", "retired",
            ]))
            self.assertTrue(reset_result["reset"])
            self.assertIsNone(store.status("XSWL-1", dispatch_flow="retired"))

            with self.assertRaisesRegex(dispatcher.DispatcherError, "未注册"):
                config.flow_for("retired")

    def test_validate_reports_registry_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            write_registry_config(root, projects)

            result = dispatcher.execute(dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"),
                "validate",
            ]))

            summary = result["config"]
            self.assertEqual(summary["default_flow"], "standard")
            self.assertEqual(
                [(flow["name"], flow["default"]) for flow in summary["flows"]],
                [("standard", True), ("lightweight", False), ("combo", False)],
            )
            self.assertEqual([stage["name"] for stage in summary["stages"]], ["shared_dispatch", "light_dispatch"])

    def test_snapshot_rejects_invalid_task_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            dispatcher.atomic_write_json(store.state_file, {"version": 1, "tasks": {"XSWL-1": "invalid"}})

            with self.assertRaisesRegex(dispatcher.DispatcherError, "任务记录格式不受支持"):
                store.snapshot()

    def test_state_store_separates_dispatch_flows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_file = Path(temporary) / ".runtime" / "state.json"
            repository_path = Path(temporary) / "repo"
            repository = dispatcher.Repository("repo", repository_path)
            store = dispatcher.StateStore(state_file)
            items = tuple(
                assignment(
                    "XSWL-1",
                    "repo",
                    repository_path,
                    worktree_path=repository_path.parent / f"repo-XSWL-1-{flow}",
                    dispatch_flow=flow,
                )
                for flow in ("direct", "complete", "proposal")
            )

            for item in items:
                store.mark_launching(item, dispatcher.TerminalPlan(repository, (item,), item.worktree_path.name))

            state = store.snapshot()
            self.assertEqual(set(state["tasks"]), {
                "XSWL-1::legacy::flow::direct",
                "XSWL-1::legacy::flow::complete",
                "XSWL-1::legacy::flow::proposal",
            })
            self.assertEqual(
                [store.status("XSWL-1", dispatch_flow=flow) for flow in ("direct", "complete", "proposal")],
                ["launching", "launching", "launching"],
            )
            self.assertEqual({item["assignment_id"] for item in state["tasks"].values()}, {"XSWL-1"})
            self.assertEqual(
                {item["tab_title"] for item in state["tasks"].values()},
                {"repo-XSWL-1-direct", "repo-XSWL-1-complete", "repo-XSWL-1-proposal"},
            )

    def test_state_key_parser_rejects_malformed_canonical_key(self) -> None:
        with self.assertRaisesRegex(dispatcher.DispatcherError, "状态键格式"):
            dispatcher.parse_state_key("XSWL-1::legacy::wrong::direct")
        with self.assertRaisesRegex(dispatcher.DispatcherError, "状态键格式"):
            dispatcher.parse_state_key("XSWL-1::legacy::flow::direct::extra")

    def test_state_store_keeps_tenant_and_flow_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            repository_path = Path(temporary) / "repo"
            repository = dispatcher.Repository("repo", repository_path)
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
                repository="repo",
                repository_path=repository_path,
                base_branch=None,
                tenant="九机",
                tenant_slug="jiuji",
                worktree_path=repository_path.parent / "repo-XSWL-1-jiuji",
                dispatch_flow="direct",
            )

            store.mark_launching(item, dispatcher.TerminalPlan(repository, (item,), item.worktree_path.name))

            key = dispatcher.state_key_for("XSWL-1", "jiuji", "direct")
            self.assertEqual(set(store.snapshot()["tasks"]), {key})
            self.assertEqual(store.status("XSWL-1", "jiuji", "direct"), "launching")
            self.assertEqual(store.snapshot()["tasks"][key]["assignment_id"], "XSWL-1::jiuji")
            self.assertEqual(store.snapshot()["tasks"][key]["tab_title"], "repo-XSWL-1-jiuji")

    def test_decide_accepts_same_task_in_different_flows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            items = tuple(
                flow_worktree_assignment(projects, repository_path, "XSWL-1", flow)
                for flow in ("direct", "complete", "proposal")
            )
            config = write_config(root, projects)
            path = root / "decision.json"
            path.write_text(json.dumps({"version": 1, "tasks": [
                {
                    "task_id": "XSWL-1",
                    "title": "测试",
                    "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped",
                    "base_branch": "origin/release",
                    "dispatch_flow": item.dispatch_flow,
                    "worktree_path": item.worktree_path.resolve().as_posix(),
                    "requirement_snapshot_path": item.requirement_snapshot_path.resolve().as_posix(),
                }
                for item in items
            ]}), encoding="utf-8")

            result = dispatcher.decide(config, path)

            self.assertEqual(result["status"], "ready")
            self.assertEqual(
                {item["dispatch_flow"] for item in result["tasks"]},
                {"direct", "complete", "proposal"},
            )
            self.assertEqual(
                {item["worktree_path"] for item in result["tasks"]},
                {item.worktree_path.resolve().as_posix() for item in items},
            )

    def test_launcher_accepts_same_task_in_different_flows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            items = tuple(
                flow_worktree_assignment(projects, repository_path, "XSWL-1", flow)
                for flow in ("direct", "complete", "proposal")
            )
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            result = dispatcher.launch(config, items, store, fake_orca, force_unlock=False)

            self.assertEqual([item["status"] for item in result["results"]], ["dispatched"] * 3)
            self.assertEqual(
                set(store.snapshot()["tasks"]),
                {
                    "XSWL-1::legacy::flow::direct",
                    "XSWL-1::legacy::flow::complete",
                    "XSWL-1::legacy::flow::proposal",
                },
            )
            current_run = json.loads(store.current_run_file.read_text(encoding="utf-8"))
            self.assertEqual(
                {item["dispatch_flow"] for item in current_run["tasks"]},
                {"direct", "complete", "proposal"},
            )

    def test_launcher_rejects_shared_worktree_across_tenants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace(
                    'base_branches: ["origin/release"]',
                    '''tenants:
        tenant-a:
          slug: "tenant-a"
        tenant-b:
          slug: "tenant-b"
      base_branches: ["origin/release"]''',
                ).format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )
            config = dispatcher.load_config(config_path)
            assignments = tuple(
                dispatcher.Assignment(
                    task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
                    repository="mapped",
                    repository_path=repository_path,
                    base_branch="origin/release",
                    tenant=tenant,
                    tenant_slug=tenant,
                    worktree_path=worktree_path,
                    requirement_snapshot_path=snapshot_path,
                )
                for tenant in ("tenant-a", "tenant-b")
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "不能共享 worktree_path"):
                dispatcher.launch(config, assignments, dispatcher.StateStore(config.state_file), FakeOrca(), False)

    def test_launcher_rejects_worktree_owned_by_other_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.replace(
                    'base_branches: ["origin/release"]',
                    '''tenants:
        tenant-a:
          slug: "tenant-a"
        tenant-b:
          slug: "tenant-b"
      base_branches: ["origin/release"]''',
                ).format(projects_root=projects.as_posix()),
                encoding="utf-8",
            )
            config = dispatcher.load_config(config_path)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            first = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                tenant="tenant-a",
                tenant_slug="tenant-a",
                worktree_path=worktree_path,
                requirement_snapshot_path=snapshot_path,
            )
            dispatcher.launch(config, (first,), store, fake_orca, force_unlock=False)
            second = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                tenant="tenant-b",
                tenant_slug="tenant-b",
                worktree_path=worktree_path,
                requirement_snapshot_path=snapshot_path,
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "不能共享 worktree_path"):
                dispatcher.launch(config, (second,), store, fake_orca, force_unlock=False)

    def test_reset_can_target_one_dispatch_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            repository_path = Path(temporary) / "repo"
            repository = dispatcher.Repository("repo", repository_path)
            items = tuple(
                assignment(
                    "XSWL-1",
                    "repo",
                    repository_path,
                    worktree_path=repository_path.parent / f"repo-XSWL-1-{flow}",
                    dispatch_flow=flow,
                )
                for flow in ("direct", "complete")
            )
            for item in items:
                store.mark_launching(item, dispatcher.TerminalPlan(repository, (item,), item.worktree_path.name))

            self.assertTrue(store.reset("XSWL-1", force_unlock=False, dispatch_flow="direct"))
            self.assertIsNone(store.status("XSWL-1", dispatch_flow="direct"))
            self.assertEqual(store.status("XSWL-1", dispatch_flow="complete"), "launching")

    def test_reset_without_flow_rejects_ambiguous_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            repository_path = Path(temporary) / "repo"
            repository = dispatcher.Repository("repo", repository_path)
            for flow in ("direct", "complete"):
                item = assignment(
                    "XSWL-1",
                    "repo",
                    repository_path,
                    worktree_path=repository_path.parent / f"repo-XSWL-1-{flow}",
                    dispatch_flow=flow,
                )
                store.mark_launching(item, dispatcher.TerminalPlan(repository, (item,), item.worktree_path.name))

            with self.assertRaisesRegex(dispatcher.DispatcherError, "多个流程"):
                store.reset("XSWL-1", force_unlock=False)

    def test_flow_filter_parser_supports_state_recover_and_reset(self) -> None:
        parser = dispatcher.build_parser()
        for command in ("state", "recover"):
            arguments = parser.parse_args([command, "--dispatch-flow", "direct"])
            self.assertEqual(arguments.dispatch_flow, "direct")
        arguments = parser.parse_args(["reset", "XSWL-1", "--dispatch-flow", "proposal"])
        self.assertEqual(arguments.dispatch_flow, "proposal")

    def test_state_view_filters_flow_and_legacy_state_defaults_to_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            dispatcher.atomic_write_json(store.state_file, {
                "version": 1,
                "tasks": {"XSWL-1": {"status": "launching"}},
            })

            complete = store.state_view("complete")
            direct = store.state_view("direct")

            self.assertEqual(complete["tasks"]["XSWL-1"]["dispatch_flow"], "complete")
            self.assertEqual(complete["tasks"]["XSWL-1"]["state_key"], "XSWL-1")
            self.assertEqual(direct["tasks"], {})
            self.assertEqual(store.snapshot()["tasks"], {"XSWL-1": {"status": "launching"}})

    def test_recover_targets_only_requested_dispatch_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            items = tuple(
                flow_worktree_assignment(projects, repository_path, "XSWL-1", flow)
                for flow in ("direct", "complete")
            )
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            dispatcher.launch(config, items, store, fake_orca, force_unlock=False)
            complete_key = dispatcher.state_key_for("XSWL-1", "legacy", "complete")
            complete_before = dict(store.snapshot()["tasks"][complete_key])
            fake_orca.snapshots.clear()

            result = dispatcher.recover(
                config,
                store,
                fake_orca,
                task_id="XSWL-1",
                dispatch_flow="direct",
                force_unlock=False,
            )

            self.assertEqual(len(result["results"]), 1)
            self.assertEqual(result["results"][0]["dispatch_flow"], "direct")
            self.assertEqual(store.status("XSWL-1", dispatch_flow="direct"), "dispatched")
            self.assertEqual(store.status("XSWL-1", dispatch_flow="complete"), "dispatched")
            self.assertEqual(store.snapshot()["tasks"][complete_key], complete_before)

    def test_recover_marks_legacy_split_state_for_manual_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            dispatcher.atomic_write_json(config.state_file, {
                "version": 1,
                "tasks": {"XSWL-1": {
                    "task_id": "XSWL-1",
                    "repository": "mapped",
                    "repository_path": repository_path.resolve().as_posix(),
                    "worktree_path": item.worktree_path.resolve().as_posix(),
                    "requirement_snapshot_path": item.requirement_snapshot_path.as_posix(),
                    "tab_title": item.worktree_path.name,
                    "task_url": "https://jira.example/XSWL-1",
                    "title": "XSWL-1",
                    "status": "launching",
                    "layout": "split",
                    "dispatch_flow": "complete",
                }},
            })
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            result = dispatcher.recover(config, store, fake_orca, task_id="XSWL-1", force_unlock=False)

            self.assertEqual(result["results"], [{
                "task_id": "XSWL-1",
                "tenant": "legacy",
                "tenant_slug": "legacy",
                "assignment_id": "XSWL-1",
                "dispatch_flow": "complete",
                "status": "requires_manual_reset",
            }])
            self.assertEqual(store.status("XSWL-1"), "requires_manual_reset")
            self.assertIn("recovery_metadata_missing", store.history_file.read_text(encoding="utf-8"))
            self.assertEqual(
                [operation for operation, _ in fake_orca.operations if operation in {"create", "list", "wait", "send"}],
                [],
            )

    def test_decide_rejects_same_task_and_flow_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            path = root / "decision.json"
            item = {
                "task_id": "XSWL-1",
                "title": "测试",
                "task_url": "https://jira.example/XSWL-1",
                "repository": "mapped",
                "base_branch": "origin/release",
                "dispatch_flow": "direct",
            }
            path.write_text(json.dumps({"version": 1, "tasks": [item, item]}), encoding="utf-8")

            with self.assertRaisesRegex(dispatcher.DispatcherError, "流程重复"):
                dispatcher.decide(config, path)

    def test_recover_requires_flow_when_task_has_multiple_flows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            items = tuple(
                flow_worktree_assignment(projects, repository_path, "XSWL-1", flow)
                for flow in ("direct", "complete")
            )
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            dispatcher.launch(config, items, store, fake_orca, force_unlock=False)
            fake_orca.snapshots.clear()

            with self.assertRaisesRegex(dispatcher.DispatcherError, "多个流程"):
                dispatcher.recover(config, store, FakeOrca(), task_id="XSWL-1", force_unlock=False)
            result = dispatcher.recover(
                config, store, fake_orca, task_id="XSWL-1", dispatch_flow="direct", force_unlock=False
            )

            self.assertEqual([item["dispatch_flow"] for item in result["results"]], ["direct"])
            self.assertEqual([item["status"] for item in result["results"]], ["recovered"])
            self.assertEqual(store.status("XSWL-1", dispatch_flow="direct"), "dispatched")
            self.assertEqual(store.status("XSWL-1", dispatch_flow="complete"), "dispatched")
            creates = [value for operation, value in fake_orca.operations if operation == "create"]
            self.assertEqual(len(creates), 3)
            self.assertEqual(
                creates[-1],
                f"path:{items[0].worktree_path.resolve().as_posix()}:{items[0].worktree_path.name}:claude",
            )

    def test_state_cli_filters_flow_and_preserves_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            repository = dispatcher.Repository("mapped", projects / "repo-a")
            for flow in ("direct", "complete"):
                item = assignment(
                    "XSWL-1",
                    "mapped",
                    repository.path,
                    worktree_path=projects / f"repo-a-XSWL-1-{flow}",
                    dispatch_flow=flow,
                )
                store.mark_launching(item, dispatcher.TerminalPlan(repository, (item,), item.worktree_path.name))
            before = store.state_file.read_text(encoding="utf-8")

            arguments = dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"),
                "state", "--dispatch-flow", "direct",
            ])
            result = dispatcher.execute(arguments)

            state = result["state"]
            self.assertEqual(set(state["tasks"]), {dispatcher.state_key_for("XSWL-1", "legacy", "direct")})
            self.assertEqual(state["tasks"][dispatcher.state_key_for("XSWL-1", "legacy", "direct")]["dispatch_flow"], "direct")
            self.assertEqual(store.state_file.read_text(encoding="utf-8"), before)

    def test_reset_cli_records_resolved_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            repository = dispatcher.Repository("mapped", projects / "repo-a")
            for flow in ("direct", "complete"):
                item = assignment(
                    "XSWL-1",
                    "mapped",
                    repository.path,
                    worktree_path=projects / f"repo-a-XSWL-1-{flow}",
                    dispatch_flow=flow,
                )
                store.mark_launching(item, dispatcher.TerminalPlan(repository, (item,), item.worktree_path.name))

            arguments = dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"),
                "reset", "XSWL-1", "--dispatch-flow", "direct",
            ])
            result = dispatcher.execute(arguments)

            self.assertTrue(result["reset"])
            self.assertEqual(result["dispatch_flow"], "direct")
            self.assertEqual(store.status("XSWL-1", dispatch_flow="complete"), "launching")
            history = store.history_file.read_text(encoding="utf-8")
            self.assertIn('"dispatch_flow": "direct"', history)

    def test_resumable_shell_requires_recognizable_prompt(self) -> None:
        snapshot = dispatcher.TerminalSnapshot(
            handle="term-1", worktree_id="repo::repo", worktree_path=Path("repo"), tab_id="tab-1", leaf_id="leaf-1",
            title="Terminal", connected=True, writable=True, agent_identity=None, preview="error#",
        )

        self.assertFalse(dispatcher.is_resumable_shell(snapshot))

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".runtime" / "state.json"
            store = dispatcher.StateStore(path)
            item = assignment(
                "XSWL-1",
                "repo",
                Path(temporary) / "repo",
                worktree_path=Path(temporary) / "repo-XSWL-1",
            )

            repository = dispatcher.Repository("repo", item.repository_path)
            plan = dispatcher.TerminalPlan(repository, (item,), item.worktree_path.name)
            snapshot = dispatcher.TerminalSnapshot(
                handle="term-1",
                worktree_id=f"repo::{item.worktree_path.resolve().as_posix()}",
                worktree_path=item.worktree_path,
                tab_id="tab-1",
                leaf_id="leaf-1",
                title=item.worktree_path.name,
                connected=True,
                writable=True,
                agent_identity="claude",
                preview="",
            )
            record = dispatcher.TerminalRecord(item, "term-1", item.worktree_path.name, snapshot)

            store.mark_launching(item, plan)
            self.assertEqual(store.status("XSWL-1"), "launching")
            self.assertTrue(store.reset("XSWL-1", force_unlock=False))
            self.assertIsNone(store.status("XSWL-1"))
            store.mark_launching(item, plan)
            store.mark_dispatched(record)
            self.assertEqual(store.status("XSWL-1"), "dispatched")
            with self.assertRaisesRegex(dispatcher.DispatcherError, "仅允许复位 launching"):
                store.reset("XSWL-1", force_unlock=False)
            self.assertTrue(store.reset("XSWL-1", force_unlock=False, force=True))
            self.assertIsNone(store.status("XSWL-1"))

    def test_old_state_without_flow_defaults_to_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            dispatcher.atomic_write_json(store.state_file, {
                "version": 1,
                "tasks": {"XSWL-1": {"status": "launching"}},
            })

            self.assertEqual(store.status("XSWL-1", dispatch_flow="complete"), "launching")
            self.assertIsNone(store.status("XSWL-1", dispatch_flow="direct"))

    def test_old_state_explicit_flow_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            dispatcher.atomic_write_json(store.state_file, {
                "version": 1,
                "tasks": {"XSWL-1": {"status": "launching", "dispatch_flow": "direct"}},
            })

            self.assertEqual(store.status("XSWL-1", dispatch_flow="direct"), "launching")
            self.assertIsNone(store.status("XSWL-1", dispatch_flow="complete"))

    def test_state_view_rejects_conflicting_legacy_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            canonical = dispatcher.state_key_for("XSWL-1", "legacy", "direct")
            dispatcher.atomic_write_json(store.state_file, {
                "version": 1,
                "tasks": {
                    canonical: {"task_id": "XSWL-1", "status": "launching", "dispatch_flow": "direct"},
                    "XSWL-1": {"status": "dispatched", "dispatch_flow": "direct"},
                },
            })

            with self.assertRaisesRegex(dispatcher.DispatcherError, "冲突"):
                store.state_view()

    def test_state_view_prefers_consistent_canonical_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            canonical = dispatcher.state_key_for("XSWL-1", "legacy", "direct")
            value = {
                "status": "launching",
                "repository": "repo",
                "dispatch_flow": "direct",
            }
            dispatcher.atomic_write_json(store.state_file, {
                "version": 1,
                "tasks": {
                    canonical: {**value, "task_id": "XSWL-1"},
                    "XSWL-1": dict(value),
                },
            })

            view = store.state_view()

            self.assertEqual(list(view["tasks"]), [canonical])

    def test_reset_removes_equivalent_legacy_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            canonical = dispatcher.state_key_for("XSWL-1", "legacy", "direct")
            value = {"status": "launching", "repository": "repo", "dispatch_flow": "direct"}
            dispatcher.atomic_write_json(store.state_file, {
                "version": 1,
                "tasks": {
                    canonical: {**value, "task_id": "XSWL-1"},
                    "XSWL-1": dict(value),
                },
            })

            self.assertTrue(store.reset("XSWL-1", force_unlock=False, dispatch_flow="direct"))
            self.assertEqual(store.state_view("direct")["tasks"], {})
            self.assertIsNone(store.status("XSWL-1", dispatch_flow="direct"))

    def test_reset_keeps_legacy_complete_state_when_resetting_direct_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            direct_key = dispatcher.state_key_for("XSWL-1", "legacy", "direct")
            dispatcher.atomic_write_json(store.state_file, {
                "version": 1,
                "tasks": {
                    direct_key: {
                        "task_id": "XSWL-1",
                        "status": "launching",
                        "dispatch_flow": "direct",
                    },
                    "XSWL-1": {"status": "launching"},
                },
            })

            self.assertTrue(store.reset("XSWL-1", force_unlock=False, dispatch_flow="direct"))
            self.assertIsNone(store.status("XSWL-1", dispatch_flow="direct"))
            self.assertEqual(store.status("XSWL-1", dispatch_flow="complete"), "launching")

    def test_canonical_state_key_rejects_mismatched_value_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            canonical = dispatcher.state_key_for("XSWL-1", "legacy", "direct")
            dispatcher.atomic_write_json(store.state_file, {
                "version": 1,
                "tasks": {
                    canonical: {
                        "task_id": "XSWL-1",
                        "status": "launching",
                        "dispatch_flow": "complete",
                    },
                },
            })

            with self.assertRaisesRegex(dispatcher.DispatcherError, "identity 与状态键不一致"):
                store.state_view()

    def test_state_mutation_updates_equivalent_legacy_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            canonical = dispatcher.state_key_for("XSWL-1", "legacy", "direct")
            value = {"status": "launching", "repository": "repo", "dispatch_flow": "direct"}
            dispatcher.atomic_write_json(store.state_file, {
                "version": 1,
                "tasks": {
                    canonical: {**value, "task_id": "XSWL-1"},
                    "XSWL-1": dict(value),
                },
            })

            store.mark_requires_manual_reset(canonical, "probe")

            self.assertEqual(store.status("XSWL-1", dispatch_flow="direct"), "requires_manual_reset")

    def test_state_recovery_updates_equivalent_legacy_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = dispatcher.StateStore(root / ".runtime" / "state.json")
            canonical = dispatcher.state_key_for("XSWL-1", "legacy", "direct")
            value = {"status": "launching", "repository": "repo", "dispatch_flow": "direct"}
            dispatcher.atomic_write_json(store.state_file, {
                "version": 1,
                "tasks": {
                    canonical: {**value, "task_id": "XSWL-1"},
                    "XSWL-1": dict(value),
                },
            })
            snapshot = dispatcher.TerminalSnapshot(
                handle="term-1",
                worktree_id="repo::repo",
                worktree_path=root / "repo",
                tab_id="tab-1",
                leaf_id="leaf-1",
                title="Terminal",
                connected=True,
                writable=True,
                agent_identity="claude",
                preview="claude tui",
            )

            store.mark_recovered(canonical, snapshot, "probe")

            state = store.snapshot()["tasks"]
            self.assertEqual(state[canonical]["status"], "dispatched")
            self.assertEqual(state["XSWL-1"]["status"], "dispatched")
            self.assertEqual(store.status("XSWL-1", dispatch_flow="direct"), "dispatched")

    def test_state_view_keeps_multiple_flows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            item = assignment(
                "XSWL-1",
                "repo",
                Path(temporary) / "repo",
                worktree_path=Path(temporary) / "repo-XSWL-1-direct",
                dispatch_flow="direct",
            )
            repository = dispatcher.Repository("repo", item.repository_path)
            store.mark_launching(item, dispatcher.TerminalPlan(repository, (item,), item.worktree_path.name))
            item = assignment(
                "XSWL-1",
                "repo",
                Path(temporary) / "repo",
                worktree_path=Path(temporary) / "repo-XSWL-1-complete",
                dispatch_flow="complete",
            )
            store.mark_launching(item, dispatcher.TerminalPlan(repository, (item,), item.worktree_path.name))

            direct = store.state_view("direct")
            all_flows = store.state_view()

            self.assertEqual(list(direct["tasks"]), [dispatcher.state_key_for("XSWL-1", "legacy", "direct")])
            self.assertEqual(len(all_flows["tasks"]), 2)
            self.assertEqual(len(store.snapshot()["tasks"]), 2)

    def test_reset_parser_supports_force(self) -> None:
        arguments = dispatcher.build_parser().parse_args(["reset", "XSWL-1", "--force"])
        self.assertTrue(arguments.force)
        arguments = dispatcher.build_parser().parse_args(["reset", "XSWL-1"])
        self.assertFalse(arguments.force)

    def test_planner_creates_one_independent_terminal_per_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = dispatcher.Repository("first", root / "first")
            second = dispatcher.Repository("second", root / "second")
            assignments = tuple(
                [assignment(f"A-{index}", "first", first.path, worktree_path=root / "worktrees" / f"A-{index}") for index in range(5)]
                + [assignment("B-1", "second", second.path, worktree_path=root / "worktrees" / "B-1")]
            )

            plans = dispatcher.build_terminal_plans(assignments, {"first": first, "second": second})

            self.assertEqual(
                [[item.task.task_id for item in plan.assignments] for plan in plans],
                [["A-0"], ["A-1"], ["A-2"], ["A-3"], ["A-4"], ["B-1"]],
            )
            self.assertEqual(
                [plan.tab_title for plan in plans],
                [f"A-{index}" for index in range(5)] + ["B-1"],
            )
            self.assertEqual([plan.repository.name for plan in plans], ["first"] * 5 + ["second"])

    def test_launcher_creates_all_terminals_before_waiting_and_sending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            assignments = tuple(
                worktree_assignment(projects, repository_path, f"XSWL-{index}") for index in range(4)
            )
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            result = dispatcher.launch(config, assignments, store, fake_orca, force_unlock=False)

            operations = [item[0] for item in fake_orca.operations]
            self.assertEqual(operations, [
                "status", "repo-list",
                "worktree-list", "repo-add", "worktree-resolve", "create", "show",
                "worktree-list", "repo-add", "worktree-resolve", "create", "show",
                "worktree-list", "repo-add", "worktree-resolve", "create", "show",
                "worktree-list", "repo-add", "worktree-resolve", "create", "show",
                "wait", "show", "wait", "show", "wait", "show", "wait", "show",
                "send", "worktree-status", "send", "worktree-status",
                "send", "worktree-status", "send", "worktree-status",
            ])
            self.assertEqual(
                [value for operation, value in fake_orca.operations if operation == "create"],
                [
                    f"id:repo-{item.worktree_path.name}::{item.worktree_path.resolve().as_posix()}"
                    f":{item.worktree_path.name}:claude"
                    for item in assignments
                ],
            )
            self.assertFalse(any(operation.startswith("split-") for operation in operations))
            self.assertEqual([item["status"] for item in result["results"]], ["dispatched"] * 4)
            self.assertEqual([plan["task_ids"] for plan in result["plans"]], [[f"XSWL-{index}"] for index in range(4)])
            self.assertEqual(
                [plan["tab_title"] for plan in result["plans"]],
                [item.worktree_path.name for item in assignments],
            )
            self.assertTrue(all(store.status(f"XSWL-{index}") == "dispatched" for index in range(4)))
            current_run = json.loads(store.current_run_file.read_text(encoding="utf-8"))
            self.assertEqual([item["task_id"] for item in current_run["tasks"]], [f"XSWL-{index}" for index in range(4)])
            self.assertNotIn("assignments", current_run)

    def test_launcher_auto_registers_missing_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca()

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            expected_path = os.path.normcase(os.path.normpath(str(repository_path.resolve())))
            self.assertEqual([item[0] for item in fake_orca.operations], [
                "status", "repo-list", "repo-add", "repo-list", "worktree-list", "repo-add", "worktree-resolve",
                "create", "show", "wait", "show", "send", "worktree-status",
            ])
            self.assertIn(("repo-add", expected_path), fake_orca.operations)
            self.assertEqual(result["results"][0]["status"], "dispatched")

    def test_launch_supports_selected_recursive_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            repository_path = projects / "nested" / "custom-service"
            worktree_path = projects / "custom-service-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-custom"})
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "XSWL-1", "https://jira.example/XSWL-1"),
                repository="custom-service",
                repository_path=repository_path,
                base_branch=None,
                worktree_path=worktree_path,
                requirement_snapshot_path=snapshot_path,
            )

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            self.assertIn("create", [operation for operation, _ in fake_orca.operations])

    def test_base_branch_only_appears_in_dev_spec_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "XSWL-1", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch="origin/release",
                worktree_path=worktree_path,
                requirement_snapshot_path=snapshot_path,
            )

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
            worktree_path = projects / "repo-a-XSWL-1"
            create_linked_worktree(repository_path, worktree_path)
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试任务", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                reference_plan="参考方案内容",
                worktree_path=worktree_path,
                requirement_snapshot_path=snapshot_path,
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
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

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
            }, "complete")

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
            snapshot_path = create_requirement_snapshot(worktree_path, "XSWL-1")
            report_path = worktree_path / "docs" / "engineering" / "research" / "XSWL-1-gitnexus.md"
            report_path.parent.mkdir(parents=True)
            report_path.write_text("# 调研报告\n", encoding="utf-8")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({worktree_path: "repo-worktree"})
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试任务", "https://jira.example/XSWL-1", "任务描述"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                worktree_path=worktree_path,
                requirement_snapshot_path=snapshot_path,
                assignee="测试负责人",
                gitnexus_report_path=report_path,
            )

            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            task_command = next(value for operation, value in fake_orca.operations if operation == "send" and value.startswith("/dev-spec-gen"))
            self.assertNotIn("任务描述", task_command)
            self.assertIn(f"- 完整原始需求快照：{snapshot_path.resolve().as_posix()}", task_command)
            self.assertIn("- 负责人：“测试负责人”", task_command)
            self.assertIn(f"- GitNexus 调研报告：{report_path.resolve().as_posix()}（复用该报告并跳过 GitNexus 调研节点）", task_command)
            state = dispatcher.read_json_object(config.state_file, {})["tasks"][dispatcher.state_key_for("XSWL-1")]
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
            config = write_config(root, projects)
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
                }, "complete")

    def test_terminal_failures_require_manual_reset(self) -> None:
        for failure, task_count, affected_task_id, orca_options in (
            ("create", 1, "XSWL-0", {}),
            ("wait", 1, "XSWL-0", {}),
            ("send", 1, "XSWL-0", {}),
            ("show", 1, "XSWL-0", {}),
            # 独立终端：首个任务的终端创建重试耗尽后失败，不影响后续任务
            (None, 2, "XSWL-0", {"create_timeout_count": 4}),
        ):
            with self.subTest(failure=failure, task_count=task_count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                projects = root / "projects"
                repository_path = projects / "repo-a"
                items = tuple(
                    worktree_assignment(projects, repository_path, f"XSWL-{index}") for index in range(task_count)
                )
                config = write_config(root, projects)
                store = dispatcher.StateStore(config.state_file)
                fake_orca = FakeOrca({repository_path: "repo-mapped"}, failure=failure, **orca_options)

                result = dispatcher.launch(config, items, store, fake_orca, force_unlock=False)

                statuses = {item["task_id"]: item["status"] for item in result["results"]}
                self.assertEqual(statuses[affected_task_id], "requires_manual_reset")
                self.assertEqual(store.status(affected_task_id), "requires_manual_reset")
                self.assertEqual(
                    {task_id: status for task_id, status in statuses.items() if task_id != affected_task_id},
                    {
                        f"XSWL-{index}": "dispatched"
                        for index in range(task_count)
                        if f"XSWL-{index}" != affected_task_id
                    },
                )

    def test_launch_retries_ready_wait_after_timeout(self) -> None:
        for wait_timeout_count, after_claude_count, expected_waits in (
            (2, 0, ["term-1:120000", "term-1:120000", "term-1:240000"]),
            (0, 4, ["term-1:120000", "term-1:120000", "term-1:240000", "term-1:240000", "term-1:360000"]),
        ):
            with self.subTest(
                wait_timeout_count=wait_timeout_count, after_claude_count=after_claude_count
            ), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                projects = root / "projects"
                repository_path = projects / "repo-a"
                item = worktree_assignment(projects, repository_path, "XSWL-1")
                config = write_config(root, projects)
                store = dispatcher.StateStore(config.state_file)
                fake_orca = FakeOrca(
                    {repository_path: "repo-mapped"},
                    wait_timeout_count=wait_timeout_count,
                    wait_timeout_after_claude_count=after_claude_count,
                )

                result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

                self.assertEqual(result["results"][0]["status"], "dispatched")
                self.assertEqual(
                    [value for operation, value in fake_orca.operations if operation == "wait"],
                    expected_waits,
                )
                self.assertFalse(
                    any(value == "claude" for operation, value in fake_orca.operations if operation == "send")
                )

    def test_launch_retries_send_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, send_timeout_count=1)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            self.assertEqual(len(sends), 2)
            self.assertEqual(sends[0], sends[1])
            self.assertTrue(sends[0].startswith("/dev-spec-gen"))

    def test_launch_retries_when_session_has_no_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            # 创建校验 1 次 + 第一轮就绪检测 1 次返回空会话，重新等待后检测到内容
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, show_empty_count=2)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            waits = [value for operation, value in fake_orca.operations if operation == "wait"]
            self.assertEqual(len(waits), 2)

    def test_launch_rewaits_without_resending_agent_when_claude_session_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            # 创建校验 1 次 + 第一轮就绪检测 1 次返回空会话：等待重试期间不重发任何 agent 命令
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, show_empty_after_claude_count=2)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            self.assertEqual(sends.count("claude"), 0)
            self.assertEqual(sum(value.startswith("/dev-spec-gen") for value in sends), 1)
            waits = [value for operation, value in fake_orca.operations if operation == "wait"]
            self.assertEqual(len(waits), 2)

    def test_launch_fails_when_session_stays_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            # 创建验证 1 次 + 每轮就绪检测(ready_retry_attempts+1=4 轮)均返回空会话
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, show_empty_count=5)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")

    def test_ready_wait_timeout_increments_across_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            # 前 5 次 wait 失败：120s×2、240s×2 耗尽后 360s 轮第一次失败第二次成功
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, wait_timeout_count=5)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            waits = [value for operation, value in fake_orca.operations if operation == "wait"]
            self.assertEqual(waits, [
                "term-1:120000", "term-1:120000",
                "term-1:240000", "term-1:240000",
                "term-1:360000", "term-1:360000",
            ])

    def test_independent_terminals_use_task_worktree_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            items = tuple(worktree_assignment(projects, repository_path, f"XSWL-{index}") for index in range(2))
            worktree_paths = tuple(item.worktree_path for item in items)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca()

            result = dispatcher.launch(config, items, store, fake_orca, force_unlock=False)

            creates = [value for operation, value in fake_orca.operations if operation == "create"]
            self.assertEqual(creates, [
                f"id:repo-{worktree_paths[0].name}::{worktree_paths[0].resolve().as_posix()}:{worktree_paths[0].name}:claude",
                f"id:repo-{worktree_paths[1].name}::{worktree_paths[1].resolve().as_posix()}:{worktree_paths[1].name}:claude",
            ])
            self.assertFalse(any(operation.startswith("split-") for operation, _ in fake_orca.operations))
            self.assertFalse(any(value == "claude" for operation, value in fake_orca.operations if operation == "send"))
            state = store.snapshot()["tasks"]
            first = state[dispatcher.state_key_for("XSWL-0")]
            self.assertNotIn("layout", first)
            self.assertEqual(first["tab_title"], worktree_paths[0].name)
            self.assertEqual(first["repository_path"], worktree_paths[0].resolve().as_posix())
            self.assertEqual(first["source_repository_path"], repository_path.resolve().as_posix())
            self.assertEqual(first["worktree_path"], worktree_paths[0].resolve().as_posix())
            self.assertEqual([item["status"] for item in result["results"]], ["dispatched", "dispatched"])
            self.assertEqual(
                [value for operation, value in fake_orca.operations if operation == "worktree-status"],
                [worktree_paths[0].resolve().as_posix(), worktree_paths[1].resolve().as_posix()],
            )

    def test_independent_terminal_create_retries_after_handle_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            worktree_path = item.worktree_path
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(create_timeout_count=1)
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
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(create_side_effect_timeout_count=1)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched")
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "create"), 1)
            self.assertEqual(store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1")]["terminal_handle"], "term-1")
            self.assertEqual(sum(value.startswith("/dev-spec-gen") for operation, value in fake_orca.operations if operation == "send"), 1)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(create_timeout_count=3)

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
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(create_timeout_count=4)

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "create"), 4)

    def test_independent_terminal_does_not_retry_other_create_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca(failure="create")

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
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, failure="workspace-status")

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            task_result = result["results"][0]
            self.assertEqual(task_result["status"], "dispatched")
            self.assertIn("workspace_status_error", task_result)
            self.assertEqual(store.status("XSWL-1"), "dispatched")
            self.assertEqual(
                [value for operation, value in fake_orca.operations if operation == "worktree-status"],
                [item.worktree_path.resolve().as_posix()],
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

            assignments = dispatcher.read_assignments(path, "complete")

            self.assertEqual(assignments[0].worktree_path, Path("D:/repo-a-task"))

    def test_independent_terminals_do_not_share_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            items = tuple(worktree_assignment(projects, repository_path, f"XSWL-{index}") for index in range(2))
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            dispatcher.launch(config, items, store, fake_orca, force_unlock=False)

            self.assertEqual(
                [value for operation, value in fake_orca.operations if operation == "create"],
                [
                    f"id:repo-{item.worktree_path.name}::{item.worktree_path.resolve().as_posix()}"
                    f":{item.worktree_path.name}:claude"
                    for item in items
                ],
            )
            self.assertFalse(any(operation.startswith("split-") for operation, _ in fake_orca.operations))
            self.assertFalse(any(operation == "rename" for operation, _ in fake_orca.operations))
            state = store.snapshot()["tasks"]
            first = state[dispatcher.state_key_for("XSWL-0")]
            second = state[dispatcher.state_key_for("XSWL-1")]
            self.assertEqual(first["tab_title"], items[0].worktree_path.name)
            self.assertEqual(second["tab_title"], items[1].worktree_path.name)
            self.assertNotEqual(first["tab_id"], second["tab_id"])
            self.assertEqual(first["worktree_path"], items[0].worktree_path.resolve().as_posix())
            self.assertEqual(second["worktree_path"], items[1].worktree_path.resolve().as_posix())

    def test_recover_reuses_restored_claude_terminal_without_resending_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            worktree_path = item.worktree_path
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)
            fake_orca.restore_handle("term-1", "term-restored")
            send_count = sum(1 for operation, _ in fake_orca.operations if operation == "send")

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual(result["results"], [{
                "task_id": "XSWL-1",
                "tenant": "legacy",
                "tenant_slug": "legacy",
                "assignment_id": "XSWL-1",
                "dispatch_flow": "complete",
                "status": "native_recovered",
                "terminal_handle": "term-restored",
            }])
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "send"), send_count)
            self.assertEqual(store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1")]["terminal_handle"], "term-restored")
            self.assertEqual(
                [value for operation, value in fake_orca.operations if operation == "worktree-status"],
                [worktree_path.resolve().as_posix(), worktree_path.resolve().as_posix()],
            )

    def test_recover_marks_nonwritable_claude_terminal_for_manual_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca()
            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)
            snapshot = fake_orca.snapshots["term-1"]
            fake_orca.snapshots["term-1"] = dispatcher.TerminalSnapshot(
                **{**snapshot.__dict__, "writable": False}
            )

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual(result["results"], [{
                "task_id": "XSWL-1",
                "tenant": "legacy",
                "tenant_slug": "legacy",
                "assignment_id": "XSWL-1",
                "dispatch_flow": "complete",
                "status": "requires_manual_reset",
            }])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)
            fake_orca.restore_handle("term-1", "term-restored", agent_identity=None, preview="PS D:\\repo>")

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "recovered")
            sends = [value for operation, value in fake_orca.operations if operation == "send"]
            self.assertIn("claude --continue", sends)
            self.assertTrue(any("这是恢复会话" in value for value in sends))
            self.assertEqual(store.status("XSWL-1"), "dispatched")

    def test_recover_recreates_missing_independent_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            worktree_path = item.worktree_path
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
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
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            dispatcher.atomic_write_json(config.state_file, {
                "version": 1,
                "tasks": {
                    "XSWL-1::jiuji": {"repository": "mapped", "status": "launching"},
                },
            })
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            with self.assertRaisesRegex(dispatcher.DispatcherError, "--tenant-slug"):
                dispatcher.recover(config, store, fake_orca, task_id="XSWL-1", force_unlock=False)
            result = dispatcher.recover(config, store, fake_orca, task_id="XSWL-1", tenant_slug="jiuxunyun", force_unlock=False)

            self.assertEqual(result["results"], [])
            self.assertEqual(store.status("XSWL-1", "jiuji"), "launching")

    def test_recover_tenant_task_with_slug_recreates_terminal_with_snapshot(self) -> None:
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

    def test_recover_recreates_missing_first_independent_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            items = tuple(worktree_assignment(projects, repository_path, f"XSWL-{index}") for index in range(2))
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            dispatcher.launch(config, items, store, fake_orca, force_unlock=False)
            fake_orca.snapshots.pop("term-1")

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual([item["status"] for item in result["results"]], ["recovered", "native_recovered"])
            creates = [value for operation, value in fake_orca.operations if operation == "create"]
            self.assertEqual(len(creates), 3)
            self.assertEqual(
                creates[-1],
                f"path:{items[0].worktree_path.resolve().as_posix()}:{items[0].worktree_path.name}:claude",
            )
            self.assertEqual(result["results"][0]["terminal_handle"], "term-3")
            self.assertEqual(store.status("XSWL-0"), "dispatched")
            self.assertEqual(store.status("XSWL-1"), "dispatched")

    def test_recover_recreates_missing_later_independent_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            items = tuple(worktree_assignment(projects, repository_path, f"XSWL-{index}") for index in range(3))
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            dispatcher.launch(config, items, store, fake_orca, force_unlock=False)
            fake_orca.snapshots.pop("term-3")

            result = dispatcher.recover(config, store, fake_orca, task_id=None, force_unlock=False)

            self.assertEqual(
                [item["status"] for item in result["results"]],
                ["native_recovered", "native_recovered", "recovered"],
            )
            creates = [value for operation, value in fake_orca.operations if operation == "create"]
            self.assertEqual(len(creates), 4)
            self.assertEqual(
                creates[-1],
                f"path:{items[2].worktree_path.resolve().as_posix()}:{items[2].worktree_path.name}:claude",
            )
            self.assertEqual(result["results"][2]["terminal_handle"], "term-4")

    def test_recover_marks_task_with_missing_worktree_for_manual_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            items = tuple(worktree_assignment(projects, repository_path, f"XSWL-{index}") for index in range(2))
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            dispatcher.launch(config, items, store, fake_orca, force_unlock=False)
            items[1].worktree_path.rename(items[1].worktree_path.with_name("repo-a-XSWL-1-moved"))
            create_count = sum(1 for operation, _ in fake_orca.operations if operation == "create")

            result = dispatcher.recover(config, store, fake_orca, task_id="XSWL-1", force_unlock=False)

            self.assertEqual(result["results"], [{
                "task_id": "XSWL-1",
                "tenant": "legacy",
                "tenant_slug": "legacy",
                "assignment_id": "XSWL-1",
                "dispatch_flow": "complete",
                "status": "requires_manual_reset",
            }])
            self.assertEqual(store.status("XSWL-1"), "requires_manual_reset")
            self.assertEqual(sum(1 for operation, _ in fake_orca.operations if operation == "create"), create_count)
            history = store.history_file.read_text(encoding="utf-8")
            self.assertIn('"task": "XSWL-1"', history)
            self.assertIn('"result": "requires_manual_reset"', history)

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

            self.assertEqual(result["results"], [{
                "task_id": "XSWL-1",
                "tenant": "legacy",
                "tenant_slug": "legacy",
                "assignment_id": "XSWL-1",
                "dispatch_flow": "complete",
                "status": "requires_manual_reset",
            }])
            self.assertEqual(store.status("XSWL-1"), "requires_manual_reset")

    def test_git_child_excludes_config_directory_environment(self) -> None:
        captured: dict[str, object] = {}
        original_run = dispatcher.subprocess.run

        def capture_run(*args: object, **kwargs: object) -> SimpleNamespace:
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {"ORCA_DISPATCHER_CONFIG_DIR": "D:/customer/private"}, clear=False):
            dispatcher.subprocess.run = capture_run
            try:
                self.assertTrue(dispatcher.branch_exists(dispatcher.Repository("repo", Path("D:/repo")), "main"))
            finally:
                dispatcher.subprocess.run = original_run

        environment = captured["kwargs"]["env"]
        assert isinstance(environment, dict)
        self.assertNotIn("ORCA_DISPATCHER_CONFIG_DIR", environment)

    def test_orca_terminal_create_requests_focus(self) -> None:
        captured: dict[str, object] = {}
        original_run = dispatcher.subprocess.run

        def capture_run(*args: object, **kwargs: object) -> SimpleNamespace:
            captured["args"] = args
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"ok": True, "result": {"handle": "term-1"}}),
                stderr="",
            )

        dispatcher.subprocess.run = capture_run
        try:
            handle = dispatcher.OrcaClient().terminal_create("id:repo::D:/repo-task", "repo-task", "claude")
        finally:
            dispatcher.subprocess.run = original_run

        self.assertEqual(handle, "term-1")
        command = captured["args"][0]
        assert isinstance(command, list)
        self.assertIn("--focus", command)
        self.assertEqual(command[command.index("--worktree") + 1], "id:repo::D:/repo-task")
        self.assertEqual(command[command.index("--title") + 1], "repo-task")
        self.assertEqual(command[command.index("--command") + 1], "claude")

    def test_claude_settings_gain_skip_dangerous_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.json"

            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": temporary}, clear=False):
                written = dispatcher.ensure_claude_skip_dangerous_prompt()
                again = dispatcher.ensure_claude_skip_dangerous_prompt()

            self.assertEqual(written, settings_path)
            self.assertIsNone(again)
            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8")),
                {"skipDangerousModePermissionPrompt": True},
            )

    def test_claude_settings_skip_prompt_keeps_other_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.json"
            settings_path.write_text(
                json.dumps({"statusLine": {"type": "command"}}),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": temporary}, clear=False):
                dispatcher.ensure_claude_skip_dangerous_prompt()

            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8")),
                {"statusLine": {"type": "command"}, "skipDangerousModePermissionPrompt": True},
            )

    def test_claude_settings_skip_prompt_leaves_broken_config_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.json"
            for broken in ("{ not json", '["array"]'):
                with self.subTest(broken=broken):
                    settings_path.write_text(broken, encoding="utf-8")

                    with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": temporary}, clear=False):
                        written = dispatcher.ensure_claude_skip_dangerous_prompt()

                    self.assertIsNone(written)
                    self.assertEqual(settings_path.read_text(encoding="utf-8"), broken)

    def test_orca_terminal_send_preserves_slash_command_from_msys_conversion(self) -> None:
        captured: dict[str, object] = {}
        original_run = dispatcher.subprocess.run

        def capture_run(*args: object, **kwargs: object) -> SimpleNamespace:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "result": {}}), stderr="")

        original_environment = dispatcher.os.environ.copy()
        dispatcher.os.environ["DISPATCHER_TEST_ENV"] = "keep"
        dispatcher.os.environ["ORCA_DISPATCHER_CONFIG_DIR"] = "D:/customer/private"
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
        self.assertNotIn("ORCA_DISPATCHER_CONFIG_DIR", environment)

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
