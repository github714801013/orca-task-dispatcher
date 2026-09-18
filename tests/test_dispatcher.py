from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable
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


def staged_engineering_root(repository_path: Path, task_id: str) -> Path:
    """任务制品暂存根：与工作区内的 docs/engineering 同构。"""
    return repository_path / ".runtime" / task_id / "docs" / "engineering"


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
    """暂存需求快照与附件，返回可直接分发的 assignment；工作区由 Orca 在 launch 时创建。"""
    snapshot_path = stage_snapshot(repository_path, task_id)
    return dispatcher.Assignment(
        task=dispatcher.Task(task_id, task_id, f"https://jira.example/{task_id}"),
        repository="mapped",
        repository_path=repository_path,
        base_branch=branch,
        tenant=tenant,
        tenant_slug=tenant_slug,
        worktree_slug=worktree_suffix.lstrip("-") or task_id.lower(),
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
        max_tasks=1, max_agents=1, read_retry_attempts=1, read_retry_delay_ms=0, state_file=Path("state.json"),
        task_url_template="https://jira.example/{task_id}", task_source_type="prompt",
        task_source_query="", fetch_prompt="",
        stages={stage.name: stage},
        flows={flow: flow_definition},
        default_flow=flow,
    )


def assignment(
    task_id: str,
    repository: str,
    path: Path,
    branch: str | None = None,
    worktree_slug: str | None = None,
    dispatch_flow: str = "complete",
    requirement_snapshot_path: Path | None = None,
) -> dispatcher.Assignment:
    return dispatcher.Assignment(
        task=dispatcher.Task(task_id=task_id, title=task_id, task_url=f"https://jira.example/{task_id}"),
        repository=repository,
        repository_path=path,
        base_branch=branch,
        worktree_slug=worktree_slug,
        dispatch_flow=dispatch_flow,
        requirement_snapshot_path=requirement_snapshot_path,
    )


def ensure_git_repository(repository_path: Path) -> None:
    """把测试用仓库初始化成可用的 Git 仓库（含一次提交）。"""
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
def stage_snapshot(repository_path: Path, task_id: str) -> Path:
    """初始化源仓库并把需求快照与附件暂存到 .runtime/<task_id>/docs/engineering 下。"""
    ensure_git_repository(repository_path)
    return create_requirement_snapshot(staged_engineering_root(repository_path, task_id), task_id)


def create_requirement_snapshot(docs_root: Path, task_id: str) -> Path:
    specs_root = docs_root / "specs"
    attachments_root = docs_root / "attachments" / task_id
    specs_root.mkdir(parents=True, exist_ok=True)
    attachments_root.mkdir(parents=True, exist_ok=True)
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
    """返回（源仓库路径、暂存 docs/engineering 根、快照路径、assignment）。"""
    repository_path = projects / "repo-a"
    ensure_git_repository(repository_path)
    docs_root = staged_engineering_root(repository_path, "XSWL-1")
    snapshot_path = create_requirement_snapshot(docs_root, "XSWL-1")
    item = dispatcher.Assignment(
        task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
        repository="mapped",
        repository_path=repository_path,
        base_branch=None,
        requirement_snapshot_path=snapshot_path,
    )
    return repository_path, docs_root, snapshot_path, item


class FakeOrca:
    """按新流水线实现的最小 Orca 替身：建工作区、开普通终端、单次投递。"""

    def __init__(
        self,
        repository_ids: dict[Path, str] | None = None,
        failure: str | None = None,
        worktree_create_failure: str | None = None,
        terminal_create_failure: str | None = None,
        terminal_send_failure: str | None = None,
        terminal_idle: bool = True,
        send_accepted: bool = True,
        send_stages: tuple[str, ...] = ("input_accepted", "turn_started"),
        send_request_id: str | None = "req-1",
        send_observation: str | None = "submitted",
        send_payload: "dict[str, object] | None" = None,
        create_suffix: str = "",
        before_terminal_send: Callable[[str], None] | None = None,
    ) -> None:
        self.operations: list[tuple[str, str]] = []
        self.repository_ids = {
            os.path.normcase(os.path.normpath(str(path.resolve()))): repository_id
            for path, repository_id in (repository_ids or {}).items()
        }
        self.repository_paths = {
            repository_id: path.resolve() for path, repository_id in (repository_ids or {}).items()
        }
        self.failure = failure
        self.worktree_create_failure = worktree_create_failure
        self.terminal_create_failure = terminal_create_failure
        self.terminal_send_failure = terminal_send_failure
        self.terminal_idle = terminal_idle
        self.send_accepted = send_accepted
        self.send_stages = send_stages
        self.send_request_id = send_request_id
        self.send_observation = send_observation
        self.send_payload = send_payload
        self.create_suffix = create_suffix
        self.before_terminal_send = before_terminal_send
        self.worktrees_by_id: dict[str, dispatcher.OrcaWorktree] = {}
        self.terminals_by_handle: dict[str, dispatcher.OrcaTerminal] = {}

    def status(self) -> None:
        self.operations.append(("status", ""))

    def repo_ids(self) -> dict[str, str]:
        self.operations.append(("repo-list", ""))
        return self.repository_ids

    def repo_add(self, repository: dispatcher.Repository) -> None:
        path = os.path.normcase(os.path.normpath(str(repository.path.resolve())))
        self.operations.append(("repo-add", path))
        self.repository_ids[path] = f"repo-{repository.name}"

    def repo_show_by_path(self, path: Path) -> str | None:
        key = os.path.normcase(os.path.normpath(str(path.resolve())))
        self.operations.append(("repo-show", key))
        return self.repository_ids.get(key)

    def worktree_set_in_progress(self, worktree_path: Path) -> None:
        self.operations.append(("worktree-status", worktree_path.resolve().as_posix()))
        if self.failure == "workspace-status":
            raise dispatcher.DispatcherError("orca_command_failed", "状态写入失败")

    def worktrees(self, repository_id: str) -> tuple[dispatcher.OrcaWorktree, ...]:
        self.operations.append(("worktree-list", repository_id))
        return tuple(
            worktree for worktree in self.worktrees_by_id.values()
            if worktree.repository_id == repository_id
        )

    def terminals(self, worktree: dispatcher.OrcaWorktree) -> tuple[dispatcher.OrcaTerminal, ...]:
        self.operations.append(("terminal-list", worktree.worktree_id))
        return tuple(
            terminal for terminal in self.terminals_by_handle.values()
            if terminal.worktree_id == worktree.worktree_id
        )

    def add_agent_terminal(self, worktree_id: str) -> str:
        """预置一个既有开发会话，供占用判定与清理保护使用。"""
        handle = f"term-{len(self.terminals_by_handle) + 1}"
        self.terminals_by_handle[handle] = dispatcher.OrcaTerminal(
            handle, worktree_id, "claude", True, True,
        )
        return handle

    def worktree_remove(self, worktree: dispatcher.OrcaWorktree) -> None:
        self.operations.append(("worktree-remove", worktree.worktree_id))
        repository = self.repository_paths[worktree.repository_id]
        subprocess.run(
            ("git", "-C", str(repository), "worktree", "remove", str(worktree.path)),
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.worktrees_by_id.pop(worktree.worktree_id, None)

    def worktree_create(
        self,
        name: str,
        repository_id: str,
        base_branch: str | None,
        comment: str,
    ) -> dispatcher.OrcaWorktree:
        repo_path = self.repository_paths.get(repository_id)
        if repo_path is None:
            repo_path = next(
                (Path(path) for path, identifier in self.repository_ids.items() if identifier == repository_id),
                None,
            )
        if repo_path is None:
            raise dispatcher.DispatcherError("orca_repository_not_registered", "未注册源仓库")
        target = repo_path.parent / f"{name}{self.create_suffix}"
        self.operations.append(("worktree-create", f"{name}:{target.as_posix()}:{comment}"))
        if self.worktree_create_failure:
            raise dispatcher.DispatcherError("orca_command_failed", self.worktree_create_failure)
        subprocess.run(
            ("git", "-C", str(repo_path), "worktree", "add", "-B", name, str(target)),
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        worktree = dispatcher.OrcaWorktree(
            f"{repository_id}::{target.resolve().as_posix()}",
            target.resolve(),
            repository_id=repository_id,
            base_branch=base_branch,
            comment=comment,
        )
        self.worktrees_by_id[worktree.worktree_id] = worktree
        return worktree

    def terminal_create(
        self,
        worktree: dispatcher.OrcaWorktree,
        title: str,
        command: str,
    ) -> str:
        self.operations.append(("terminal-create", worktree.worktree_id))
        self.operations.append(("terminal-title", title))
        self.operations.append(("terminal-command", command))
        if self.terminal_create_failure:
            raise dispatcher.DispatcherError("orca_command_failed", self.terminal_create_failure)
        handle = f"term-{len(self.terminals_by_handle) + 1}"
        self.terminals_by_handle[handle] = dispatcher.OrcaTerminal(
            handle, worktree.worktree_id, "claude", True, True,
        )
        return handle

    def terminal_show(self, handle: str) -> dispatcher.OrcaTerminal:
        self.operations.append(("terminal-show", handle))
        terminal = self.terminals_by_handle.get(handle)
        if terminal is None:
            raise dispatcher.DispatcherError("orca_command_failed", "未知终端句柄")
        return terminal

    def terminal_wait(self, handle: str, timeout_ms: int) -> "dict[str, object]":
        self.operations.append(("terminal-wait", handle))
        if self.terminal_idle:
            return {"satisfied": True, "status": "idle"}
        return {"satisfied": False, "status": "timeout"}

    def terminal_send(self, handle: str, text: str) -> "dict[str, object]":
        self.operations.append(("terminal-send", handle))
        self.operations.append(("terminal-spec", text))
        if self.before_terminal_send is not None:
            self.before_terminal_send(handle)
        if self.terminal_send_failure:
            raise dispatcher.DispatcherError("orca_command_failed", self.terminal_send_failure)
        if self.send_payload is not None:
            return self.send_payload
        prompt: dict[str, object] = {
            "requestId": self.send_request_id,
            "stages": list(self.send_stages),
            "provider": "claude",
        }
        if self.send_observation is not None:
            prompt["observation"] = self.send_observation
        return {"send": {"accepted": self.send_accepted, "prompt": prompt}}


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        # 文件同步由 dev-spec-gen 的子进程完成，单测里替身掉，只验证调用时机。
        self._sync_patch = mock.patch.object(dispatcher, "sync_worktree_files", return_value=())
        self._sync_patch.start()
        self.addCleanup(self._sync_patch.stop)

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
                "parent_lookup": {"relation": "parent", "field": "查询条件里父项条件所用的同一个字段", "required": True},
                "post_filter": "该字段在子任务或父任务上非空",
                "next_steps": ["执行独立 Jira 节点并归档完整需求与附件", "执行独立 GitNexus 调研节点", "暂存需求制品到源仓库 .runtime/<task-id>/docs/engineering/", "生成 version=1 decide 输入", "通过 decide 后 launch"],
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
            self.assertIn("父项条件所用的同一个字段", result["parent_lookup"]["field"])
            self.assertIn("独立 Jira 节点", " ".join(result["next_steps"]))
            self.assertIn("独立 GitNexus", " ".join(result["next_steps"]))
            self.assertIn("独立 GitNexus 调研节点", " ".join(result["next_steps"]))

    def test_proposal_command_only_contains_jira_url_and_snapshot_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
            repository_path.mkdir(parents=True)
            config = write_config(root, projects)
            snapshot_path = worktree_path / "specs" / "2026-09-09-test-raw-requirements.md"
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "不应下发的标题", "https://jira.example/XSWL-1", "不应下发的正文"),
                repository="repo-a",
                repository_path=repository_path,
                base_branch="main",
                requirement_snapshot_path=snapshot_path,
                dispatch_flow="proposal",
            )

            command = dispatcher.command_for(config, item)

            self.assertEqual(command, f"/dev-spec-gen 出具开发方案 https://jira.example/XSWL-1 {snapshot_path.resolve().as_posix()}")
            self.assertNotIn("不应下发", command)

    def test_default_proposal_includes_user_task_and_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository_path = root / "repo-a"
            repository_path.mkdir()
            config_path = root / "dispatcher.yaml"
            config_path.write_text(
                f'workspace:\n  projects_root: "{root.as_posix()}"\n', encoding="utf-8",
            )
            config = dispatcher.load_config(config_path)
            self.assertEqual(
                dispatcher.task_source_prompt(config, flow="proposal")["proposal_command"],
                "/dev-spec-gen 出具开发方案 {task_url} {requirement_snapshot_path}\n"
                "用户任务编号：{source_task_id}\n当前用户名：{source_assignee}",
            )
            snapshot_path = staged_engineering_root(repository_path, "TASK-1") / "specs" / "raw-requirements.md"
            cases = (
                ("CHILD-2", "当前用户", "TASK-1", "父需求负责人", "CHILD-2", "当前用户"),
                (None, None, None, "当前负责人", "TASK-1", "当前负责人"),
                ("TASK-1", None, None, "当前负责人", "TASK-1", "当前负责人"),
                ("CHILD-2", None, "TASK-1", "父需求负责人", "CHILD-2", None),
                (None, None, "TASK-1", "父需求负责人", "TASK-1", None),
                (None, None, None, None, "TASK-1", None),
                ("CHILD-2", " 当前\n用户 ", "TASK-1", "父需求负责人", "CHILD-2", "当前 用户"),
            )
            for source_id, source_name, parent_id, assignee, task_id, username in cases:
                with self.subTest(source_id=source_id, source_name=source_name, parent_id=parent_id):
                    item = dispatcher.Assignment(
                        task=dispatcher.Task("TASK-1", "不应下发的标题", "https://jira.example/TASK-1", "不应下发的正文"),
                        repository="repo-a", repository_path=repository_path, base_branch=None,
                        requirement_snapshot_path=snapshot_path, dispatch_flow="proposal",
                        source_task_id=source_id, source_assignee=source_name,
                        parent_task_id=parent_id, assignee=assignee,
                    )
                    expected = (
                        f"/dev-spec-gen 出具开发方案 https://jira.example/TASK-1 {snapshot_path.resolve().as_posix()}\n"
                        f"用户任务编号：{task_id}"
                    )
                    if username is not None:
                        expected += f"\n当前用户名：{username}"
                    self.assertEqual(dispatcher.command_for(config, item), expected)

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

        self.assertIn("查询条件（JQL）必须原样使用以下内容，严禁改写查询意图", prompt)
        self.assertIn("解析失败才判定为伪 SQL 或伪 JQL", prompt)
        self.assertIn("再把转换结果原样交给同一个 Jira 原生解析器校验", prompt)
        self.assertIn("通过校验后才执行查询", prompt)
        self.assertIn("解析、转换或再校验失败时停止", prompt)
        self.assertIn("原生 JQL 无法表达指向父项的字段条件", prompt)
        self.assertIn("禁止伪造「父需求的 cf」这类语法", prompt)
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
        self.assertIn("需求归档与调研报告一律先暂存", prompt)

        self.assertIn("严禁改写查询意图、增删条件或调整范围", prompt)
        self.assertIn("参考方案字段：配置值为 customfield_11103", prompt)
        self.assertIn("按下方规则回退到其他证据联合决策", prompt)
        self.assertIn("branch_priority", prompt)
        self.assertNotIn("created", prompt)
        self.assertIn("任务查询、Jira 归档、调研与路由在当前会话完成", session_prompt)
        self.assertIn("工作区创建、制品迁移、开发会话启动全部由 dispatcher launch 完成", session_prompt)
        self.assertIn("暂存到 `<源仓库>/.runtime/<实际-task-id>/docs/engineering/`", session_prompt)
        self.assertIn("GitNexus 调研报告必须先从 gitnexus_report_path", session_prompt)
        self.assertIn("本会话不调用 orca 命令，也不创建 Git worktree", session_prompt)
        self.assertIn("不得创建、迁移或预检任务工作区", session_prompt)
        self.assertNotIn("jira.9ji.com", prompt)

    def test_requirement_snapshot_requirement_follows_flow_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
            ensure_git_repository(repository_path)
            config = write_config(root, projects)
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                tenant="legacy",
                tenant_slug="legacy",
                dispatch_flow="direct",
            )
            repositories = {"mapped": dispatcher.Repository("mapped", repository_path)}

            def complete_assignment(**extra: object) -> dispatcher.Assignment:
                return dispatcher.Assignment(
                    task=item.task,
                    repository=item.repository,
                    repository_path=repository_path,
                    base_branch=None,
                    tenant="legacy",
                    tenant_slug="legacy",
                    dispatch_flow="complete",
                    **extra,
                )

            # direct_dispatch 节点声明 requires_snapshot: false，无需快照即可分发。
            dispatcher.validate_assignment(config, item, repositories)

            # 未声明该字段的流程维持默认要求。
            with self.assertRaisesRegex(dispatcher.DispatcherError, "流程 complete 要求提供 requirement_snapshot_path"):
                dispatcher.validate_assignment(config, complete_assignment(), repositories)

            snapshot_path = create_requirement_snapshot(staged_engineering_root(repository_path, "XSWL-1"), "XSWL-1")
            dispatcher.validate_assignment(
                config,
                complete_assignment(requirement_snapshot_path=snapshot_path),
                repositories,
            )

    def test_requirement_snapshot_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
            ensure_git_repository(repository_path)
            snapshot_root = worktree_path / "specs"
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
                requirement_snapshot_path=link,
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "现有普通文件"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_path_must_be_in_task_worktree_specs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            snapshot_path = stage_snapshot(repository_path, "XSWL-1")
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
            config = write_config(root, projects)
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试", "https://jira.example/XSWL-1", "描述正文由快照承载"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
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
            manifest_path = worktree_path / "attachments" / "XSWL-1" / "manifest.json"
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
            attachment_path = worktree_path / "attachments" / "XSWL-1" / "001-requirement.txt"
            attachment_path.write_text("被篡改的附件内容\n", encoding="utf-8")
            config = write_config(root, projects)

            with self.assertRaisesRegex(dispatcher.DispatcherError, "附件完整性校验失败"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_requirement_snapshot_rejects_attachment_size_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path, worktree_path, _, item = make_snapshot_assignment(projects)
            manifest_path = worktree_path / "attachments" / "XSWL-1" / "manifest.json"
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
            (worktree_path / "attachments" / "XSWL-1" / "001-requirement.txt").unlink()
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
            attachments_root = worktree_path / "attachments" / "XSWL-1"
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
            snapshot_path = stage_snapshot(repository_path, "XSWL-2")
            worktree_path = staged_engineering_root(repository_path, "XSWL-2")
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
                    "requirement_snapshot_path": snapshot_path.as_posix(),
                },
            ]}), encoding="utf-8")

            result = dispatcher.decide(config, path)

            self.assertEqual(result["status"], "needs_confirmation")
            self.assertIn("requirement_snapshot_path", result["tasks"][0]["reason"])
            self.assertIn("未标记为完整", result["tasks"][1]["reason"])

    def test_worktree_branch_name_uses_sanitised_username(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            projects = Path(temporary) / "projects"
            repository_path = projects / "repo-a"
            ensure_git_repository(repository_path)
            item = worktree_assignment(projects, repository_path, "XSWL-1", worktree_suffix="-fix")

            branch = dispatcher.worktree_branch_for(item, dispatcher.Repository("mapped", repository_path))

            # git user.name 为 "Dispatcher Test"：空格被过滤为短横线，分支名仍是合法 ref。
            self.assertEqual(branch, "dispatcher-test/XSWL-1-fix")

    def test_launch_renames_worktree_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            worktree_path = repository_path.parent / dispatcher.worktree_name_for(item)
            completed = subprocess.run(
                ("git", "-C", str(worktree_path), "branch", "--show-current"),
                capture_output=True, text=True, encoding="utf-8", check=False,
            )
            self.assertEqual(completed.stdout.strip(), "dispatcher-test/XSWL-1-xswl-1")

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
            ensure_git_repository(repository_path)
            docs_root = staged_engineering_root(repository_path, "XSWL-1")
            snapshot_jiuji = create_requirement_snapshot(docs_root, "XSWL-1")
            snapshot_jiuxun = create_requirement_snapshot(docs_root, "XSWL-1")
            path = root / "decision.json"
            path.write_text(json.dumps({"version": 1, "tasks": [
                {
                    "task_id": "XSWL-1", "title": "测试", "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped", "tenant": "九机", "tenant_slug": "jiuji",
                    "requirement_snapshot_path": snapshot_jiuji.as_posix(),
                },
                {
                    "task_id": "XSWL-1", "title": "测试", "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped", "tenant": "九讯云", "tenant_slug": "jiuxun",
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
            snapshot_path = stage_snapshot(repository, "CW-7622")
            write_config(root, projects)
            arguments = dispatcher.build_parser().parse_args([
                "--config", str(root / "config" / "dispatcher.yaml"), "decide",
                "--task-id", "CW-7622", "--source-task-id", "CW-7624",
                "--title", "测试任务", "--task-url", "https://jira.example/CW-7622", "--repository", "mapped",
                "--base-branch", "origin/release", "--dispatch-flow", "direct",
                "--worktree-slug", "fix-cw",
                "--requirement-snapshot-path", snapshot_path.resolve().as_posix(),
            ])
            result = dispatcher.execute(arguments)
            selected = result["launch_input"]["tasks"][0]
            self.assertEqual(selected["dispatch_flow"], "direct")
            self.assertEqual(selected["source_task_id"], "CW-7624")
            self.assertEqual(selected["worktree_slug"], "fix-cw")
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
            snapshot_path = stage_snapshot(repository_path, "XSWL-1")
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
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
                "requirement_snapshot_path": snapshot_path.resolve().as_posix(),
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
            snapshot_path = stage_snapshot(repository_path, "XSWL-1")
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
            config = write_config(root, projects)
            path = root / "decision.json"
            path.write_text(json.dumps({"version": 1, "tasks": [
                {
                    "task_id": "XSWL-1",
                    "title": "已确认",
                    "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped",
                    "base_branch": "origin/release",
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
            ensure_git_repository(repository_path)
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

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            sends = [value for operation, value in fake_orca.operations if operation == "terminal-spec"]
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
            snapshot_path = stage_snapshot(repository_path, "XSWL-1")
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")

            tasks_path = root / "tasks.json"
            tasks_path.write_text(json.dumps({"tasks": [{
                "task_id": "XSWL-1",
                "title": "测试",
                "task_url": "https://jira.example/XSWL-1",
                "repository": "mapped",
                "repository_path": repository_path.as_posix(),
                "base_branch": "origin/release",
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
                    worktree_slug=flow,
                    dispatch_flow=flow,
                )
                for flow in ("direct", "complete", "proposal")
            )

            for item in items:
                store.mark_launching(item)

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
                {item["worktree_name"] for item in state["tasks"].values()},
                {"XSWL-1-direct", "XSWL-1-complete", "XSWL-1-proposal"},
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
                worktree_slug="jiuji",
                dispatch_flow="direct",
            )

            store.mark_launching(item)

            key = dispatcher.state_key_for("XSWL-1", "jiuji", "direct")
            self.assertEqual(set(store.snapshot()["tasks"]), {key})
            self.assertEqual(store.status("XSWL-1", "jiuji", "direct"), "launching")
            self.assertEqual(store.snapshot()["tasks"][key]["assignment_id"], "XSWL-1::jiuji")
            self.assertEqual(store.snapshot()["tasks"][key]["worktree_name"], "XSWL-1-jiuji")

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
                    "worktree_slug": item.worktree_slug,
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
                {item["worktree_slug"] for item in result["tasks"]},
                {item.worktree_slug for item in items},
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
                    worktree_slug=flow,
                    dispatch_flow=flow,
                )
                for flow in ("direct", "complete")
            )
            for item in items:
                store.mark_launching(item)

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
                    worktree_slug=flow,
                    dispatch_flow=flow,
                )
                store.mark_launching(item)

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

    def test_recover_marks_state_without_terminal_handle_for_manual_reset(self) -> None:
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
                    "requirement_snapshot_path": item.requirement_snapshot_path.as_posix(),
                    "task_url": "https://jira.example/XSWL-1",
                    "title": "XSWL-1",
                    "status": "launching",
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
            self.assertIn("terminal_handle_missing", store.history_file.read_text(encoding="utf-8"))
            self.assertEqual(
                [operation for operation, _ in fake_orca.operations if operation in {"terminal-send", "terminal-spec"}],
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

            with self.assertRaisesRegex(dispatcher.DispatcherError, "多个流程"):
                dispatcher.recover(config, store, FakeOrca(), task_id="XSWL-1", force_unlock=False)
            result = dispatcher.recover(
                config, store, fake_orca, task_id="XSWL-1", dispatch_flow="direct", force_unlock=False
            )

            self.assertEqual([item["dispatch_flow"] for item in result["results"]], ["direct"])
            self.assertEqual([item["status"] for item in result["results"]], ["recovered"])
            self.assertEqual(store.status("XSWL-1", dispatch_flow="direct"), "dispatched")
            self.assertEqual(store.status("XSWL-1", dispatch_flow="complete"), "dispatched")
            creates = [value for operation, value in fake_orca.operations if operation == "worktree-create"]
            self.assertEqual(
                [value.split(":", 1)[0] for value in creates],
                [dispatcher.worktree_name_for(item) for item in items],
            )
            for value, item in zip(creates, items):
                self.assertIn(f"{dispatcher.worktree_name_for(item)}:", value)

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
                    worktree_slug=flow,
                    dispatch_flow=flow,
                )
                store.mark_launching(item)
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
                    worktree_slug=flow,
                    dispatch_flow=flow,
                )
                store.mark_launching(item)

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
            store.mark_recovered(canonical, "turn_started", "recovered", terminal_handle="term-1")

            state = store.snapshot()["tasks"]
            self.assertEqual(state[canonical]["status"], "dispatched")
            self.assertEqual(state["XSWL-1"]["status"], "dispatched")
            self.assertEqual(state[canonical]["terminal_handle"], "term-1")
            self.assertEqual(state["XSWL-1"]["terminal_handle"], "term-1")
            self.assertEqual(store.status("XSWL-1", dispatch_flow="direct"), "dispatched")

    def test_state_view_keeps_multiple_flows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = dispatcher.StateStore(Path(temporary) / ".runtime" / "state.json")
            item = assignment(
                "XSWL-1",
                "repo",
                Path(temporary) / "repo",
                dispatch_flow="direct",
            )
            repository = dispatcher.Repository("repo", item.repository_path)
            store.mark_launching(item)
            item = assignment(
                "XSWL-1",
                "repo",
                Path(temporary) / "repo",
                dispatch_flow="complete",
            )
            store.mark_launching(item)

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
                "status", "repo-list", "repo-show", "repo-add", "repo-show",
                "worktree-list", "worktree-create", "worktree-list", "worktree-list",
                "terminal-list", "terminal-create", "terminal-title", "terminal-command",
                "terminal-show", "terminal-wait", "terminal-send", "terminal-spec",
                "worktree-status",
            ])
            self.assertIn(("repo-add", expected_path), fake_orca.operations)
            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])

    def test_launch_supports_selected_recursive_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            repository_path = projects / "nested" / "custom-service"
            snapshot_path = stage_snapshot(repository_path, "XSWL-1")
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-custom"})
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "XSWL-1", "https://jira.example/XSWL-1"),
                repository="custom-service",
                repository_path=repository_path,
                base_branch=None,
                requirement_snapshot_path=snapshot_path,
            )

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            self.assertIn("worktree-create", [operation for operation, _ in fake_orca.operations])

    def test_base_branch_only_appears_in_dev_spec_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            snapshot_path = stage_snapshot(repository_path, "XSWL-1")
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "XSWL-1", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch="origin/release",
                requirement_snapshot_path=snapshot_path,
            )

            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            create = next(value for operation, value in fake_orca.operations if operation == "worktree-create")
            sends = [value for operation, value in fake_orca.operations if operation == "terminal-spec"]
            task_command = next(value for value in sends if value.startswith("/dev-spec-gen"))
            self.assertNotIn("origin/release", create)
            self.assertIn("base_branch=origin/release", task_command)

    def test_reference_plan_sent_as_part_of_task_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            snapshot_path = stage_snapshot(repository_path, "XSWL-1")
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试任务", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                reference_plan="参考方案内容",
                requirement_snapshot_path=snapshot_path,
            )

            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            sends = [value for operation, value in fake_orca.operations if operation == "terminal-spec"]
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

            sends = [value for operation, value in fake_orca.operations if operation == "terminal-spec"]
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
            snapshot_path = stage_snapshot(repository_path, "XSWL-1")
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
            report_path = worktree_path / "research" / "XSWL-1-gitnexus.md"
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
                requirement_snapshot_path=snapshot_path,
                assignee="测试负责人",
                gitnexus_report_path=report_path,
            )

            dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            task_command = next(value for operation, value in fake_orca.operations if operation == "terminal-spec" and value.startswith("/dev-spec-gen"))
            # 命令里的制品路径指向任务工作区（launch 已把暂存制品迁入）。
            worktree_docs = repository_path.parent / "XSWL-1" / "docs" / "engineering"
            self.assertNotIn("任务描述", task_command)
            self.assertIn(f"specs/{snapshot_path.name}", task_command)
            self.assertIn("- 负责人：“测试负责人”", task_command)
            self.assertIn(f"research/{report_path.name}（复用该报告并跳过 GitNexus 调研节点）", task_command)
            state = dispatcher.read_json_object(config.state_file, {})["tasks"][dispatcher.state_key_for("XSWL-1")]
            self.assertEqual(state["description"], "任务描述")
            self.assertEqual(state["assignee"], "测试负责人")
            self.assertTrue(state["gitnexus_report_path"].endswith(f"research/{report_path.name}"))

    def test_assignment_rejects_report_outside_task_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            worktree_path = staged_engineering_root(repository_path, "XSWL-1")
            ensure_git_repository(repository_path)
            outside_report = root / "report.md"
            outside_report.write_text("# 调研报告\n", encoding="utf-8")
            config = write_config(root, projects)
            item = dispatcher.Assignment(
                task=dispatcher.Task("XSWL-1", "测试任务", "https://jira.example/XSWL-1"),
                repository="mapped",
                repository_path=repository_path,
                base_branch=None,
                gitnexus_report_path=outside_report,
            )

            with self.assertRaisesRegex(dispatcher.DispatcherError, "gitnexus_report_path 必须位于 docs/engineering/research 下的现有文件"):
                dispatcher.validate_assignment(config, item, {"mapped": dispatcher.Repository("mapped", repository_path)})

    def test_assignment_rejects_relative_paths(self) -> None:
        for field in ("repository_path", "gitnexus_report_path"):
            with self.subTest(field=field), self.assertRaisesRegex(dispatcher.DispatcherError, f"{field} 必须是绝对路径"):
                dispatcher.Assignment.from_dict({
                    "task_id": "XSWL-1",
                    "title": "测试任务",
                    "task_url": "https://jira.example/XSWL-1",
                    "repository": "mapped",
                    "repository_path": "C:/repo" if field != "repository_path" else "repo",
                    "base_branch": None,
                    "gitnexus_report_path": "C:/worktree/docs/engineering/research/report.md" if field != "gitnexus_report_path" else "report.md",
                }, "complete")

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
                [(item.repository_path.parent / dispatcher.worktree_name_for(item)).resolve().as_posix()],
            )
            self.assertIn("workspace_status_failed", store.history_file.read_text(encoding="utf-8"))

    def test_read_assignments_accepts_worktree_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps({"tasks": [{
                "task_id": "XSWL-1",
                "title": "测试",
                "task_url": "https://jira.example/XSWL-1",
                "repository": "mapped",
                "repository_path": "D:/repo-a",
                "base_branch": None,
                "worktree_slug": "fix-cw",
            }]}), encoding="utf-8")

            assignments = dispatcher.read_assignments(path, "complete")

            self.assertEqual(assignments[0].worktree_slug, "fix-cw")

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

    def test_task_source_flow_query_overrides_base_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            flow_query = "issuetype = 开发需求 AND cf[11001] IS NOT EMPTY"
            config_path = root / "config" / "dispatcher.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                CONFIG.format(projects_root=projects.as_posix())
                + "stages:\n"
                "  - name: queried_stage\n"
                "    command_template: \"/dev-spec-gen {task_url} queried\"\n"
                "    query: \"" + flow_query + "\"\n"
                "flows:\n"
                "  - name: queried\n"
                "    stages: [queried_stage]\n"
                "    default: true\n",
                encoding="utf-8",
            )
            config = dispatcher.load_config(config_path)

            from_flow = dispatcher.task_source_prompt(config, flow="queried")
            from_cli = dispatcher.task_source_prompt(config, query_override="project = X", flow="queried")

            self.assertEqual(from_flow["query"], flow_query)
            self.assertEqual(from_flow["jql_source"], "flow")
            self.assertEqual(from_cli["query"], "project = X")
            self.assertEqual(from_cli["jql_source"], "cli")

    def test_agent_session_starts_after_artifacts_are_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)

            def assert_artifacts_ready(handle: str) -> None:
                self.assertEqual(
                    fake_orca.terminal_show(handle).worktree_id,
                    next(iter(fake_orca.worktrees_by_id)),
                )
                worktree = next(iter(fake_orca.worktrees_by_id.values()))
                docs = worktree.path / "docs" / "engineering"
                self.assertTrue((docs / "specs" / item.requirement_snapshot_path.name).is_file())
                self.assertTrue((docs / "attachments" / "XSWL-1" / "manifest.json").is_file())
                self.assertTrue((docs / "attachments" / "XSWL-1" / "001-requirement.txt").is_file())

            fake_orca = FakeOrca({repository_path: "repo-mapped"}, before_terminal_send=assert_artifacts_ready)
            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            create = next(value for operation, value in fake_orca.operations if operation == "worktree-create")
            self.assertIn(dispatcher.worktree_comment_for(item), create)
            self.assertIn(("terminal-command", "claude"), fake_orca.operations)
            self.assertIn(("terminal-title", dispatcher.worktree_name_for(item)), fake_orca.operations)

    def test_matching_worktree_is_reused_without_second_create(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            creation = fake_orca.worktree_create(
                dispatcher.worktree_name_for(item), "repo-mapped", item.base_branch,
                dispatcher.worktree_comment_for(item),
            )
            dispatcher.rename_worktree_branch(
                creation.path,
                dispatcher.worktree_branch_for(item, dispatcher.Repository("mapped", repository_path)),
            )
            fake_orca.operations.clear()

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            self.assertNotIn("worktree-create", [operation for operation, _ in fake_orca.operations])
            self.assertIn(("terminal-title", dispatcher.worktree_name_for(item)), fake_orca.operations)

    def test_reused_worktree_with_initial_branch_is_renamed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            repository = dispatcher.Repository("mapped", repository_path)
            creation = fake_orca.worktree_create(
                dispatcher.worktree_name_for(item), "repo-mapped", item.base_branch,
                dispatcher.worktree_comment_for(item),
            )
            fake_orca.operations.clear()

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            self.assertNotIn("worktree-create", [operation for operation, _ in fake_orca.operations])
            self.assertEqual(
                dispatcher.git_current_branch(creation.path),
                dispatcher.worktree_branch_for(item, repository),
            )

    def test_clean_suffix_worktree_is_removed_before_single_stable_create(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            suffix = fake_orca.worktree_create(
                f"{dispatcher.worktree_name_for(item)}-2", "repo-mapped", item.base_branch,
                dispatcher.worktree_comment_for(item),
            )
            fake_orca.operations.clear()

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            self.assertIn(("worktree-remove", suffix.worktree_id), fake_orca.operations)
            creates = [value for operation, value in fake_orca.operations if operation == "worktree-create"]
            self.assertEqual(len(creates), 1)
            self.assertIn(f"{dispatcher.worktree_name_for(item)}:", creates[0])

    def test_dirty_suffix_worktree_blocks_launch_without_second_create(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})
            suffix = fake_orca.worktree_create(
                f"{dispatcher.worktree_name_for(item)}-2", "repo-mapped", item.base_branch,
                dispatcher.worktree_comment_for(item),
            )
            (suffix.path / "uncommitted.txt").write_text("x", encoding="utf-8")
            fake_orca.operations.clear()

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertNotIn("worktree-create", [operation for operation, _ in fake_orca.operations])
            self.assertTrue(suffix.path.is_dir())

    def test_automatic_suffix_return_never_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"}, create_suffix="-2")

            result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertNotIn("terminal-send", [operation for operation, _ in fake_orca.operations])
            self.assertEqual(store.status("XSWL-1"), "requires_manual_reset")

    def test_duplicate_physical_worktree_name_is_rejected_before_orca_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            duplicate = dataclasses.replace(item, tenant="其他", tenant_slug="other")
            config = write_config(root, projects)
            config = dataclasses.replace(config, projects={
                **config.projects,
                "mapped": dataclasses.replace(config.projects["mapped"], tenants={"其他": "other"}),
            })
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            with self.assertRaisesRegex(dispatcher.DispatcherError, "相同 worktree_name"):
                dispatcher.launch(config, (item, duplicate), store, fake_orca, force_unlock=False)

            self.assertEqual(fake_orca.operations, [])

    def test_artifact_copy_failure_marks_manual_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake_orca = FakeOrca({repository_path: "repo-mapped"})

            with mock.patch.object(dispatcher.shutil, "copytree", side_effect=OSError("磁盘错误")):
                result = dispatcher.launch(config, (item,), store, fake_orca, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertEqual(store.status("XSWL-1"), "requires_manual_reset")
            self.assertNotIn("terminal-send", [operation for operation, _ in fake_orca.operations])

    def test_create_waits_for_delayed_registration_and_terminal_without_recreating(self) -> None:
        for failure in (None, "runtime_unavailable", "runtime_timeout", "orca_timeout"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repository_path = root / "projects" / "repo-a"
                item = worktree_assignment(root / "projects", repository_path, "XSWL-1")
                config = write_config(root, root / "projects")
                store = dispatcher.StateStore(config.state_file)
                fake = FakeOrca({repository_path: "repo-mapped"})
                create, listing = fake.worktree_create, fake.worktrees
                clock = [0.0]

                def delayed_create(*args: object, **kwargs: object) -> dispatcher.OrcaWorktree:
                    receipt = create(*args, **kwargs)
                    clock[0] = 30.0
                    if failure:
                        raise dispatcher.DispatcherError(
                            "orca_timeout" if failure == "orca_timeout" else "orca_command_failed",
                            "模拟创建回执丢失",
                            orca_code="runtime_timeout" if failure == "orca_timeout" else failure,
                        )
                    return receipt

                def delayed_listing(repo_id: str) -> tuple[dispatcher.OrcaWorktree, ...]:
                    if clock[0] < 35:
                        return ()
                    if clock[0] == 35:
                        raise dispatcher.DispatcherError(
                            "orca_command_failed", "模拟只读查询短暂断连", orca_code="runtime_unavailable",
                        )
                    return listing(repo_id)

                def assert_inputs_ready(handle: str) -> None:
                    worktree = next(iter(fake.worktrees_by_id.values()))
                    # 投递必须发生在制品迁入并校验之后，且必须等过只读查询的等待窗口。
                    self.assertEqual(fake.terminal_show(handle).worktree_id, worktree.worktree_id)
                    self.assertGreater(clock[0], 35)
                    docs = worktree.path / "docs" / "engineering"
                    settled = dataclasses.replace(item, requirement_snapshot_path=docs / "specs" / item.requirement_snapshot_path.name)
                    dispatcher.validate_requirement_snapshot_path(config, settled, docs)

                fake.before_terminal_send = assert_inputs_ready
                with mock.patch.object(fake, "worktree_create", side_effect=delayed_create) as created, \
                     mock.patch.object(fake, "worktrees", side_effect=delayed_listing), \
                     mock.patch.object(dispatcher.time, "monotonic", side_effect=lambda: clock[0]), \
                     mock.patch.object(dispatcher.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
                    result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

                self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
                created.assert_called_once()
                self.assertEqual(sum(op == "terminal-send" for op, _ in fake.operations), 1)
                self.assertEqual(len(fake.worktrees_by_id), 1)
                if failure:
                    self.assertTrue(result["results"][0].get("warnings"))

    def test_create_observation_timeout_never_recreates_or_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1")
            config = write_config(root, root / "projects")
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            clock = [0.0]
            error = dispatcher.DispatcherError("orca_timeout", "模拟创建超时", orca_code="runtime_timeout")
            with mock.patch.object(fake, "worktree_create", side_effect=error) as created, \
                 mock.patch.object(dispatcher, "ORCA_WORKTREE_TIMEOUT_SECONDS", 10), \
                 mock.patch.object(dispatcher.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(dispatcher.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
                result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertEqual(clock[0], 10)
            created.assert_called_once()
            self.assertNotIn("terminal-send", [op for op, _ in fake.operations])
            state = store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1")]
            self.assertEqual(state["recovery_history"][-1]["reason"], "worktree_creation_unconfirmed")

    def test_create_definitive_error_does_not_enter_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1")
            config = write_config(root, root / "projects")
            fake = FakeOrca({path: "repo-mapped"}, worktree_create_failure="没有创建权限")
            with mock.patch.object(dispatcher.time, "sleep") as sleep:
                result = dispatcher.launch(config, (item,), dispatcher.StateStore(config.state_file), fake, force_unlock=False)
            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            sleep.assert_not_called()
            self.assertEqual(sum(op == "worktree-create" for op, _ in fake.operations), 1)
            self.assertEqual(sum(op == "worktree-list" for op, _ in fake.operations), 1)

    def test_transient_read_failure_after_create_keeps_single_worktree_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1")
            config = write_config(root, root / "projects")
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            real_terminals, calls = fake.terminals, {"count": 0}

            def flaky_terminals(worktree: dispatcher.OrcaWorktree) -> tuple[dispatcher.OrcaTerminal, ...]:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise dispatcher.DispatcherError(
                        "orca_command_failed", "模拟创建后读取断连", orca_code="runtime_unavailable",
                    )
                return real_terminals(worktree)

            with mock.patch.object(fake, "terminals", side_effect=flaky_terminals):
                result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            self.assertGreaterEqual(calls["count"], 2)
            self.assertEqual(sum(op == "worktree-create" for op, _ in fake.operations), 1)
            self.assertEqual(len(fake.worktrees_by_id), 1)
            state = store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1")]
            self.assertEqual(state["worktree_id"], next(iter(fake.worktrees_by_id)))
            self.assertIsNotNone(state["worktree_path"])

    def test_suffix_worktree_during_observation_stops_without_recreate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1")
            config = write_config(root, root / "projects")
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            name = dispatcher.worktree_name_for(item)
            suffix = dispatcher.OrcaWorktree(
                f"repo-mapped::{(path.parent / (name + '-2')).as_posix()}",
                path.parent / f"{name}-2",
                repository_id="repo-mapped",
                base_branch=item.base_branch,
                comment=dispatcher.worktree_comment_for(item),
            )
            state = {"created": False}

            def transport_failure(*_args: object, **_kwargs: object) -> None:
                state["created"] = True
                raise dispatcher.DispatcherError(
                    "orca_command_failed", "模拟创建断连", orca_code="runtime_unavailable",
                )

            def listing(_repo_id: str) -> tuple[dispatcher.OrcaWorktree, ...]:
                return (suffix,) if state["created"] else ()

            fake.operations.clear()
            with mock.patch.object(fake, "worktree_create", side_effect=transport_failure) as created, \
                 mock.patch.object(fake, "worktrees", side_effect=listing):
                result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertIn("后缀", result["results"][0]["message"])
            created.assert_called_once()

    def test_worktree_state_recorded_before_confirmation_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1")
            config = write_config(root, root / "projects")
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            real_listing, calls = fake.worktrees, {"count": 0}

            def failing_listing(repo_id: str) -> tuple[dispatcher.OrcaWorktree, ...]:
                calls["count"] += 1
                if calls["count"] > 1:
                    raise dispatcher.DispatcherError("orca_invalid_json", "模拟创建后列表不可读")
                return real_listing(repo_id)

            with mock.patch.object(fake, "worktrees", side_effect=failing_listing):
                result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            state = store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1")]
            self.assertEqual(state["worktree_id"], next(iter(fake.worktrees_by_id)))
            self.assertIsNotNone(state["worktree_path"])
            self.assertNotIn("terminal-send", [op for op, _ in fake.operations])

    def test_new_worktree_with_existing_agent_session_allows_new_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1")
            config = write_config(root, root / "projects")
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            real_terminals = fake.terminals

            def duplicated(worktree: dispatcher.OrcaWorktree) -> tuple[dispatcher.OrcaTerminal, ...]:
                return real_terminals(worktree) + (
                    dispatcher.OrcaTerminal(f"term-{worktree.path.name}-2", worktree.worktree_id, "claude", True, True),
                )

            with mock.patch.object(fake, "terminals", side_effect=duplicated):
                result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            self.assertEqual(sum(op == "terminal-send" for op, _ in fake.operations), 1)

    def test_reuse_rejects_base_branch_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1", branch="origin/release")
            config = write_config(root, root / "projects")
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            creation = fake.worktree_create(
                dispatcher.worktree_name_for(item), "repo-mapped", "master-new",
                dispatcher.worktree_comment_for(item),
            )
            dispatcher.rename_worktree_branch(
                creation.path, dispatcher.worktree_branch_for(item, dispatcher.Repository("mapped", path)),
            )
            fake.operations.clear()

            result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertIn("基础分支", result["results"][0]["message"])
            self.assertNotIn("worktree-create", [op for op, _ in fake.operations])

    def test_base_branch_matches_accepts_remote_tracking_ref(self) -> None:
        # 实测：--base-branch origin/release_9ji 被 Orca 记成 refs/remotes/origin/release_9ji。
        worktree = dispatcher.OrcaWorktree("repo::D:/probe", Path("D:/probe"))
        for recorded in ("origin/release_9ji", "refs/remotes/origin/release_9ji"):
            self.assertTrue(
                dispatcher.base_branch_matches(
                    dataclasses.replace(worktree, base_branch=recorded), "origin/release_9ji"
                ),
                recorded,
            )
        for recorded in ("release_9ji", "refs/heads/release_9ji"):
            self.assertTrue(
                dispatcher.base_branch_matches(
                    dataclasses.replace(worktree, base_branch=recorded), "release_9ji"
                ),
                recorded,
            )
        remote_tracking = dataclasses.replace(
            worktree, base_branch="refs/remotes/origin/release_9ji"
        )
        self.assertFalse(dispatcher.base_branch_matches(remote_tracking, "origin/release_saas"))
        self.assertFalse(dispatcher.base_branch_matches(remote_tracking, "release_9ji"))
        self.assertTrue(dispatcher.base_branch_matches(remote_tracking, None))

    def test_reused_worktree_accepts_remote_tracking_base_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1", branch="origin/release")
            config = write_config(root, root / "projects")
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            creation = fake.worktree_create(
                dispatcher.worktree_name_for(item), "repo-mapped", "refs/remotes/origin/release",
                dispatcher.worktree_comment_for(item),
            )
            dispatcher.rename_worktree_branch(
                creation.path, dispatcher.worktree_branch_for(item, dispatcher.Repository("mapped", path)),
            )
            fake.operations.clear()

            result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            self.assertNotIn("worktree-create", [op for op, _ in fake.operations])

    def test_reuse_rejects_foreign_comment_and_guides_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1")
            config = write_config(root, root / "projects")
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            creation = fake.worktree_create(dispatcher.worktree_name_for(item), "repo-mapped", item.base_branch, "")
            dispatcher.rename_worktree_branch(
                creation.path, dispatcher.worktree_branch_for(item, dispatcher.Repository("mapped", path)),
            )
            fake.operations.clear()

            result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertIn("人工确认后删除或改名", result["results"][0]["message"])
            self.assertNotIn("worktree-create", [op for op, _ in fake.operations])

    def test_existing_agent_session_allows_new_prompt_on_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1")
            config = write_config(root, root / "projects")
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            creation = fake.worktree_create(
                dispatcher.worktree_name_for(item), "repo-mapped", item.base_branch,
                dispatcher.worktree_comment_for(item),
            )
            dispatcher.rename_worktree_branch(
                creation.path, dispatcher.worktree_branch_for(item, dispatcher.Repository("mapped", path)),
            )
            worktree_id = creation.worktree_id
            duplicates = (
                dispatcher.OrcaTerminal("term-a", worktree_id, "claude", True, True),
                dispatcher.OrcaTerminal("term-b", worktree_id, "claude", True, True),
            )
            fake.operations.clear()

            with mock.patch.object(fake, "terminals", return_value=duplicates):
                result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "dispatched", result["results"][0])
            self.assertNotIn("worktree-create", [op for op, _ in fake.operations])
            self.assertEqual(sum(op == "terminal-send" for op, _ in fake.operations), 1)

    def test_agent_session_occupancy_counts_only_live_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            (projects / "repo-a" / ".git").mkdir(parents=True)
            config = write_config(root, projects)
            worktree = dispatcher.OrcaWorktree("repo::D:/probe", Path("D:/probe"))
            fake = FakeOrca()

            self.assertFalse(dispatcher.has_live_agent_terminal(config, fake, worktree))
            fake.add_agent_terminal(worktree.worktree_id)
            self.assertTrue(dispatcher.has_live_agent_terminal(config, fake, worktree))
            with mock.patch.object(fake, "terminals", return_value=(
                dispatcher.OrcaTerminal("term-disconnected", worktree.worktree_id, "claude", False, False),
            )):
                self.assertTrue(dispatcher.has_live_agent_terminal(config, fake, worktree))
                self.assertEqual(dispatcher.active_agent_terminals(config, fake, worktree), ())

            def shell_only(_worktree: object) -> tuple[dispatcher.OrcaTerminal, ...]:
                return (dispatcher.OrcaTerminal("term-shell", worktree.worktree_id, None, True, True),)

            self.assertFalse(dispatcher.has_live_agent_terminal(
                config, SimpleNamespace(terminals=shell_only), worktree
            ))

            def unreadable(_worktree: object) -> tuple[dispatcher.OrcaTerminal, ...]:
                raise dispatcher.DispatcherError("orca_command_failed", "读取失败")

            # 只读探测失败时按占用处理：宁可停手，也不并发投递到同一个工作区。
            self.assertTrue(dispatcher.has_live_agent_terminal(
                config, SimpleNamespace(terminals=unreadable), worktree
            ))

    def test_orca_error_retains_structured_transport_code(self) -> None:
        response = SimpleNamespace(returncode=1, stderr="", stdout=json.dumps({
            "ok": False, "error": {"code": "runtime_unavailable", "message": "模拟连接断开"},
        }))
        with mock.patch.object(dispatcher.subprocess, "run", return_value=response):
            with self.assertRaises(dispatcher.DispatcherError) as raised:
                dispatcher.OrcaClient().status()
        self.assertEqual(raised.exception.orca_code, "runtime_unavailable")

    def test_truncated_orca_lists_are_not_treated_as_absence(self) -> None:
        worktree = dispatcher.OrcaWorktree("repo::D:/probe", Path("D:/probe"))
        for method, args, key in (("worktrees", ("repo",), "worktrees"), ("terminals", (worktree,), "terminals")):
            with self.subTest(method=method):
                client = dispatcher.OrcaClient()
                with mock.patch.object(client, "_call", return_value={key: [], "truncated": True}):
                    with self.assertRaisesRegex(dispatcher.DispatcherError, "截断"):
                        getattr(client, method)(*args)

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

    def launch_single_task(self, **orca_options: object) -> SimpleNamespace:
        """单任务启动的公共脚手架：临时仓库、暂存快照、状态与 Orca 替身。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        projects = root / "projects"
        repository_path = projects / "repo-a"
        item = worktree_assignment(projects, repository_path, "XSWL-1")
        config = write_config(root, projects)
        store = dispatcher.StateStore(config.state_file)
        fake = FakeOrca({repository_path: "repo-mapped"}, **orca_options)
        result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)
        return SimpleNamespace(
            result=result, fake=fake, item=item, config=config, store=store,
            payload=result["results"][0], status=store.status("XSWL-1"),
        )

    def test_worktree_create_never_requests_an_agent(self) -> None:
        payload = {"worktree": {
            "id": "repo-1::D:/probe", "path": "D:/probe", "repoId": "repo-1",
            "baseRef": "origin/release",
        }}
        with mock.patch.object(dispatcher.OrcaClient, "_call", return_value=payload) as call:
            created = dispatcher.OrcaClient("orca").worktree_create(
                "XSWL-1", "repo-1", "origin/release", "orca-task-dispatcher:XSWL-1",
            )

        arguments = call.call_args.args
        self.assertNotIn("--agent", arguments)
        self.assertNotIn("--prompt", arguments)
        self.assertIn("--comment", arguments)
        self.assertIn("--no-parent", arguments)
        self.assertEqual(created.worktree_id, "repo-1::D:/probe")

    def test_dispatch_pipeline_never_touches_orchestration(self) -> None:
        launched = self.launch_single_task()

        self.assertEqual(launched.status, "dispatched")
        orchestration = [
            operation for operation, _ in launched.fake.operations
            if "orchestration" in operation or operation.startswith(("worker-", "run-"))
        ]
        self.assertEqual(orchestration, [])
        self.assertFalse((launched.store.runtime_dir / "orchestration.json").exists())

    def test_same_prompt_across_flows_skips_duplicate_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            first = worktree_assignment(projects, repository_path, "XSWL-1", dispatch_flow="direct")
            second = dataclasses.replace(first, dispatch_flow="proposal")
            config = write_config(root, projects)
            config = dataclasses.replace(config, flows={
                **config.flows,
                "proposal": dataclasses.replace(
                    config.flows["proposal"], command_template=config.flows["direct"].command_template
                ),
            })
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({repository_path: "repo-mapped"})

            first_result = dispatcher.launch(config, (first,), store, fake, force_unlock=False)
            first_state = store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1", dispatch_flow="direct")]
            second_result = dispatcher.launch(config, (second,), store, fake, force_unlock=False)

            self.assertEqual(first_result["results"][0]["status"], "dispatched")
            self.assertEqual(second_result["results"][0]["status"], "skipped_duplicate_session")
            self.assertEqual(store.status("XSWL-1", dispatch_flow="direct"), "dispatched")
            self.assertEqual(store.status("XSWL-1", dispatch_flow="proposal"), "dispatched")
            self.assertEqual(sum(op == "terminal-create" for op, _ in fake.operations), 1)
            self.assertEqual(sum(op == "terminal-send" for op, _ in fake.operations), 1)
            self.assertEqual(
                second_result["results"][0]["duplicate_of_state_key"],
                "XSWL-1::legacy::flow::direct",
            )
            second_key = dispatcher.state_key_for("XSWL-1", dispatch_flow="proposal")
            second_state = store.snapshot()["tasks"][second_key]
            self.assertEqual(second_state["terminal_handle"], first_state["terminal_handle"])
            self.assertEqual(second_state["send_request_id"], first_state["send_request_id"])
            recovered = dispatcher.recover(config, store, fake, task_id="XSWL-1", dispatch_flow="proposal", force_unlock=False)
            self.assertEqual(recovered["results"][0]["status"], "recovered")
            store.reset("XSWL-1", force_unlock=False, force=True, dispatch_flow="proposal")
            self.assertEqual(store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1", dispatch_flow="direct")], first_state)

    def test_different_prompt_across_flows_starts_second_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            first = worktree_assignment(projects, repository_path, "XSWL-1", dispatch_flow="direct")
            second = dataclasses.replace(first, dispatch_flow="proposal")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({repository_path: "repo-mapped"})

            result = dispatcher.launch(config, (first, second), store, fake, force_unlock=False)

            self.assertEqual([value["status"] for value in result["results"]], ["dispatched", "dispatched"])
            self.assertEqual(sum(op == "worktree-create" for op, _ in fake.operations), 1)
            self.assertEqual(len(fake.worktrees_by_id), 1)
            self.assertEqual(sum(op == "terminal-create" for op, _ in fake.operations), 2)
            self.assertEqual(sum(op == "terminal-send" for op, _ in fake.operations), 2)
            self.assertEqual(
                len({
                    state["initial_prompt_digest"]
                    for state in store.snapshot()["tasks"].values()
                }),
                2,
            )

    def test_legacy_active_terminal_without_digest_is_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            first = worktree_assignment(projects, repository_path, "XSWL-1", dispatch_flow="direct")
            second = dataclasses.replace(first, dispatch_flow="proposal")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({repository_path: "repo-mapped"})
            config = dataclasses.replace(config, flows={
                **config.flows,
                "proposal": dataclasses.replace(
                    config.flows["proposal"], command_template=config.flows["direct"].command_template
                ),
            })

            # 旧状态没有摘要，不能证明首次文本一致；应新开终端而不是静默跳过。
            first_result = dispatcher.launch(config, (first,), store, fake, force_unlock=False)
            state = store.snapshot()
            direct_key = dispatcher.state_key_for("XSWL-1", dispatch_flow="direct")
            dispatcher.atomic_write_json(store.state_file, {
                **state,
                "tasks": {**state["tasks"], direct_key: {
                    key: value for key, value in state["tasks"][direct_key].items()
                    if key != "initial_prompt_digest"
                }},
            })
            second_result = dispatcher.launch(config, (second,), store, fake, force_unlock=False)

            self.assertEqual(first_result["results"][0]["status"], "dispatched")
            self.assertEqual(second_result["results"][0]["status"], "dispatched")
            self.assertEqual(sum(op == "terminal-create" for op, _ in fake.operations), 2)

    def test_duplicate_accepted_without_turn_started_keeps_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            path = projects / "repo-a"
            first = worktree_assignment(projects, path, "XSWL-1", dispatch_flow="direct")
            second = dataclasses.replace(first, dispatch_flow="proposal")
            config = write_config(root, projects)
            config = dataclasses.replace(config, flows={
                **config.flows,
                "proposal": dataclasses.replace(
                    config.flows["proposal"], command_template=config.flows["direct"].command_template
                ),
            })
            fake = FakeOrca({path: "repo-mapped"}, send_stages=("input_accepted",))
            store = dispatcher.StateStore(config.state_file)
            result = dispatcher.launch(config, (first, second), store, fake, force_unlock=False)
            self.assertEqual(result["results"][1]["status"], "skipped_duplicate_session", result)
            self.assertEqual(result["results"][1]["dispatch_state"], "input_accepted")
            self.assertIn(dispatcher.TURN_START_UNOBSERVED, result["results"][1].get("warnings", []))
            self.assertEqual(sum(op == "terminal-send" for op, _ in fake.operations), 1)

    def test_inactive_terminal_digest_does_not_block_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            path = projects / "repo-a"
            first = worktree_assignment(projects, path, "XSWL-1", dispatch_flow="direct")
            second = dataclasses.replace(first, dispatch_flow="proposal")
            config = write_config(root, projects)
            config = dataclasses.replace(config, flows={
                **config.flows,
                "proposal": dataclasses.replace(
                    config.flows["proposal"], command_template=config.flows["direct"].command_template
                ),
            })
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            result = dispatcher.launch(config, (first,), store, fake, force_unlock=False)
            handle = result["results"][0]["terminal_handle"]
            fake.terminals_by_handle[handle] = dataclasses.replace(
                fake.terminals_by_handle[handle], connected=False, writable=False
            )
            second_result = dispatcher.launch(config, (second,), store, fake, force_unlock=False)
            self.assertEqual(second_result["results"][0]["status"], "dispatched", second_result)
            self.assertEqual(sum(op == "terminal-send" for op, _ in fake.operations), 2)

    def test_uncertain_same_prompt_never_creates_a_second_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            first = worktree_assignment(projects, repository_path, "XSWL-1", dispatch_flow="direct")
            second = dataclasses.replace(first, dispatch_flow="proposal")
            config = write_config(root, projects)
            config = dataclasses.replace(config, flows={
                **config.flows,
                "proposal": dataclasses.replace(
                    config.flows["proposal"], command_template=config.flows["direct"].command_template
                ),
            })
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({repository_path: "repo-mapped"}, terminal_send_failure="发送回执丢失")
            failed = dispatcher.launch(config, (first,), store, fake, force_unlock=False)
            self.assertEqual(failed["results"][0]["status"], "requires_manual_reset")
            fake.terminal_send_failure = None

            result = dispatcher.launch(config, (second,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset", result)
            self.assertEqual(sum(op == "terminal-create" for op, _ in fake.operations), 1)
            self.assertEqual(sum(op == "terminal-send" for op, _ in fake.operations), 1)
            self.assertIn("首次投递结果未确认", result["results"][0]["message"])

    def test_prompt_digest_is_saved_before_single_send(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            path = projects / "repo-a"
            item = worktree_assignment(projects, path, "XSWL-1", dispatch_flow="direct")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({path: "repo-mapped"})
            expected = dispatcher.command_for(config, item)

            def assert_saved(handle: str) -> None:
                state = store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1", dispatch_flow="direct")]
                self.assertEqual(state["dispatch_state"], "sending")
                self.assertEqual(state["terminal_handle"], handle)
                self.assertEqual(state["initial_prompt_digest"], hashlib.sha256(expected.encode("utf-8")).hexdigest())
                self.assertNotIn(expected, store.state_file.read_text(encoding="utf-8"))

            fake.before_terminal_send = assert_saved
            result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)
            self.assertEqual(result["results"][0]["status"], "dispatched", result)
            self.assertEqual(sum(op == "terminal-send" for op, _ in fake.operations), 1)

    def test_unreadable_active_terminals_stop_without_creating_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "projects" / "repo-a"
            item = worktree_assignment(root / "projects", path, "XSWL-1")
            config = write_config(root, root / "projects")
            fake = FakeOrca({path: "repo-mapped"})
            with mock.patch.object(fake, "terminals", side_effect=dispatcher.DispatcherError("orca_invalid_json", "无法读取终端")):
                result = dispatcher.launch(config, (item,), dispatcher.StateStore(config.state_file), fake, force_unlock=False)
            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertNotIn("terminal-create", [op for op, _ in fake.operations])
            self.assertNotIn("terminal-send", [op for op, _ in fake.operations])

    def test_shared_worktree_different_flows_preserve_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            proposal = worktree_assignment(projects, repository_path, "XSWL-1", dispatch_flow="proposal")
            direct = dataclasses.replace(proposal, dispatch_flow="direct", requirement_snapshot_path=None)
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({repository_path: "repo-mapped"})
            first = dispatcher.launch(config, (proposal,), store, fake, force_unlock=False)
            self.assertEqual(first["results"][0]["status"], "dispatched", first)
            proposal_key = dispatcher.state_key_for("XSWL-1", dispatch_flow="proposal")
            original_state = store.snapshot()["tasks"][proposal_key]
            snapshot = Path(original_state["requirement_snapshot_path"])
            snapshot.write_text(snapshot.read_text(encoding="utf-8") + "\n旧会话的新增内容\n", encoding="utf-8")
            before = snapshot.read_bytes()

            with mock.patch.object(dispatcher, "sync_worktree_files", side_effect=AssertionError("不能重新同步活动工作区")):
                second = dispatcher.launch(config, (direct,), store, fake, force_unlock=False)

            self.assertEqual(second["results"][0]["status"], "dispatched", second)
            self.assertEqual(snapshot.read_bytes(), before)
            self.assertEqual(store.snapshot()["tasks"][proposal_key], original_state)
            self.assertEqual(len(fake.worktrees_by_id), 1)
            self.assertEqual(sum(op == "worktree-create" for op, _ in fake.operations), 1)
            self.assertEqual(sum(op == "terminal-create" for op, _ in fake.operations), 2)
            self.assertNotEqual(first["results"][0]["terminal_handle"], second["results"][0]["terminal_handle"])

    def test_turn_started_confirms_dispatch_without_warning(self) -> None:
        launched = self.launch_single_task()

        self.assertEqual(launched.status, "dispatched")
        self.assertEqual(launched.payload["dispatch_state"], "turn_started")
        self.assertEqual(launched.payload["send_request_id"], "req-1")
        self.assertNotIn("warnings", launched.payload)
        state = launched.store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1")]
        self.assertEqual(state["send_stages"], ["input_accepted", "turn_started"])
        self.assertTrue(state["send_accepted"])

    def test_input_accepted_dispatches_once_with_warning(self) -> None:
        launched = self.launch_single_task(send_stages=("input_accepted",))

        self.assertEqual(launched.status, "dispatched")
        self.assertEqual(launched.payload["dispatch_state"], "input_accepted")
        self.assertEqual(launched.payload["warnings"], [dispatcher.TURN_START_UNOBSERVED])
        # 观察期静默不是重发理由：只投递一次。
        self.assertEqual(sum(op == "terminal-send" for op, _ in launched.fake.operations), 1)

    def test_refused_send_marks_manual_reset_without_resend(self) -> None:
        launched = self.launch_single_task(send_accepted=False, send_stages=("input_accepted",))

        self.assertEqual(launched.status, "requires_manual_reset")
        self.assertIn("未接受", launched.payload["message"])
        self.assertEqual(launched.payload["send_request_id"], "req-1")
        self.assertEqual(sum(op == "terminal-send" for op, _ in launched.fake.operations), 1)

    def test_unobservable_host_marks_manual_reset_without_resend(self) -> None:
        launched = self.launch_single_task(
            send_stages=("input_accepted",),
            send_request_id="unsupported-old-host",
            send_observation="unsupported",
        )

        self.assertEqual(launched.status, "requires_manual_reset")
        self.assertIn("不支持投递观察", launched.payload["message"])
        self.assertEqual(sum(op == "terminal-send" for op, _ in launched.fake.operations), 1)

    def test_unparsable_send_receipt_marks_manual_reset(self) -> None:
        launched = self.launch_single_task(send_payload={"ok": True})

        self.assertEqual(launched.status, "requires_manual_reset")
        self.assertIn("send", launched.payload["message"])
        self.assertEqual(sum(op == "terminal-send" for op, _ in launched.fake.operations), 1)

    def test_terminal_not_idle_never_receives_text(self) -> None:
        launched = self.launch_single_task(terminal_idle=False)

        self.assertEqual(launched.status, "requires_manual_reset")
        self.assertIn("未在预算内进入空闲状态", launched.payload["message"])
        self.assertEqual([op for op, _ in launched.fake.operations if op == "terminal-send"], [])
        state = launched.store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1")]
        self.assertEqual(state["terminal_handle"], launched.payload["terminal_handle"])

    def test_sync_failure_stops_before_terminal_creation(self) -> None:
        with mock.patch.object(
            dispatcher, "sync_worktree_files",
            side_effect=dispatcher.DispatcherError("worktree_sync_failed", "同步失败"),
        ):
            launched = self.launch_single_task()

        self.assertEqual(launched.status, "requires_manual_reset")
        self.assertIn("同步失败", launched.payload["message"])
        self.assertNotIn("terminal-create", [op for op, _ in launched.fake.operations])
        self.assertNotIn("terminal-send", [op for op, _ in launched.fake.operations])

    def test_terminal_create_failure_marks_manual_reset_keeping_worktree(self) -> None:
        launched = self.launch_single_task(terminal_create_failure="句柄超时")

        self.assertEqual(launched.status, "requires_manual_reset")
        self.assertIn("句柄超时", launched.payload["message"])
        self.assertNotIn("terminal-send", [op for op, _ in launched.fake.operations])
        self.assertEqual(len(launched.fake.worktrees_by_id), 1)

    def test_state_write_after_send_marks_manual_reset_without_resend(self) -> None:
        with mock.patch.object(
            dispatcher.StateStore, "mark_dispatched",
            side_effect=dispatcher.DispatcherError("state_unreadable", "状态写入失败"),
        ):
            launched = self.launch_single_task()

        self.assertEqual(launched.status, "requires_manual_reset")
        self.assertEqual(launched.payload["send_request_id"], "req-1")
        self.assertTrue(launched.payload["terminal_handle"])
        self.assertEqual(sum(op == "terminal-send" for op, _ in launched.fake.operations), 1)
        self.assertEqual(launched.store.status("XSWL-1"), "requires_manual_reset")

    def test_suffix_worktree_with_agent_session_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            repository_path = projects / "repo-a"
            item = worktree_assignment(projects, repository_path, "XSWL-1")
            config = write_config(root, projects)
            store = dispatcher.StateStore(config.state_file)
            fake = FakeOrca({repository_path: "repo-mapped"})
            suffix = fake.worktree_create(
                f"{dispatcher.worktree_name_for(item)}-2", "repo-mapped", item.base_branch,
                dispatcher.worktree_comment_for(item),
            )
            fake.add_agent_terminal(suffix.worktree_id)
            fake.operations.clear()

            result = dispatcher.launch(config, (item,), store, fake, force_unlock=False)

            self.assertEqual(result["results"][0]["status"], "requires_manual_reset")
            self.assertIn("开发会话", result["results"][0]["message"])
            self.assertNotIn("worktree-remove", [op for op, _ in fake.operations])
            self.assertTrue(suffix.path.is_dir())

    def recover_record(self, store: dispatcher.StateStore, **fields: object) -> None:
        """写入一条可直接 recover 的分发记录。"""
        value = {
            "task_id": "XSWL-1",
            "repository": "mapped",
            "task_url": "https://jira.example/XSWL-1",
            "title": "XSWL-1",
            "dispatch_flow": "complete",
            "status": "dispatched",
            "terminal_handle": "term-1",
            "worktree_id": "repo-mapped::D:/probe",
            "send_accepted": True,
            "send_stages": ["input_accepted", "turn_started"],
            **fields,
        }
        dispatcher.atomic_write_json(store.state_file, {
            "version": 1,
            "tasks": {dispatcher.state_key_for("XSWL-1"): value},
        })

    def recover_store(self) -> tuple[dispatcher.Config, dispatcher.StateStore]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        projects = root / "projects"
        (projects / "repo-a" / ".git").mkdir(parents=True)
        return write_config(root, projects), dispatcher.StateStore(root / ".runtime" / "state.json")

    def test_recover_confirms_only_accepted_send_evidence(self) -> None:
        config, store = self.recover_store()
        self.recover_record(store)
        fake = FakeOrca()
        fake.terminals_by_handle["term-1"] = dispatcher.OrcaTerminal(
            "term-1", "repo-mapped::D:/probe", "claude", True, True,
        )

        result = dispatcher.recover(config, store, fake, task_id="XSWL-1", force_unlock=False)

        self.assertEqual([item["status"] for item in result["results"]], ["recovered"])
        self.assertEqual(store.status("XSWL-1"), "dispatched")
        state = store.snapshot()["tasks"][dispatcher.state_key_for("XSWL-1")]
        self.assertEqual(state["terminal_handle"], "term-1")
        self.assertEqual(len(state["recovery_history"]), 1)

    def test_recover_without_send_evidence_requires_manual_reset(self) -> None:
        config, store = self.recover_store()
        self.recover_record(store, status="launching", send_accepted=None, send_stages=[])
        fake = FakeOrca()
        fake.terminals_by_handle["term-1"] = dispatcher.OrcaTerminal(
            "term-1", "repo-mapped::D:/probe", "claude", True, True,
        )

        result = dispatcher.recover(config, store, fake, task_id="XSWL-1", force_unlock=False)

        self.assertEqual([item["status"] for item in result["results"]], ["requires_manual_reset"])
        self.assertEqual(store.status("XSWL-1"), "requires_manual_reset")
        self.assertIn("dispatch_not_confirmed", store.history_file.read_text(encoding="utf-8"))

    def test_recover_rejects_terminal_from_other_worktree(self) -> None:
        config, store = self.recover_store()
        self.recover_record(store, worktree_id="repo-mapped::D:/other")
        fake = FakeOrca()
        fake.terminals_by_handle["term-1"] = dispatcher.OrcaTerminal(
            "term-1", "repo-mapped::D:/probe", "claude", True, True,
        )

        result = dispatcher.recover(config, store, fake, task_id="XSWL-1", force_unlock=False)

        self.assertEqual([item["status"] for item in result["results"]], ["requires_manual_reset"])
        self.assertIn("terminal_worktree_mismatch", store.history_file.read_text(encoding="utf-8"))

    def test_recover_leaves_legacy_orchestration_records_untouched(self) -> None:
        config, store = self.recover_store()
        self.recover_record(store, terminal_handle=None, dispatch_id="ctx-1", orchestration_run_id="run-1")
        store.runtime_dir.mkdir(parents=True, exist_ok=True)
        run_file = store.runtime_dir / "orchestration.json"
        run_file.write_text('{"runId": "run-1"}\n', encoding="utf-8")
        fake = FakeOrca()

        result = dispatcher.recover(config, store, fake, task_id="XSWL-1", force_unlock=False)

        self.assertEqual([item["status"] for item in result["results"]], ["requires_manual_reset"])
        self.assertIn("terminal_handle_missing", store.history_file.read_text(encoding="utf-8"))
        self.assertEqual(run_file.read_text(encoding="utf-8"), '{"runId": "run-1"}\n')
        self.assertEqual(
            [op for op, _ in fake.operations if "orchestration" in op or op.startswith(("worker-", "run-"))],
            [],
        )

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
