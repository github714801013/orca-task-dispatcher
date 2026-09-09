#!/usr/bin/env python3
"""通过 Orca CLI 确定性分发开发任务。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import string
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping
from urllib.parse import quote, urlparse

import yaml


class DispatcherError(Exception):
    """可安全返回给 Skill 的业务错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DispatcherError("invalid_input", f"{field} 必须是非空字符串")
    return value.strip()


def require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DispatcherError("invalid_config", f"{field} 必须是对象")
    return value


def require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DispatcherError("invalid_config", f"{field} 必须是布尔值")
    return value


def require_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise DispatcherError("invalid_config", f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return value


def require_string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise DispatcherError("invalid_config", f"{field} 必须是字符串列表")
    return tuple(require_text(item, field) for item in value)


def require_optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return require_text(value, field)


def require_branch_mapping(value: Any, field: str) -> Mapping[str, str | None]:
    """解析分支配置：兼容纯字符串列表（无描述）与「分支名: 描述」键值映射。"""
    if isinstance(value, list):
        return {require_text(item, field): None for item in value}
    if not isinstance(value, Mapping):
        raise DispatcherError("invalid_config", f"{field} 必须是字符串列表或「分支名: 描述」对象")
    mapping: dict[str, str | None] = {}
    for branch, description in value.items():
        name = require_text(branch, field)
        if description is None or (isinstance(description, str) and not description.strip()):
            mapping[name] = None
        else:
            mapping[name] = require_text(description, f"{field}.{name}")
    return mapping


TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TENANT_SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
COMMAND_ARGUMENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
SHELL_PROMPT_PATTERN = re.compile(
    r"(?:^|\n)(?:PS [^\n>]+>|(?:[A-Za-z]:)?[\\/][^\n>]*>|[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[^\n$#]*[$#])\s*\Z"
)
TASK_STATUSES = frozenset({"launching", "dispatched", "requires_manual_reset"})
DISPATCH_FLOWS = frozenset({"direct", "complete", "proposal"})
COMMAND_TEMPLATE_FIELDS = frozenset({"task_url", "task_id", "base_branch", "requirement_snapshot_path"})
TASK_CONTEXT_TEMPLATE_FIELDS = frozenset({
    "title", "description", "assignee", "tenant", "assignment_id",
    "reference_plan", "gitnexus_report_path", "requirement_snapshot_path", "source_task_id",
})
COMMAND_TEMPLATE_ALLOWED_FIELDS = COMMAND_TEMPLATE_FIELDS | TASK_CONTEXT_TEMPLATE_FIELDS
CLAUDE_AUTHORIZATION_ACCEPT = "Yes, I accept"
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
CLAUDE_AUTHORIZATION_OPTION_PATTERN = re.compile(r"^(?:[>❯]\s*)?(No, exit|Yes, I accept)$")
ORCA_COMMAND_TIMEOUT_SECONDS = 30
READY_TIMEOUT_MS_MAX = 360_000


def require_task_id(value: Any) -> str:
    task_id = require_text(value, "task_id")
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise DispatcherError("invalid_input", "task_id 格式不合法")
    return task_id


def require_tenant_slug(value: Any) -> str:
    tenant_slug = require_text(value, "tenant_slug")
    if not TENANT_SLUG_PATTERN.fullmatch(tenant_slug):
        raise DispatcherError("invalid_input", "tenant_slug 格式不合法")
    return tenant_slug


def require_command_argument(value: str, field: str) -> str:
    if not COMMAND_ARGUMENT_PATTERN.fullmatch(value):
        raise DispatcherError("invalid_input", f"{field} 包含不支持的命令字符")
    return value


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    task_url: str
    description: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Task":
        task_url = require_text(value.get("task_url"), "task_url")
        parsed_url = urlparse(task_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc or any(char.isspace() for char in task_url):
            raise DispatcherError("invalid_input", "task_url 必须是无空白字符的 HTTPS 地址")
        description = value.get("description", "")
        if not isinstance(description, str):
            raise DispatcherError("invalid_input", "description 必须是字符串")
        return cls(
            task_id=require_task_id(value.get("task_id")),
            title=require_text(value.get("title"), "title"),
            task_url=task_url,
            description=description,
        )


@dataclass(frozen=True)
class Repository:
    name: str
    path: Path
    description: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"name": self.name, "path": self.path.as_posix(), "description": self.description}


@dataclass(frozen=True)
class OrcaWorktree:
    worktree_id: str
    path: Path

    @property
    def selector(self) -> str:
        return f"id:{self.worktree_id}"


@dataclass(frozen=True)
class Assignment:
    task: Task
    repository: str
    repository_path: Path
    base_branch: str | None
    tenant: str = "legacy"
    tenant_slug: str = "legacy"
    worktree_path: Path | None = None
    reference_plan: str | None = None
    assignee: str | None = None
    source_task_id: str | None = None
    source_assignee: str | None = None
    parent_task_id: str | None = None
    parent_assignee: str | None = None
    gitnexus_report_path: Path | None = None
    requirement_snapshot_path: Path | None = None
    dispatch_flow: str = "complete"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Assignment":
        unknown_fields = set(value) - {
            "task_id", "title", "description", "task_url", "repository", "repository_path",
            "base_branch", "worktree_path", "reference_plan", "assignee", "tenant", "tenant_slug", "assignment_id",
            "source_task_id", "source_assignee", "parent_task_id", "parent_assignee", "gitnexus_report_path",
            "requirement_snapshot_path",
            "dispatch_flow",
        }
        if unknown_fields:
            raise DispatcherError("invalid_input", f"任务包含未知字段：{sorted(unknown_fields)[0]}")
        branch = value.get("base_branch")
        if branch is not None and (not isinstance(branch, str) or not branch.strip()):
            raise DispatcherError("invalid_input", "base_branch 必须是字符串或 null")
        worktree_path = value.get("worktree_path")
        if worktree_path is not None and (not isinstance(worktree_path, str) or not worktree_path.strip()):
            raise DispatcherError("invalid_input", "worktree_path 必须是字符串或 null")
        reference_plan = value.get("reference_plan")
        if reference_plan is not None and (not isinstance(reference_plan, str) or not reference_plan.strip()):
            raise DispatcherError("invalid_input", "reference_plan 必须是字符串或 null")
        assignee = value.get("assignee")
        if assignee is not None and (not isinstance(assignee, str) or not assignee.strip()):
            raise DispatcherError("invalid_input", "assignee 必须是字符串或 null")
        tenant = require_optional_text(value.get("tenant"), "tenant") or "legacy"
        tenant_slug = require_tenant_slug(value.get("tenant_slug", "legacy"))
        if tenant_slug == "legacy" and tenant != "legacy":
            raise DispatcherError("invalid_input", "已指定 tenant 时不能使用保留 tenant_slug：legacy")
        source_task_id = value.get("source_task_id")
        if source_task_id is not None:
            source_task_id = require_task_id(source_task_id)
        source_assignee = require_optional_text(value.get("source_assignee"), "source_assignee")
        parent_task_id = value.get("parent_task_id")
        if parent_task_id is not None:
            parent_task_id = require_task_id(parent_task_id)
        parent_assignee = require_optional_text(value.get("parent_assignee"), "parent_assignee")
        gitnexus_report_path = value.get("gitnexus_report_path")
        if gitnexus_report_path is not None and (not isinstance(gitnexus_report_path, str) or not gitnexus_report_path.strip()):
            raise DispatcherError("invalid_input", "gitnexus_report_path 必须是字符串或 null")
        requirement_snapshot_path = value.get("requirement_snapshot_path")
        if requirement_snapshot_path is not None and (not isinstance(requirement_snapshot_path, str) or not requirement_snapshot_path.strip()):
            raise DispatcherError("invalid_input", "requirement_snapshot_path 必须是字符串或 null")
        repository_path = Path(require_text(value.get("repository_path"), "repository_path"))
        if not repository_path.is_absolute():
            raise DispatcherError("invalid_input", "repository_path 必须是绝对路径")
        worktree_value = worktree_path.strip() if isinstance(worktree_path, str) else None
        if worktree_value is not None and not Path(worktree_value).is_absolute():
            raise DispatcherError("invalid_input", "worktree_path 必须是绝对路径")
        report_value = gitnexus_report_path.strip() if isinstance(gitnexus_report_path, str) else None
        if report_value is not None and not Path(report_value).is_absolute():
            raise DispatcherError("invalid_input", "gitnexus_report_path 必须是绝对路径")
        snapshot_value = requirement_snapshot_path.strip() if isinstance(requirement_snapshot_path, str) else None
        if snapshot_value is not None and not Path(snapshot_value).is_absolute():
            raise DispatcherError("invalid_input", "requirement_snapshot_path 必须是绝对路径")
        dispatch_flow = require_text(value.get("dispatch_flow", "complete"), "dispatch_flow")
        if dispatch_flow not in DISPATCH_FLOWS:
            raise DispatcherError("invalid_input", "dispatch_flow 必须是 direct、complete 或 proposal")
        return cls(
            task=Task.from_dict(value),
            repository=require_text(value.get("repository"), "repository"),
            repository_path=repository_path,
            base_branch=branch.strip() if isinstance(branch, str) else None,
            tenant=tenant,
            tenant_slug=tenant_slug,
            worktree_path=Path(worktree_value) if worktree_value is not None else None,
            reference_plan=reference_plan.strip() if isinstance(reference_plan, str) else None,
            assignee=assignee.strip() if isinstance(assignee, str) else None,
            source_task_id=source_task_id,
            source_assignee=source_assignee,
            parent_task_id=parent_task_id,
            parent_assignee=parent_assignee,
            gitnexus_report_path=Path(report_value) if report_value is not None else None,
            requirement_snapshot_path=Path(snapshot_value) if snapshot_value is not None else None,
            dispatch_flow=dispatch_flow,
        )

    @property
    def assignment_id(self) -> str:
        return self.task.task_id if self.tenant_slug == "legacy" else f"{self.task.task_id}::{self.tenant_slug}"

    def to_dict(self) -> dict[str, str | None]:
        return {
            "task_id": self.task.task_id,
            "title": self.task.title,
            "description": self.task.description,
            "task_url": self.task.task_url,
            "repository": self.repository,
            "tenant": self.tenant,
            "tenant_slug": self.tenant_slug,
            "assignment_id": self.assignment_id,
            "repository_path": self.repository_path.as_posix(),
            "base_branch": self.base_branch,
            "worktree_path": self.worktree_path.as_posix() if self.worktree_path else None,
            "reference_plan": self.reference_plan,
            "assignee": self.assignee,
            "source_task_id": self.source_task_id,
            "source_assignee": self.source_assignee,
            "parent_task_id": self.parent_task_id,
            "parent_assignee": self.parent_assignee,
            "gitnexus_report_path": self.gitnexus_report_path.as_posix() if self.gitnexus_report_path else None,
            "requirement_snapshot_path": self.requirement_snapshot_path.as_posix() if self.requirement_snapshot_path else None,
            "dispatch_flow": self.dispatch_flow,
        }


@dataclass(frozen=True)
class TerminalPlan:
    repository: Repository
    assignments: tuple[Assignment, ...]
    tab_title: str
    layout_mode: str


@dataclass(frozen=True)
class TerminalSnapshot:
    handle: str
    worktree_id: str
    worktree_path: Path
    tab_id: str
    leaf_id: str
    title: str
    connected: bool
    writable: bool
    agent_identity: str | None
    preview: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TerminalSnapshot":
        handle = require_text(value.get("handle"), "Orca terminal.handle")
        worktree_path = Path(require_text(value.get("worktreePath"), "Orca terminal.worktreePath"))
        return cls(
            handle=handle,
            worktree_id=require_text(value.get("worktreeId"), "Orca terminal.worktreeId"),
            worktree_path=worktree_path,
            tab_id=require_text(value.get("tabId"), "Orca terminal.tabId"),
            leaf_id=require_text(value.get("leafId"), "Orca terminal.leafId"),
            title=require_text(value.get("title"), "Orca terminal.title"),
            connected=require_bool(value.get("connected"), "Orca terminal.connected"),
            writable=require_bool(value.get("writable"), "Orca terminal.writable"),
            agent_identity=value.get("agentIdentity") if isinstance(value.get("agentIdentity"), str) else None,
            preview=value.get("preview") if isinstance(value.get("preview"), str) else "",
        )


@dataclass(frozen=True)
class TerminalRecord:
    assignment: Assignment
    handle: str
    tab_title: str
    layout_mode: str
    pane_index: int
    snapshot: TerminalSnapshot


@dataclass(frozen=True)
class Project:
    path: str
    base_branches: Mapping[str, str | None]
    description: str | None = None
    tenants: Mapping[str, str] = field(default_factory=dict)
    branch_priority: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class Config:
    root: Path
    projects_root: Path
    projects: Mapping[str, Project]
    branch_options: Mapping[str, str | None]
    validate_branch: bool
    max_tasks: int
    max_agents: int
    max_panes: int
    ready_timeout_ms: int
    read_retry_attempts: int
    read_retry_delay_ms: int
    ready_retry_attempts: int
    send_retry_attempts: int
    shell_command: str
    agent_extra_args: str
    state_file: Path
    task_url_template: str
    task_source_type: str
    task_source_query: str
    fetch_prompt: str
    agent_command: str
    command_templates: Mapping[str, str]
    reference_plan_field: str | None = None
    task_source_flows: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    layout_mode: str = "split"
    session_prompt: str = ""
    recovery_session_prompt: str = ""

    @property
    def runtime_dir(self) -> Path:
        return self.state_file.parent

    def branches_for(self, repository: str) -> tuple[str, ...]:
        return tuple(self.branch_map_for(repository))

    def branch_map_for(self, repository: str) -> Mapping[str, str | None]:
        project = self.projects.get(repository)
        if project is not None:
            return project.base_branches
        return self.branch_options

    def tenants_for(self, repository: str) -> Mapping[str, str]:
        project = self.projects.get(repository)
        return project.tenants if project is not None else {}


def skill_root_from_config(config_file: Path) -> Path:
    return config_file.parent.parent.resolve()


def config_path_from_script() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "dispatcher.yaml"


def config_default_path_from_script() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "dispatcher.default.yaml"


def merge_config_values(base: Any, override: Any, path: tuple[str, ...] = ()) -> Any:
    if path == ("workspace", "projects"):
        return override
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        return {
            **base,
            **{
                key: merge_config_values(base[key], value, (*path, str(key))) if key in base else value
                for key, value in override.items()
            },
        }
    return override


def read_config_yaml(config_file: Path, *, required: bool) -> Mapping[str, Any] | None:
    try:
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return None
        raise DispatcherError("config_unreadable", f"无法读取配置：{config_file}")
    except OSError as error:
        raise DispatcherError("config_unreadable", f"无法读取配置：{config_file}") from error
    except yaml.YAMLError as error:
        raise DispatcherError("invalid_config", f"YAML 格式错误：{error}") from error
    return require_mapping(raw, "配置根")


def config_layers(config_file: Path | None) -> Mapping[str, Any]:
    default_file = config_default_path_from_script()
    default = read_config_yaml(default_file, required=False)
    user_file = config_file or config_path_from_script()
    user = read_config_yaml(user_file, required=False)
    if default is None and user is None:
        raise DispatcherError("config_unreadable", f"默认配置不存在：{default_file}")
    if default is None:
        return user or {}
    return merge_config_values(default, user or {})

def relative_to_root(root: Path, value: Any, field: str) -> Path:
    candidate = Path(require_text(value, field))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise DispatcherError("invalid_config", f"{field} 必须位于技能目录内")
    return root / candidate


def require_project_tenants(value: Any, field: str) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    mapping = require_mapping(value, field)
    tenants: dict[str, str] = {}
    for name, item in mapping.items():
        tenant = require_text(name, f"{field} 名称")
        details = require_mapping(item, f"{field}.{tenant}")
        slug = require_tenant_slug(details.get("slug", tenant))
        if slug in tenants.values():
            raise DispatcherError("invalid_config", f"{field} 包含重复 tenant slug：{slug}")
        tenants[tenant] = slug
    return MappingProxyType(tenants)


def require_branch_priority(value: Any, field: str) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DispatcherError("invalid_config", f"{field} 必须是列表")
    rules: list[Mapping[str, object]] = []
    for index, item in enumerate(value):
        rule = require_mapping(item, f"{field}[{index}]")
        when_all = require_string_list(rule.get("when_all"), f"{field}[{index}].when_all")
        branch = require_text(rule.get("branch"), f"{field}[{index}].branch")
        rules.append(MappingProxyType({"when_all": when_all, "branch": branch}))
    return tuple(rules)


def load_config(config_file: Path) -> Config:
    root_data = config_layers(config_file)

    workspace = require_mapping(root_data.get("workspace"), "workspace")
    base_branch = require_mapping(root_data.get("base_branch"), "base_branch")
    task_source = require_mapping(root_data.get("task_source"), "task_source")
    interaction = require_mapping(root_data.get("interaction"), "interaction")
    dispatch = require_mapping(root_data.get("dispatch"), "dispatch")
    skill = require_mapping(dispatch.get("skill"), "dispatch.skill")
    layout = require_mapping(dispatch.get("layout"), "dispatch.layout")
    terminal = require_mapping(dispatch.get("terminal"), "dispatch.terminal")
    concurrency = require_mapping(dispatch.get("concurrency"), "dispatch.concurrency")
    dedup = require_mapping(root_data.get("dedup"), "dedup")

    root = skill_root_from_config(config_file)
    projects_root = Path(require_text(workspace.get("projects_root"), "workspace.projects_root")).resolve()
    if not projects_root.is_dir():
        raise DispatcherError("invalid_config", f"projects_root 不存在：{projects_root}")

    projects_data = require_mapping(workspace.get("projects"), "workspace.projects")
    projects = MappingProxyType({
        require_text(name, "workspace.projects 键"): Project(
            path=require_text(require_mapping(project, f"workspace.projects.{name}").get("path"), f"workspace.projects.{name}.path"),
            base_branches=require_branch_mapping(
                require_mapping(project, f"workspace.projects.{name}").get("base_branches"),
                f"workspace.projects.{name}.base_branches",
            ),
            description=require_optional_text(
                require_mapping(project, f"workspace.projects.{name}").get("description"),
                f"workspace.projects.{name}.description",
            ),
            tenants=require_project_tenants(
                require_mapping(project, f"workspace.projects.{name}").get("tenants"),
                f"workspace.projects.{name}.tenants",
            ),
            branch_priority=require_branch_priority(
                require_mapping(project, f"workspace.projects.{name}").get("branch_priority"),
                f"workspace.projects.{name}.branch_priority",
            ),
        )
        for name, project in projects_data.items()
    })

    if not projects:
        raise DispatcherError("invalid_config", "workspace.projects 至少需要一个项目")
    task_source_type = require_text(task_source.get("type"), "task_source.type")
    task_source_query = require_text(task_source.get("query"), "task_source.query")
    fetch_prompt = require_text(task_source.get("fetch_prompt"), "task_source.fetch_prompt")
    reference_plan_field = task_source.get("reference_plan_field")
    if reference_plan_field is not None and (not isinstance(reference_plan_field, str) or not reference_plan_field.strip()):
        raise DispatcherError("invalid_config", "task_source.reference_plan_field 必须是字符串或 null")
    task_source_flows = task_source.get("flows", {})
    if not isinstance(task_source_flows, Mapping):
        raise DispatcherError("invalid_config", "task_source.flows 必须是对象")
    session_prompt = require_mapping(task_source.get("session_prompt"), "task_source.session_prompt")
    recovery_session_prompt = require_text(session_prompt.get("recovery"), "task_source.session_prompt.recovery")
    require_text(interaction.get("repository_selection"), "interaction.repository_selection")
    require_text(interaction.get("base_branch_selection"), "interaction.base_branch_selection")
    require_integer(interaction.get("ask_batch_size"), "interaction.ask_batch_size", 1, 4)
    if require_text(layout.get("group_by"), "dispatch.layout.group_by") != "repository":
        raise DispatcherError("invalid_config", "dispatch.layout.group_by 必须为 repository")
    layout_mode = require_text(layout.get("mode"), "dispatch.layout.mode")
    if layout_mode not in {"split", "separate"}:
        raise DispatcherError("invalid_config", "dispatch.layout.mode 必须为 split 或 separate")
    if not require_bool(dedup.get("enabled"), "dedup.enabled"):
        raise DispatcherError("invalid_config", "dedup.enabled 必须为 true")

    task_url_template = require_text(task_source.get("task_url_template"), "task_source.task_url_template")
    task_url_for(task_url_template, "template-check")
    command_templates = require_mapping(skill.get("command_templates"), "dispatch.skill.command_templates")
    template_values = MappingProxyType({
        mode: require_text(command_templates.get(mode), f"dispatch.skill.command_templates.{mode}")
        for mode in ("separate", "split")
    })
    direct_template = require_text(
        command_templates.get("direct", "/dev-spec-gen {task_url}"),
        "dispatch.skill.command_templates.direct",
    )
    proposal_template = require_text(
        command_templates.get("proposal", "/dev-spec-gen 出具开发方案 {task_url} {requirement_snapshot_path}"),
        "dispatch.skill.command_templates.proposal",
    )
    for template in (*template_values.values(), direct_template, proposal_template):
        validate_command_template(template)

    shell_commands = require_mapping(terminal.get("shell_commands"), "dispatch.terminal.shell_commands")
    shell_platform = "windows" if os.name == "nt" else "posix"
    shell_command = require_text(shell_commands.get(shell_platform), f"dispatch.terminal.shell_commands.{shell_platform}")
    agent_extra_args = dispatch.get("agent_extra_args")
    if agent_extra_args is None:
        agent_extra_args = ""
    if not isinstance(agent_extra_args, str):
        raise DispatcherError("invalid_config", "dispatch.agent_extra_args 必须是字符串")
    agent_commands = dispatch.get("agent_commands")
    if agent_commands is None:
        agent_command = require_text(dispatch.get("agent"), "dispatch.agent")
    else:
        agent_commands = require_mapping(agent_commands, "dispatch.agent_commands")
        if os.name == "nt":
            agent_key = "windows_pwsh" if shutil.which("pwsh.exe") else "windows_powershell"
        elif sys.platform == "darwin":
            agent_key = "macos"
        else:
            agent_key = "linux"
        agent_command = require_text(agent_commands.get(agent_key), f"dispatch.agent_commands.{agent_key}")

    return Config(
        root=root,
        projects_root=projects_root,
        projects=projects,
        branch_options=require_branch_mapping(base_branch.get("options", []), "base_branch.options"),
        validate_branch=require_bool(base_branch.get("validate"), "base_branch.validate"),
        max_tasks=require_integer(task_source.get("max_tasks"), "task_source.max_tasks", 1, 12),
        max_agents=require_integer(concurrency.get("max_agents"), "dispatch.concurrency.max_agents", 1, 12),
        max_panes=require_integer(layout.get("max_panes_per_tab"), "dispatch.layout.max_panes_per_tab", 1, 4),
        ready_timeout_ms=require_integer(terminal.get("ready_timeout_ms"), "dispatch.terminal.ready_timeout_ms", 1_000, READY_TIMEOUT_MS_MAX),
        read_retry_attempts=require_integer(terminal.get("read_retry_attempts"), "dispatch.terminal.read_retry_attempts", 1, 3),
        read_retry_delay_ms=require_integer(terminal.get("read_retry_delay_ms"), "dispatch.terminal.read_retry_delay_ms", 0, 5_000),
        ready_retry_attempts=require_integer(terminal.get("ready_retry_attempts", 1), "dispatch.terminal.ready_retry_attempts", 0, 3),
        send_retry_attempts=require_integer(terminal.get("send_retry_attempts", 1), "dispatch.terminal.send_retry_attempts", 0, 3),
        shell_command=shell_command,
        agent_extra_args=agent_extra_args,
        state_file=relative_to_root(root, dedup.get("state_file"), "dedup.state_file"),
        task_url_template=task_url_template,
        task_source_type=task_source_type,
        task_source_query=task_source_query,
        fetch_prompt=fetch_prompt,
        reference_plan_field=reference_plan_field,
        task_source_flows=MappingProxyType({
            require_text(name, "task_source.flows 键"): require_mapping(value, f"task_source.flows.{name}")
            for name, value in task_source_flows.items()
        }),
        agent_command=agent_command,
        command_templates=MappingProxyType({**template_values, "direct": direct_template, "proposal": proposal_template}),
        layout_mode=layout_mode,
        session_prompt=require_text(session_prompt.get(layout_mode), f"task_source.session_prompt.{layout_mode}"),
        recovery_session_prompt=recovery_session_prompt,
    )


def task_url_for(template: str, task_id: str) -> str:
    if template.count("{task_id}") != 1 or re.search(r"\{[^}]*\}", template.replace("{task_id}", "")):
        raise DispatcherError("invalid_config", "task_url_template 必须且只能包含一个 {task_id}")
    task_url = template.replace("{task_id}", quote(task_id, safe=""))
    parsed_url = urlparse(task_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc or any(char.isspace() for char in task_url):
        raise DispatcherError("invalid_config", "task_url_template 必须生成无空白字符的 HTTPS 地址")
    return task_url


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def repository_from_path(
    name: str,
    path: Path,
    projects_root: Path,
    description: str | None = None,
    allow_outside_projects_root: bool = False,
) -> Repository:
    resolved = path.resolve()
    if not allow_outside_projects_root and not is_within(resolved, projects_root):
        raise DispatcherError("invalid_repository", f"仓库路径越出 projects_root：{name}")
    if not resolved.is_dir() or not (resolved / ".git").is_dir():
        raise DispatcherError("invalid_repository", f"不是可用 Git 仓库：{resolved}")
    return Repository(name=name, path=resolved, description=description)


def configured_project_path(config: Config, project: Project) -> tuple[Path, bool]:
    path = Path(project.path)
    if path.is_absolute():
        return path, True
    if ".." in path.parts:
        raise DispatcherError("invalid_config", "项目路径不能越出 projects_root")
    return config.projects_root / path, False


def configured_repository(config: Config, name: str) -> Repository | None:
    project = config.projects.get(name)
    if project is None:
        return None
    path, allow_outside = configured_project_path(config, project)
    return repository_from_path(name, path, config.projects_root, project.description, allow_outside)


def repository_for_name(config: Config, name: str) -> Repository:
    repository = configured_repository(config, name)
    if repository is not None:
        return repository
    candidates = recursive_repositories(config, name)
    if not candidates:
        raise DispatcherError("repository_not_found", f"未找到候选仓库：{name}")
    if len(candidates) != 1:
        raise DispatcherError("repository_ambiguous", f"项目名匹配多个候选仓库：{name}")
    return candidates[0]


def recursive_repositories(config: Config, name: str) -> tuple[Repository, ...]:
    excluded = {
        ".git", "node_modules", ".venv", "target", "build", "dist", "bin", "obj", "out",
        ".gradle", ".next", ".nuxt", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
        "htmlcov", "worktrees",
    }
    needle = name.casefold()
    matches: list[Repository] = []
    for current, directories, _ in os.walk(config.projects_root):
        relative = Path(current).relative_to(config.projects_root)
        depth = len(relative.parts)
        directories[:] = [] if depth >= 10 else [directory for directory in directories if directory not in excluded]
        candidate = Path(current)
        git_path = candidate / ".git"
        if needle not in candidate.name.casefold() or not git_path.is_dir():
            continue
        resolved = candidate.resolve()
        if is_within(resolved, config.projects_root):
            matches.append(Repository(name=candidate.name, path=resolved))
    return tuple(sorted(matches, key=lambda repository: repository.path.as_posix().casefold()))


def discover_repositories(config: Config) -> tuple[Repository, ...]:
    found: list[Repository] = []

    for name, project in config.projects.items():
        path, allow_outside = configured_project_path(config, project)
        repository = repository_from_path(name, path, config.projects_root, project.description, allow_outside)
        found.append(repository)

    return tuple(found)


def repositories_by_name(config: Config) -> dict[str, Repository]:
    return {repository.name: repository for repository in discover_repositories(config)}


def repositories_for_assignments(config: Config, assignments: Iterable[Assignment]) -> dict[str, Repository]:
    repositories = repositories_by_name(config)
    for assignment in assignments:
        if assignment.repository in repositories:
            continue
        candidates = recursive_repositories(config, assignment.repository)
        matching = [
            candidate
            for candidate in candidates
            if candidate.path.resolve() == assignment.repository_path.resolve()
        ]
        if len(matching) != 1:
            raise DispatcherError("repository_not_found", f"未找到候选仓库：{assignment.repository}")
        repositories[assignment.repository] = matching[0]
    return repositories


def git_common_dir(path: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return Path(completed.stdout.strip()).resolve()


def is_linked_worktree(repository: Repository, worktree: Path) -> bool:
    common_dir = git_common_dir(repository.path)
    if common_dir is None or common_dir != git_common_dir(worktree):
        return False
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository.path), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return False
    if completed.returncode != 0:
        return False
    return any(
        line.startswith("worktree ") and Path(line.removeprefix("worktree ")).resolve() == worktree.resolve()
        for line in completed.stdout.splitlines()
    )


def branch_exists(repository: Repository, branch: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repository.path), "rev-parse", "--verify", f"{branch}^{{commit}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode == 0


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1_048_576), b""):
                digest.update(block)
    except OSError as error:
        raise DispatcherError("invalid_input", f"无法读取文件内容：{path.name}") from error
    return digest.hexdigest()


def validate_requirement_snapshot_path(config: Config, assignment: Assignment) -> None:
    path = assignment.requirement_snapshot_path
    if path is None:
        raise DispatcherError("invalid_input", "requirement_snapshot_path 是必填项")
    if assignment.worktree_path is None:
        raise DispatcherError("invalid_input", "requirement_snapshot_path 需要任务 worktree")
    if path.is_symlink():
        raise DispatcherError("invalid_input", "requirement_snapshot_path 必须是现有普通文件")
    resolved = path.resolve()
    specs_root = (assignment.worktree_path.resolve() / "docs" / "engineering" / "specs").resolve()
    attachments_root = (assignment.worktree_path.resolve() / "docs" / "engineering" / "attachments" / assignment.task.task_id).resolve()
    if not resolved.is_file() or not is_within(resolved, specs_root):
        raise DispatcherError("invalid_input", "requirement_snapshot_path 必须位于任务 worktree specs 目录")
    if not resolved.name.endswith("-raw-requirements.md"):
        raise DispatcherError("invalid_input", "requirement_snapshot_path 必须是原始需求 Markdown")
    try:
        content = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DispatcherError("invalid_input", "无法读取 requirement_snapshot_path") from error
    if not content.startswith("---\n"):
        raise DispatcherError("invalid_input", "requirement_snapshot_path 缺少归档元数据")
    frontmatter, separator, _ = content[4:].partition("\n---\n")
    if not separator:
        raise DispatcherError("invalid_input", "requirement_snapshot_path 归档元数据格式不合法")
    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError as error:
        raise DispatcherError("invalid_input", "requirement_snapshot_path 归档元数据格式不合法") from error
    if not isinstance(metadata, Mapping):
        raise DispatcherError("invalid_input", "requirement_snapshot_path 归档元数据必须是对象")
    if metadata.get("task_id") != assignment.task.task_id or metadata.get("snapshot_status") != "complete":
        raise DispatcherError("invalid_input", "requirement_snapshot_path 未标记为完整")
    manifest_value = metadata.get("attachment_manifest")
    if not isinstance(manifest_value, str) or not manifest_value.strip():
        raise DispatcherError("invalid_input", "requirement_snapshot_path 缺少附件清单")
    manifest_candidate = resolved.parent / manifest_value
    if manifest_candidate.is_symlink():
        raise DispatcherError("invalid_input", "附件清单必须是现有普通文件")
    manifest_path = manifest_candidate.resolve()
    if not is_within(manifest_path, attachments_root) or not manifest_path.is_file():
        raise DispatcherError("invalid_input", "附件清单必须位于任务附件目录")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DispatcherError("invalid_input", "附件清单不可读取") from error
    if not isinstance(manifest, Mapping):
        raise DispatcherError("invalid_input", "附件清单必须是对象")
    if manifest.get("task_id") != assignment.task.task_id or manifest.get("status") != "complete":
        raise DispatcherError("invalid_input", "附件清单未标记为完整")
    attachments = manifest.get("attachments")
    if not isinstance(attachments, list):
        raise DispatcherError("invalid_input", "附件清单 attachments 必须是列表")
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            raise DispatcherError("invalid_input", "附件清单项目必须是对象")
        relative_path = attachment.get("path")
        expected_sha256 = attachment.get("sha256")
        expected_size = attachment.get("size")
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
        ):
            raise DispatcherError("invalid_input", "附件路径不合法")
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise DispatcherError("invalid_input", "附件 SHA-256 不合法")
        if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
            raise DispatcherError("invalid_input", "附件大小不合法")
        attachment_candidate = attachments_root / relative_path
        if attachment_candidate.is_symlink():
            raise DispatcherError("invalid_input", "附件文件必须是现有普通文件")
        attachment_path = attachment_candidate.resolve()
        if not attachment_path.is_file() or not is_within(attachment_path, attachments_root):
            raise DispatcherError("invalid_input", "附件文件不存在或越出归档目录")
        if attachment_path.stat().st_size != expected_size or file_sha256(attachment_path) != expected_sha256:
            raise DispatcherError("invalid_input", "附件完整性校验失败")


def validate_gitnexus_report_path(config: Config, assignment: Assignment) -> None:
    if assignment.gitnexus_report_path is None:
        return
    if assignment.worktree_path is None:
        raise DispatcherError("invalid_input", "gitnexus_report_path 需要任务 worktree")
    report_path = assignment.gitnexus_report_path.resolve()
    report_root = (assignment.worktree_path.resolve() / "docs" / "engineering" / "research").resolve()
    if not is_within(report_path, report_root) or not report_path.is_file():
        raise DispatcherError("invalid_input", "gitnexus_report_path 必须是任务 worktree research 目录中的现有文件")


def validate_assignment(config: Config, assignment: Assignment, repositories: Mapping[str, Repository]) -> None:
    validate_assignment_path(assignment, repositories)
    repository = repositories[assignment.repository]
    project = config.projects.get(assignment.repository)
    if assignment.tenant_slug != "legacy":
        if project is None or project.tenants.get(assignment.tenant) != assignment.tenant_slug:
            raise DispatcherError("invalid_input", f"租户不属于项目配置：{assignment.repository}/{assignment.tenant}")
    expected_task_url = task_url_for(config.task_url_template, assignment.task.task_id)
    if assignment.task.task_url != expected_task_url:
        raise DispatcherError("invalid_input", "task_url 必须由 task_url_template 生成")
    if assignment.worktree_path is not None:
        worktree = assignment.worktree_path.resolve()
        if (
            not is_within(worktree, config.projects_root)
            or not worktree.is_dir()
            or not (worktree / ".git").exists()
            or not is_linked_worktree(repository, worktree)
        ):
            raise DispatcherError("invalid_input", "worktree_path 必须是源仓库的可用 Git worktree")
        if worktree == repository.path:
            raise DispatcherError("invalid_input", "worktree_path 不能等于源仓库路径")
    elif config.layout_mode == "separate" or assignment.tenant_slug != "legacy":
        raise DispatcherError("invalid_input", "独立租户 assignment 要求主 Agent 提供 worktree_path")
    validate_gitnexus_report_path(config, assignment)
    if assignment.requirement_snapshot_path is not None:
        validate_requirement_snapshot_path(config, assignment)
    elif assignment.tenant_slug != "legacy":
        raise DispatcherError("invalid_input", "requirement_snapshot_path 是必填项")
    if assignment.base_branch is None:
        return
    if assignment.base_branch not in config.branches_for(repository.name):
        raise DispatcherError("invalid_branch", f"{assignment.base_branch} 不在 {repository.name} 的配置白名单中")
    if config.validate_branch and not branch_exists(repository, assignment.base_branch):
        raise DispatcherError("branch_not_found", f"{assignment.base_branch} 在 {repository.name} 中不存在")


def chunked(values: tuple[Assignment, ...], size: int) -> Iterable[tuple[Assignment, ...]]:
    iterator = iter(values)
    while chunk := tuple(islice(iterator, size)):
        yield chunk


def terminal_repositories(plans: Iterable[TerminalPlan]) -> tuple[Repository, ...]:
    repositories: OrderedDict[Path, Repository] = OrderedDict()
    for plan in plans:
        repositories.setdefault(plan.repository.path.resolve(), plan.repository)
    return tuple(repositories.values())

def build_terminal_plans(
    assignments: Iterable[Assignment],
    repositories: Mapping[str, Repository],
    max_panes: int,
    layout_mode: str = "split",
) -> tuple[TerminalPlan, ...]:
    grouped: OrderedDict[tuple[str, str], list[Assignment]] = OrderedDict()
    for assignment in assignments:
        validate_assignment_path(assignment, repositories)
        grouped.setdefault((assignment.repository, assignment.tenant_slug), []).append(assignment)

    plans: list[TerminalPlan] = []
    for (repository_name, tenant_slug), values in grouped.items():
        chunks = (
            ((assignment,) for assignment in values)
            if layout_mode == "separate" or any(assignment.worktree_path is not None for assignment in values)
            else chunked(tuple(values), max_panes)
        )
        for tab_index, chunk in enumerate(chunks, start=1):
            tab_title = (
                chunk[0].worktree_path.resolve().name
                if chunk[0].worktree_path is not None
                else f"{repository_name}{f'.{tenant_slug}' if tenant_slug != 'legacy' else ''}.tab{tab_index}"
            )
            plans.append(TerminalPlan(
                repository=repositories[repository_name],
                assignments=chunk,
                tab_title=tab_title,
                layout_mode=layout_mode,
            ))
    return tuple(plans)


def validate_assignment_path(assignment: Assignment, repositories: Mapping[str, Repository]) -> None:
    repository = repositories.get(assignment.repository)
    if repository is None:
        raise DispatcherError("repository_not_found", f"未找到候选仓库：{assignment.repository}")
    if assignment.repository_path.resolve() != repository.path.resolve():
        raise DispatcherError("invalid_assignment", f"任务 {assignment.task.task_id} 的仓库路径不匹配")


def read_json_object(path: Path, missing: dict[str, object]) -> dict[str, object]:
    if not path.exists():
        return missing
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DispatcherError("state_unreadable", f"无法读取 JSON 文件：{path}") from error
    if not isinstance(value, dict):
        raise DispatcherError("state_unreadable", f"JSON 根节点必须是对象：{path}")
    return value


def process_is_running(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if getattr(error, "winerror", None) == 87:
            return False
        raise
    return True


def atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        raise DispatcherError("state_unwritable", f"无法原子写入 JSON 文件：{path}") from error


class StateStore:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file

    @property
    def runtime_dir(self) -> Path:
        return self.state_file.parent

    @property
    def current_run_file(self) -> Path:
        return self.runtime_dir / "current-run.json"

    @property
    def history_file(self) -> Path:
        return self.runtime_dir / "history.jsonl"

    @property
    def lock_file(self) -> Path:
        return self.runtime_dir / "launch.lock"

    def snapshot(self) -> dict[str, object]:
        state = read_json_object(self.state_file, {"version": 1, "tasks": {}})
        if state.get("version") != 1 or not isinstance(state.get("tasks"), dict):
            raise DispatcherError("state_unreadable", "state.json 格式不受支持")
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        if any(
            not isinstance(task, Mapping) or task.get("status") not in TASK_STATUSES
            for task in tasks.values()
        ):
            raise DispatcherError("state_unreadable", "state.json 任务记录格式不受支持")
        return state

    def status(self, task_id: str, tenant_slug: str = "legacy") -> str | None:
        tasks = self.snapshot()["tasks"]
        assert isinstance(tasks, dict)
        key = task_id if tenant_slug == "legacy" else f"{task_id}::{tenant_slug}"
        value = tasks.get(key)
        return value.get("status") if isinstance(value, dict) and isinstance(value.get("status"), str) else None

    def mark_launching(self, assignment: Assignment, plan: TerminalPlan, pane_index: int) -> None:
        state = self.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        state_key = assignment.assignment_id
        next_tasks = {
            **tasks,
            state_key: {
                "repository": assignment.repository,
                "repository_path": (
                    assignment.worktree_path.resolve().as_posix()
                    if plan.layout_mode == "separate" and assignment.worktree_path is not None
                    else plan.repository.path.as_posix()
                ),
                "worktree_path": assignment.worktree_path.resolve().as_posix() if assignment.worktree_path else None,
                "source_repository_path": assignment.repository_path.resolve().as_posix(),
                "base_branch": assignment.base_branch,
                "tenant": assignment.tenant,
                "tenant_slug": assignment.tenant_slug,
                "assignment_id": assignment.assignment_id,
                "task_url": assignment.task.task_url,
                "title": assignment.task.title,
                "description": assignment.task.description,
                "reference_plan": assignment.reference_plan,
                "assignee": assignment.assignee,
                "source_task_id": assignment.source_task_id,
                "source_assignee": assignment.source_assignee,
                "parent_task_id": assignment.parent_task_id,
                "parent_assignee": assignment.parent_assignee,
                "gitnexus_report_path": assignment.gitnexus_report_path.as_posix() if assignment.gitnexus_report_path else None,
                "requirement_snapshot_path": assignment.requirement_snapshot_path.as_posix() if assignment.requirement_snapshot_path else None,
                "dispatch_flow": assignment.dispatch_flow,
                "layout": plan.layout_mode,
                "tab_title": plan.tab_title,
                "pane_index": pane_index,
                "status": "launching",
                "dispatch_state": "pending",
                "updated_at": utc_now(),
            },
        }
        atomic_write_json(self.state_file, {"version": 1, "tasks": next_tasks})

    def mark_dispatched(self, record: TerminalRecord) -> None:
        state = self.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        existing = tasks.get(record.assignment.assignment_id)
        if not isinstance(existing, Mapping):
            raise DispatcherError("state_unreadable", "任务启动状态缺失")
        snapshot = record.snapshot
        next_tasks = {
            **tasks,
            record.assignment.assignment_id: {
                **existing,
                "status": "dispatched",
                "terminal_handle": record.handle,
                "worktree_path": snapshot.worktree_path.as_posix(),
                "tab_id": snapshot.tab_id,
                "leaf_id": snapshot.leaf_id,
                "terminal_title": snapshot.title,
                "dispatched_at": utc_now(),
                "updated_at": utc_now(),
                "recovery_history": [],
            },
        }
        atomic_write_json(self.state_file, {"version": 1, "tasks": next_tasks})

    def mark_recovered(self, task_id: str, snapshot: TerminalSnapshot, result: str) -> None:
        state = self.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        existing = tasks.get(task_id)
        if not isinstance(existing, Mapping):
            raise DispatcherError("state_unreadable", "待恢复任务状态缺失")
        previous_history = existing.get("recovery_history")
        history = list(previous_history) if isinstance(previous_history, list) else []
        event = {"at": utc_now(), "result": result, "terminal_handle": snapshot.handle}
        next_tasks = {
            **tasks,
            task_id: {
                **existing,
                "status": "dispatched",
                "terminal_handle": snapshot.handle,
                "worktree_path": snapshot.worktree_path.as_posix(),
                "tab_id": snapshot.tab_id,
                "leaf_id": snapshot.leaf_id,
                "terminal_title": snapshot.title,
                "updated_at": event["at"],
                "recovery_history": [*history, event],
            },
        }
        atomic_write_json(self.state_file, {"version": 1, "tasks": next_tasks})

    def mark_requires_manual_reset(self, assignment_id: str, reason: str, tenant_slug: str = "legacy") -> None:
        state = self.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        state_key = assignment_id if "::" in assignment_id or tenant_slug == "legacy" else f"{assignment_id}::{tenant_slug}"
        existing = tasks.get(state_key)
        if not isinstance(existing, Mapping):
            raise DispatcherError("state_unreadable", "待恢复任务状态缺失")
        previous_history = existing.get("recovery_history")
        history = list(previous_history) if isinstance(previous_history, list) else []
        event = {"at": utc_now(), "result": "requires_manual_reset", "reason": reason}
        next_tasks = {
            **tasks,
            state_key: {
                **existing,
                "status": "requires_manual_reset",
                "updated_at": event["at"],
                "recovery_history": [*history, event],
            },
        }
        atomic_write_json(self.state_file, {"version": 1, "tasks": next_tasks})

    def reset(self, task_id: str, force_unlock: bool, force: bool = False, tenant_slug: str | None = None) -> bool:
        with self.launch_lock(force_unlock):
            state = self.snapshot()
            tasks = state["tasks"]
            assert isinstance(tasks, dict)
            state_key = task_id if tenant_slug is None else f"{task_id}::{require_tenant_slug(tenant_slug)}"
            if state_key not in tasks:
                return False
            task = tasks[state_key]
            if not isinstance(task, Mapping):
                raise DispatcherError("reset_not_allowed", "任务状态格式不合法")
            status = task.get("status")
            if status in {"launching", "requires_manual_reset"}:
                pass
            elif status == "dispatched" and force:
                pass
            else:
                raise DispatcherError(
                    "reset_not_allowed",
                    "仅允许复位 launching 或 requires_manual_reset 状态；复位 dispatched 状态需使用 --force",
                )
            next_tasks = {key: value for key, value in tasks.items() if key != state_key}
            atomic_write_json(self.state_file, {"version": 1, "tasks": next_tasks})
            return True

    def write_current_run(self, value: Mapping[str, object]) -> None:
        atomic_write_json(self.current_run_file, value)

    def append_history(self, value: Mapping[str, object]) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self.history_file.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps({"time": utc_now(), **value}, ensure_ascii=False) + "\n")
        except OSError as error:
            raise DispatcherError("history_unwritable", f"无法写入历史记录：{self.history_file}") from error

    @contextmanager
    def launch_lock(self, force_unlock: bool) -> Iterator[None]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        if self.lock_file.exists():
            if not force_unlock:
                raise DispatcherError("launch_locked", "已有 Dispatcher 正在运行或锁未清理；确认后使用 --force-unlock")
            lock = read_json_object(self.lock_file, {})
            if process_is_running(lock.get("pid")):
                raise DispatcherError("launch_locked", "已有 Dispatcher 正在运行，不能强制解锁")
            try:
                self.lock_file.unlink()
            except OSError as error:
                raise DispatcherError("lock_unwritable", "无法清理遗留运行锁") from error
        try:
            with self.lock_file.open("x", encoding="utf-8") as file:
                file.write(json.dumps({"pid": os.getpid(), "token": token, "started_at": utc_now()}, ensure_ascii=False))
        except FileExistsError as error:
            raise DispatcherError("launch_locked", "已有 Dispatcher 正在运行") from error
        try:
            yield
        finally:
            try:
                if self.lock_file.exists():
                    lock = read_json_object(self.lock_file, {})
                    if lock.get("token") == token:
                        self.lock_file.unlink()
            except OSError:
                pass


def orca_error_message(error: Any) -> str:
    """将结构化错误转为可安全返回给调用方的摘要。"""
    if isinstance(error, Mapping):
        code = error.get("code")
        message = error.get("message")
        if isinstance(code, str) and isinstance(message, str):
            return f"{code}: {message}"
        if isinstance(message, str):
            return message
    if isinstance(error, str) and error.strip():
        return error
    return "Orca CLI 调用失败"


class OrcaClient:
    def __init__(self, executable: str = "orca") -> None:
        self.executable = executable

    def _call(
        self,
        *arguments: str,
        allow_nonzero: bool = False,
        timeout_seconds: float = ORCA_COMMAND_TIMEOUT_SECONDS,
    ) -> Mapping[str, Any]:
        child_env = os.environ.copy()
        child_env.update({
            "MSYS_NO_PATHCONV": "1",
            "MSYS2_ARG_CONV_EXCL": "*",
        })
        try:
            completed = subprocess.run(
                [self.executable, *arguments, "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout_seconds,
                env=child_env,
            )
        except subprocess.TimeoutExpired as error:
            raise DispatcherError("orca_timeout", f"Orca CLI 调用超时：{' '.join(arguments)}") from error
        except OSError as error:
            raise DispatcherError("orca_unavailable", f"无法调用 Orca CLI：{error}") from error
        stdout = completed.stdout.strip()
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise DispatcherError("orca_invalid_json", f"Orca CLI 未返回 JSON：{stdout[:200]}") from error
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            error = payload.get("error") if isinstance(payload, Mapping) else None
            safe_message = orca_error_message(error)
            if completed.stderr.strip():
                error_code = error.get("code") if isinstance(error, Mapping) else "unknown"
                retry_log(f"orca command stderr received code={error_code}")
            raise DispatcherError("orca_command_failed", safe_message)
        if completed.returncode != 0 and not allow_nonzero:
            raise DispatcherError("orca_command_failed", completed.stderr.strip() or "Orca CLI 返回非零退出码")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少对象 result")
        return result

    @staticmethod
    def _handle(result: Mapping[str, Any]) -> str:
        candidates = (result, result.get("terminal"), result.get("createdTerminal"), result.get("split"))
        for candidate in candidates:
            if isinstance(candidate, Mapping) and isinstance(candidate.get("handle"), str) and candidate["handle"]:
                return candidate["handle"]
        raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 terminal handle")

    def status(self) -> None:
        self._call("status")

    def repo_ids(self) -> dict[str, str]:
        result = self._call("repo", "list")
        repositories = result.get("repos")
        if not isinstance(repositories, list):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 repos 列表")
        ids: dict[str, str] = {}
        for repository in repositories:
            if not isinstance(repository, Mapping):
                raise DispatcherError("orca_invalid_json", "Orca CLI repo 项格式不合法")
            path = repository.get("path")
            repository_id = repository.get("id")
            if not isinstance(path, str) or not isinstance(repository_id, str):
                raise DispatcherError("orca_invalid_json", "Orca CLI repo 缺少路径或 ID")
            ids[os.path.normcase(os.path.normpath(str(Path(path).resolve())))] = repository_id
        return ids

    def repo_add(self, repository: Repository) -> None:
        self._call("repo", "add", "--path", repository.path.as_posix())

    def worktree_set_in_progress(self, worktree_path: Path) -> None:
        self._call(
            "worktree",
            "set",
            "--worktree",
            f"path:{worktree_path.resolve().as_posix()}",
            "--workspace-status",
            "in-progress",
        )

    def worktree_resolve(self, path: Path) -> OrcaWorktree:
        result = self._call("worktree", "show", "--worktree", f"path:{path.resolve().as_posix()}")
        worktree = result.get("worktree")
        if not isinstance(worktree, Mapping):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 worktree 对象")
        worktree_path = Path(require_text(worktree.get("path"), "Orca worktree.path")).resolve()
        if worktree_path != path.resolve():
            raise DispatcherError("orca_selector_mismatch", "Orca worktree 路径与目标工作树不一致")
        return OrcaWorktree(
            worktree_id=require_text(worktree.get("id"), "Orca worktree.id"),
            path=worktree_path,
        )

    def worktree_list(self) -> tuple[OrcaWorktree, ...]:
        result = self._call("worktree", "list")
        worktrees = result.get("worktrees")
        if not isinstance(worktrees, list):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 worktrees 列表")
        values: list[OrcaWorktree] = []
        for worktree in worktrees:
            if not isinstance(worktree, Mapping):
                raise DispatcherError("orca_invalid_json", "Orca worktree 项格式不合法")
            values.append(OrcaWorktree(
                worktree_id=require_text(worktree.get("id"), "Orca worktree.id"),
                path=Path(require_text(worktree.get("path"), "Orca worktree.path")).resolve(),
            ))
        return tuple(values)

    def terminal_create(self, worktree_selector: str, title: str, command: str) -> str:
        result = self._call(
            "terminal",
            "create",
            "--worktree",
            worktree_selector,
            "--title",
            title,
            "--command",
            command,
        )
        return self._handle(result)

    def terminal_split(self, handle: str, direction: str, command: str) -> str:
        result = self._call(
            "terminal",
            "split",
            "--terminal",
            handle,
            "--direction",
            direction,
            "--command",
            command,
        )
        return self._handle(result)

    def terminal_rename(self, handle: str, title: str) -> None:
        self._call("terminal", "rename", "--terminal", handle, "--title", title)

    def terminal_show(self, handle: str) -> TerminalSnapshot:
        result = self._call("terminal", "show", "--terminal", handle)
        terminal = result.get("terminal")
        if not isinstance(terminal, Mapping):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 terminal 对象")
        return TerminalSnapshot.from_dict(terminal)

    def terminal_list(self, repository: Repository) -> tuple[TerminalSnapshot, ...]:
        result = self._call(
            "terminal",
            "list",
            "--worktree",
            f"path:{repository.path.as_posix()}",
            "--include-visual-layouts",
        )
        terminals = result.get("terminals")
        if not isinstance(terminals, list):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 terminals 列表")
        snapshots: list[TerminalSnapshot] = []
        for terminal in terminals:
            if not isinstance(terminal, Mapping):
                raise DispatcherError("orca_invalid_json", "Orca terminal 项格式不合法")
            snapshots.append(TerminalSnapshot.from_dict(terminal))
        return tuple(snapshots)

    def terminal_wait(self, handle: str, timeout_ms: int) -> None:
        result = self._call(
            "terminal",
            "wait",
            "--terminal",
            handle,
            "--for",
            "tui-idle",
            "--timeout-ms",
            str(timeout_ms),
            allow_nonzero=True,
            timeout_seconds=max(ORCA_COMMAND_TIMEOUT_SECONDS, timeout_ms / 1000 + 15),
        )
        wait = result.get("wait")
        if not isinstance(wait, Mapping) or wait.get("satisfied") is not True:
            raise DispatcherError("orca_not_ready", f"Claude terminal 未在 {timeout_ms}ms 内就绪：{handle}")

    def terminal_send(self, handle: str, text: str) -> None:
        # 不传 --interrupt：interrupt-style 输入在 Claude Code TUI 中不会以回车提交文本，
        # 曾导致命令只被输入而未触发。普通 --enter 提交即可生效。
        self._call("terminal", "send", "--terminal", handle, "--text", text, "--enter")


def validate_command_template(template: str) -> None:
    if not template.startswith("/dev-spec-gen"):
        raise DispatcherError("invalid_config", "分发命令模板必须以 /dev-spec-gen 开头")
    try:
        fields = {
            name
            for _, name, _, _ in string.Formatter().parse(template)
            if name is not None
        }
        if not fields <= COMMAND_TEMPLATE_ALLOWED_FIELDS:
            unsupported = sorted(fields - COMMAND_TEMPLATE_ALLOWED_FIELDS)[0]
            raise DispatcherError("invalid_config", f"分发命令模板不支持占位符：{unsupported}")
        template.format(**{name: "x" for name in COMMAND_TEMPLATE_ALLOWED_FIELDS})
    except (IndexError, KeyError, ValueError) as error:
        raise DispatcherError("invalid_config", f"分发命令模板格式不合法：{error}") from error


def detect_claude_authorization_prompt(preview: str) -> bool:
    normalized = ANSI_ESCAPE_PATTERN.sub("", preview.replace("\r\n", "\n").replace("\r", "\n"))
    options = [
        (index, match.group(1))
        for index, line in enumerate(normalized.splitlines())
        if (match := CLAUDE_AUTHORIZATION_OPTION_PATTERN.fullmatch(line.strip()))
    ]
    return any(
        first == "No, exit" and second == "Yes, I accept" and second_index - first_index <= 3
        for (first_index, first), (second_index, second) in zip(options, options[1:])
    )


def safe_task_context_value(value: str) -> str:
    return " ".join(value.split()).replace("`", "'")


def task_context_values(assignment: Assignment) -> Mapping[str, str]:
    return {
        "title": safe_task_context_value(assignment.task.title),
        "description": (
            safe_task_context_value(assignment.task.description)
            if assignment.task.description and assignment.requirement_snapshot_path is None
            else ""
        ),
        "assignee": safe_task_context_value(assignment.assignee or ""),
        "tenant": safe_task_context_value(assignment.tenant) if assignment.tenant != "legacy" else "",
        "assignment_id": assignment.assignment_id if assignment.tenant != "legacy" else "",
        "reference_plan": safe_task_context_value(assignment.reference_plan or ""),
        "gitnexus_report_path": assignment.gitnexus_report_path.resolve().as_posix() if assignment.gitnexus_report_path else "",
        "requirement_snapshot_path": assignment.requirement_snapshot_path.resolve().as_posix() if assignment.requirement_snapshot_path else "",
        "dispatch_flow": assignment.dispatch_flow,
        "source_task_id": require_command_argument(assignment.source_task_id or assignment.task.task_id, "source_task_id"),
    }


def command_for(config: Config, assignment: Assignment, layout_mode: str = "split", recovery: bool = False) -> str:
    template_key = "proposal" if assignment.dispatch_flow == "proposal" else "direct" if assignment.dispatch_flow == "direct" else layout_mode
    template = config.command_templates.get(template_key)
    if template is None:
        raise DispatcherError("invalid_config", f"未配置 {template_key} 分发命令模板")
    values = {
        "task_url": assignment.task.task_url,
        "task_id": require_command_argument(assignment.task.task_id, "task_id"),
        "base_branch": require_command_argument(assignment.base_branch, "base_branch") if assignment.base_branch else "",
        **task_context_values(assignment),
    }
    lines: list[str] = []
    try:
        for line in template.splitlines():
            names = {name for _, name, _, _ in string.Formatter().parse(line) if name is not None}
            if not names:
                lines.append(line)
                continue
            line_values = {name: values[name] for name in names}
            if names <= TASK_CONTEXT_TEMPLATE_FIELDS and not any(line_values.values()):
                continue
            lines.append(line.format(**line_values))
    except (IndexError, KeyError, ValueError) as error:
        raise DispatcherError("invalid_config", f"分发命令模板格式不合法：{error}") from error
    command = "\n".join(lines)
    recovery_instruction = config.recovery_session_prompt if recovery else ""
    return "\n\n".join(value for value in (command, recovery_instruction) if value)


def retry_log(message: str) -> None:
    print(f"[dispatcher retry] {message}", file=sys.stderr, flush=True)


def retry_read(config: Config, operation: Callable[[], Any]) -> Any:
    error: DispatcherError | None = None
    for attempt in range(config.read_retry_attempts):
        try:
            return operation()
        except DispatcherError as caught:
            error = caught
            if attempt + 1 < config.read_retry_attempts and config.read_retry_delay_ms:
                retry_log(f"read attempt={attempt + 1}/{config.read_retry_attempts} failed; retrying")
                time.sleep(config.read_retry_delay_ms / 1000)
    assert error is not None
    raise error


def terminal_has_content(orca: OrcaClient, config: Config, handle: str, snapshot: TerminalSnapshot | None = None) -> bool:
    """检测会话内容：preview 非空即认为任务已实际运行。"""
    current = snapshot or retry_read(config, lambda: orca.terminal_show(handle))
    return bool(current.preview.strip())


def accept_claude_authorization(orca: OrcaClient, config: Config, handle: str) -> tuple[bool, TerminalSnapshot]:
    snapshot = retry_read(config, lambda: orca.terminal_show(handle))
    if not detect_claude_authorization_prompt(snapshot.preview):
        return False, snapshot
    if not snapshot.connected or not snapshot.writable or snapshot.agent_identity != "claude":
        raise DispatcherError("claude_authorization_unverified", "无法确认 Claude 授权终端状态")
    retry_log(f"claude_authorization accepting handle={handle}")
    try:
        orca.terminal_send(handle, CLAUDE_AUTHORIZATION_ACCEPT)
    except DispatcherError as error:
        raise DispatcherError("claude_authorization_unverified", "Claude 授权发送失败") from error
    try:
        orca.terminal_wait(handle, config.ready_timeout_ms)
    except DispatcherError as error:
        raise DispatcherError("claude_authorization_timeout", "Claude 授权后未能确认会话就绪") from error
    after = retry_read(config, lambda: orca.terminal_show(handle))
    if (
        not after.connected
        or not after.writable
        or after.agent_identity != "claude"
        or detect_claude_authorization_prompt(after.preview)
        or not after.preview.strip()
    ):
        raise DispatcherError("claude_authorization_unverified", "Claude 授权结果无法确认")
    retry_log(f"claude_authorization accepted handle={handle}")
    return True, after


def retry_ready_wait(
    orca: OrcaClient,
    handle: str,
    config: Config,
    resend_command: str | None = None,
    authorization_attempted: bool = False,
) -> None:
    """就绪等待后检测会话内容确认任务实际运行；未运行按配置重发命令重试，超时逐轮递增，预算耗尽才失败。"""
    for attempt in range(config.ready_retry_attempts + 1):
        timeout_ms = min(config.ready_timeout_ms * (attempt + 1), READY_TIMEOUT_MS_MAX)
        retry_log(f"ready_wait attempt={attempt + 1}/{config.ready_retry_attempts + 1} timeout_ms={timeout_ms} handle={handle}")
        try:
            retry_read(config, lambda: orca.terminal_wait(handle, timeout_ms))
            if authorization_attempted:
                snapshot = retry_read(config, lambda: orca.terminal_show(handle))
                if detect_claude_authorization_prompt(snapshot.preview):
                    raise DispatcherError("claude_authorization_unverified", "Claude 授权结果无法确认")
                authorization_accepted = False
            else:
                authorization_accepted, snapshot = accept_claude_authorization(orca, config, handle)
                authorization_attempted = authorization_attempted or authorization_accepted
        except DispatcherError as error:
            if error.code.startswith("claude_authorization_"):
                retry_log(f"ready_wait authorization failed handle={handle}")
                raise error
            if attempt >= config.ready_retry_attempts:
                retry_log(f"ready_wait exhausted handle={handle}")
                raise error
            retry_log(f"ready_wait failed handle={handle}; retrying")
            if resend_command is not None:
                retry_log(f"ready_wait resend handle={handle}")
                orca.terminal_send(handle, resend_command)
            continue
        if authorization_accepted:
            retry_log(f"ready_wait succeeded authorization handle={handle}")
            return
        if terminal_has_content(orca, config, handle, snapshot):
            retry_log(f"ready_wait succeeded attempt={attempt + 1}/{config.ready_retry_attempts + 1} handle={handle}")
            return
        if attempt >= config.ready_retry_attempts:
            retry_log(f"ready_wait exhausted empty_session handle={handle}")
            raise DispatcherError("orca_not_ready", f"Claude terminal 会话无内容，任务未运行：{handle}")
        retry_log(f"ready_wait empty_session handle={handle}; retrying")
        if resend_command is not None:
            retry_log(f"ready_wait resend handle={handle}")
            orca.terminal_send(handle, resend_command)


def retry_send(orca: OrcaClient, handle: str, config: Config, text: str) -> None:
    """发送命令超时后按配置重发；命令可能已送达，重发次数由配置显式允许。"""
    for attempt in range(config.send_retry_attempts + 1):
        retry_log(f"send attempt={attempt + 1}/{config.send_retry_attempts + 1} handle={handle}")
        try:
            orca.terminal_send(handle, text)
            retry_log(f"send succeeded attempt={attempt + 1}/{config.send_retry_attempts + 1} handle={handle}")
            return
        except DispatcherError as error:
            if attempt >= config.send_retry_attempts:
                retry_log(f"send exhausted handle={handle}")
                raise error
            retry_log(f"send failed handle={handle}; retrying")
            if config.read_retry_delay_ms:
                time.sleep(config.read_retry_delay_ms / 1000)


def bootstrap_agent(orca: OrcaClient, handle: str, config: Config, resume: bool = False) -> None:
    retry_ready_wait(orca, handle, config)
    command = agent_command_for(config, resume=resume)
    retry_send(orca, handle, config, command)
    retry_ready_wait(orca, handle, config, resend_command=command)


def mark_manual_reset_safely(store: StateStore, assignment: Assignment, reason: str) -> str | None:
    try:
        store.mark_requires_manual_reset(assignment.assignment_id, reason)
    except DispatcherError as error:
        return error.message
    return None

def append_history_safely(store: StateStore, value: Mapping[str, object]) -> str | None:
    try:
        store.append_history(value)
    except DispatcherError as error:
        return error.message
    return None


def set_worktree_in_progress_safely(
    orca: OrcaClient,
    store: StateStore,
    task_id: str,
    snapshot: TerminalSnapshot,
) -> str | None:
    try:
        orca.worktree_set_in_progress(snapshot.worktree_path)
    except DispatcherError as error:
        append_history_safely(store, {
            "task": task_id,
            "terminal_handle": snapshot.handle,
            "result": "workspace_status_failed",
            "reason": error.message,
        })
        return error.message
    return None


def split_parent_snapshot(
    tasks: Mapping[str, object],
    snapshots: Iterable[TerminalSnapshot],
    recovered_snapshots: Mapping[str, TerminalSnapshot],
    tab_id: object,
    pane_index: int,
) -> TerminalSnapshot:
    parent_index = 0 if pane_index == 1 else 1
    parents = tuple(
        (stored_task_id, value)
        for stored_task_id, value in tasks.items()
        if isinstance(value, Mapping)
        and value.get("status") == "dispatched"
        and value.get("layout") == "split"
        and value.get("tab_id") == tab_id
        and value.get("pane_index") == parent_index
    )
    if len(parents) != 1:
        raise DispatcherError("recovery_parent_missing", "缺少唯一可用的 split 父 pane")
    parent_task_id, parent_value = parents[0]
    parent = recovered_snapshots.get(parent_task_id)
    if parent is None:
        matches = tuple(snapshot for snapshot in snapshots if snapshot_matches_state(snapshot, parent_value))
        if len(matches) != 1:
            raise DispatcherError("recovery_parent_missing", "缺少唯一可用的 split 父 pane")
        parent = matches[0]
    if not parent.connected or not parent.writable:
        raise DispatcherError("recovery_parent_missing", "split 父 pane 不可写")
    return parent


def agent_command_for(config: Config, resume: bool = False) -> str:
    arguments = " ".join(
        value for value in (config.agent_extra_args.strip(), "--continue" if resume else "") if value
    )
    if "{agent_args}" in config.agent_command:
        return config.agent_command.replace("{agent_args}", f" {arguments}" if arguments else "")
    if not arguments:
        return config.agent_command
    marker = "claude"
    position = config.agent_command.find(marker)
    if position < 0:
        return f"{config.agent_command} {arguments}"
    end = position + len(marker)
    return f"{config.agent_command[:end]} {arguments}{config.agent_command[end:]}"


TERMINAL_HANDLE_TIMEOUT_MARKER = "Timed out waiting for terminal handle"
TERMINAL_CREATE_RETRY_ATTEMPTS = 3


def is_terminal_handle_timeout(error: DispatcherError) -> bool:
    """兼容 Orca 将超时信息嵌套在错误对象中的返回形式。"""
    message = error.message.casefold()
    return (
        TERMINAL_HANDLE_TIMEOUT_MARKER.casefold() in message
        or (
            "terminal" in message
            and "handle" in message
            and ("timed out" in message or "timeout" in message or "超时" in message)
            and "creat" in message
        )
    )


def terminal_creation_match(
    snapshot: TerminalSnapshot,
    repository: Repository,
    title: str,
) -> bool:
    return snapshot.worktree_path.resolve() == repository.path.resolve() and snapshot.title == title


def find_created_terminal(
    config: Config,
    orca: OrcaClient,
    repository: Repository,
    title: str,
) -> str | None:
    snapshots = retry_read(config, lambda: orca.terminal_list(repository))
    matches = tuple(snapshot for snapshot in snapshots if terminal_creation_match(snapshot, repository, title))
    if len(matches) > 1:
        raise DispatcherError("terminal_identity_ambiguous", "无法唯一确认超时后创建的终端")
    return matches[0].handle if matches else None


def create_terminal_with_retry(
    orca: OrcaClient,
    config: Config,
    repository: Repository,
    selector: str,
    title: str,
    command: str,
) -> str:
    """句柄等待超时后先按 worktree 和唯一标题接管，确认不存在才重建。"""
    for attempt in range(TERMINAL_CREATE_RETRY_ATTEMPTS + 1):
        retry_log(f"terminal_create attempt={attempt + 1}/{TERMINAL_CREATE_RETRY_ATTEMPTS + 1} title={title}")
        try:
            handle = orca.terminal_create(selector, title, command)
            retry_log(f"terminal_create succeeded attempt={attempt + 1}/{TERMINAL_CREATE_RETRY_ATTEMPTS + 1} handle={handle}")
            return handle
        except DispatcherError as error:
            if not is_terminal_handle_timeout(error):
                raise
            retry_log(f"terminal_create handle_timeout title={title}; finding_existing")
            recovered_handle = find_created_terminal(config, orca, repository, title)
            if recovered_handle is not None:
                retry_log(f"terminal_create reclaimed_existing handle={recovered_handle}")
                return recovered_handle
            if attempt >= TERMINAL_CREATE_RETRY_ATTEMPTS:
                retry_log(f"terminal_create exhausted title={title}")
                raise
            retry_log(f"terminal_create no_existing title={title}; rebuilding")
            time.sleep(2.0)


def resolve_assignment_worktree(
    orca: OrcaClient,
    config: Config,
    repository: Repository,
    assignment: Assignment,
    repository_ids: dict[str, str],
) -> OrcaWorktree:
    worktree_path = assignment.worktree_path
    if worktree_path is None:
        raise DispatcherError("invalid_input", "separate 布局要求主 Agent 提供 worktree_path")
    resolved = worktree_path.resolve()
    for worktree in retry_read(config, orca.worktree_list):
        if worktree.path == resolved:
            return worktree
    path_key = os.path.normcase(os.path.normpath(str(resolved)))
    if path_key not in repository_ids:
        orca.repo_add(Repository(repository.name, resolved))
        repository_ids[path_key] = "registered"
    return retry_read(config, lambda: orca.worktree_resolve(resolved))


def validate_terminal_snapshot(snapshot: TerminalSnapshot, worktree: OrcaWorktree) -> None:
    if snapshot.worktree_path.resolve() != worktree.path.resolve():
        raise DispatcherError("orca_selector_mismatch", "Orca terminal 路径与目标工作树不一致")
    if snapshot.worktree_id != worktree.worktree_id:
        raise DispatcherError("orca_selector_mismatch", "Orca terminal 未绑定目标工作树")


def launch(
    config: Config,
    assignments: tuple[Assignment, ...],
    store: StateStore,
    orca: OrcaClient,
    force_unlock: bool,
) -> dict[str, object]:
    task_ids = [assignment.assignment_id for assignment in assignments]
    if len(task_ids) != len(set(task_ids)):
        raise DispatcherError("invalid_input", "同一输入中不能包含重复 task_id 与 tenant 组合")

    repositories = repositories_for_assignments(config, assignments)
    for assignment in assignments:
        validate_assignment(config, assignment, repositories)

    with store.launch_lock(force_unlock):
        dispatched = tuple(assignment for assignment in assignments if store.status(assignment.task.task_id, assignment.tenant_slug) == "dispatched")
        uncertain = tuple(
            assignment
            for assignment in assignments
            if store.status(assignment.task.task_id, assignment.tenant_slug) in {"launching", "requires_manual_reset"}
        )
        eligible = tuple(
            assignment
            for assignment in assignments
            if assignment not in dispatched and assignment not in uncertain
        )
        capacity = min(config.max_tasks, config.max_agents)
        selected = eligible[:capacity]
        ignored = eligible[capacity:]
        results: list[dict[str, object]] = [
            {"task_id": assignment.task.task_id, "tenant": assignment.tenant, "assignment_id": assignment.assignment_id, "status": "skipped_dispatched"} for assignment in dispatched
        ] + [
            {"task_id": assignment.task.task_id, "tenant": assignment.tenant, "assignment_id": assignment.assignment_id, "status": "requires_manual_reset"} for assignment in uncertain
        ]

        if not selected:
            return {
                "results": results,
                "ignored_task_ids": [assignment.task.task_id for assignment in ignored],
                "plans": [],
            }

        retry_read(config, orca.status)
        repository_ids = retry_read(config, orca.repo_ids)
        plans = build_terminal_plans(selected, repositories, config.max_panes, config.layout_mode)
        terminal_repositories_to_register = terminal_repositories(plans)
        registered_repositories = False
        for repository in terminal_repositories_to_register:
            path = os.path.normcase(os.path.normpath(str(repository.path.resolve())))
            if path not in repository_ids:
                orca.repo_add(repository)
                registered_repositories = True
        if registered_repositories:
            repository_ids = retry_read(config, orca.repo_ids)
        missing_repositories = [
            repository
            for repository in terminal_repositories_to_register
            if os.path.normcase(os.path.normpath(str(repository.path.resolve()))) not in repository_ids
        ]
        if missing_repositories:
            missing = "；".join(f"{repository.name}（{repository.path.as_posix()}）" for repository in missing_repositories)
            raise DispatcherError("orca_repository_not_registered", f"Orca 注册后仍未找到目标仓库：{missing}")

        records: list[TerminalRecord] = []
        worktree_cache: dict[str, OrcaWorktree] = {}
        for plan in plans:
            handles: dict[int, str] = {}
            for index, assignment in enumerate(plan.assignments):
                is_split = plan.layout_mode == "split" and index > 0
                parent_handle = handles.get(0 if index == 1 else 1) if is_split else None
                if is_split and parent_handle is None:
                    results.append({
                        "task_id": assignment.task.task_id,
                        "status": "failed_create",
                        "message": "前置 pane 创建失败，未创建 terminal",
                    })
                    append_history_safely(store, {"task": assignment.task.task_id, "result": "failed", "reason": "terminal_precondition"})
                    continue
                try:
                    terminal_worktree: OrcaWorktree | None = None
                    if assignment.worktree_path is not None:
                        terminal_worktree = worktree_cache.get(assignment.assignment_id)
                        if terminal_worktree is None:
                            terminal_worktree = resolve_assignment_worktree(
                                orca,
                                config,
                                plan.repository,
                                assignment,
                                repository_ids,
                            )
                            worktree_cache[assignment.assignment_id] = terminal_worktree
                except DispatcherError as error:
                    results.append({
                        "task_id": assignment.task.task_id,
                        "status": "failed_worktree",
                        "message": error.message,
                    })
                    append_history_safely(store, {"task": assignment.task.task_id, "result": "failed", "reason": error.code})
                    continue
                try:
                    store.mark_launching(assignment, plan, index)
                except DispatcherError as error:
                    results.append({"task_id": assignment.task.task_id, "status": "failed_state", "message": error.message})
                    continue
                try:
                    if not is_split:
                        if terminal_worktree is not None:
                            handle = create_terminal_with_retry(
                                orca,
                                config,
                                Repository(name=plan.repository.name, path=terminal_worktree.path),
                                terminal_worktree.selector,
                                plan.tab_title,
                                agent_command_for(config),
                            )
                        else:
                            handle = orca.terminal_create(
                                f"path:{plan.repository.path.as_posix()}",
                                plan.tab_title,
                                config.shell_command,
                            )
                    elif index == 1:
                        handle = orca.terminal_split(parent_handle, "horizontal", config.shell_command)
                    else:
                        handle = orca.terminal_split(parent_handle, "vertical", config.shell_command)
                    snapshot = retry_read(config, lambda: orca.terminal_show(handle))
                    if terminal_worktree is not None:
                        validate_terminal_snapshot(snapshot, terminal_worktree)
                except DispatcherError as error:
                    results.append({
                        "task_id": assignment.task.task_id,
                        "status": "requires_manual_reset",
                        "message": error.message,
                    })
                    append_history_safely(store, {"task": assignment.task.task_id, "result": "requires_manual_reset", "reason": "terminal_create"})
                    state_error = mark_manual_reset_safely(store, assignment, "terminal_create")
                    if state_error:
                        results[-1]["state_error"] = state_error
                    continue
                handles[index] = handle
                records.append(TerminalRecord(
                    assignment=assignment,
                    handle=handle,
                    tab_title=plan.tab_title,
                    layout_mode=plan.layout_mode,
                    pane_index=index,
                    snapshot=snapshot,
                ))

        ready: list[TerminalRecord] = []
        for record in records:
            try:
                if record.layout_mode == "separate":
                    retry_ready_wait(orca, record.handle, config)
                else:
                    bootstrap_agent(orca, record.handle, config)
                ready.append(record)
            except DispatcherError as error:
                results.append({
                    "task_id": record.assignment.task.task_id,
                    "status": "requires_manual_reset",
                    "message": error.message,
                    "terminal_handle": record.handle,
                })
                append_history_safely(store, {
                    "task": record.assignment.task.task_id,
                    "terminal_handle": record.handle,
                    "result": "requires_manual_reset",
                    "reason": "agent_ready",
                })
                state_error = mark_manual_reset_safely(store, record.assignment, "agent_ready")
                if state_error:
                    results[-1]["state_error"] = state_error

        for record in ready:
            assignment = record.assignment
            try:
                retry_send(orca, record.handle, config, command_for(config, assignment, record.layout_mode))
            except DispatcherError as error:
                results.append({
                    "task_id": assignment.task.task_id,
                    "status": "requires_manual_reset",
                    "message": error.message,
                    "terminal_handle": record.handle,
                })
                append_history_safely(store, {
                    "task": assignment.task.task_id,
                    "terminal_handle": record.handle,
                    "result": "requires_manual_reset",
                    "reason": "terminal_send",
                })
                state_error = mark_manual_reset_safely(store, assignment, "terminal_send")
                if state_error:
                    results[-1]["state_error"] = state_error
                continue
            try:
                store.mark_dispatched(record)
            except DispatcherError as error:
                results.append({
                    "task_id": assignment.task.task_id,
                    "status": "requires_manual_reset",
                    "message": error.message,
                    "terminal_handle": record.handle,
                })
                append_history_safely(store, {
                    "task": assignment.task.task_id,
                    "terminal_handle": record.handle,
                    "result": "uncertain",
                    "reason": "state_write_after_send",
                })
                state_error = mark_manual_reset_safely(store, assignment, "state_write_after_send")
                if state_error:
                    results[-1]["state_error"] = state_error
                continue
            result = {
                "task_id": assignment.task.task_id,
                "status": "dispatched",
                "terminal_handle": record.handle,
            }
            workspace_status_error = set_worktree_in_progress_safely(
                orca,
                store,
                assignment.task.task_id,
                record.snapshot,
            )
            if workspace_status_error:
                result["workspace_status_error"] = workspace_status_error
            results.append(result)
            append_history_safely(store, {
                "task": assignment.task.task_id,
                "repo": assignment.repository,
                "terminal_handle": record.handle,
                "result": "dispatched",
            })

        current_run = {
            "tasks": [assignment.to_dict() for assignment in selected],
            "results": results,
            "updated_at": utc_now(),
        }
        current_run_error: str | None = None
        try:
            store.write_current_run(current_run)
        except DispatcherError as error:
            current_run_error = error.message

    return {
        "results": results,
        "ignored_task_ids": [assignment.task.task_id for assignment in ignored],
        "plans": [
            {
                "repository": plan.repository.to_dict(),
                "task_ids": [assignment.task.task_id for assignment in plan.assignments],
                "tab_title": plan.tab_title,
                "layout": plan.layout_mode,
            }
            for plan in plans
        ],
        **({"current_run_error": current_run_error} if current_run_error else {}),
    }


def recovery_assignment(task_id: str, value: Mapping[str, Any]) -> Assignment:
    required_fields = ("repository", "worktree_path", "task_url", "title", "layout", "tab_title", "pane_index")
    if any(field not in value for field in required_fields):
        raise DispatcherError("recovery_metadata_missing", "任务缺少恢复所需元数据")
    if value.get("status") == "dispatched" and any(field not in value for field in ("tab_id", "leaf_id")):
        raise DispatcherError("recovery_metadata_missing", "已分发任务缺少终端身份元数据")
    repository_path = value.get("source_repository_path", value.get("repository_path"))
    return Assignment.from_dict({
        "task_id": value.get("task_id", task_id.partition("::")[0]),
        "title": value.get("title"),
        "description": value.get("description", ""),
        "task_url": value.get("task_url"),
        "repository": value.get("repository"),
        "repository_path": repository_path,
        "base_branch": value.get("base_branch"),
        "reference_plan": value.get("reference_plan"),
        "assignee": value.get("assignee"),
        "source_task_id": value.get("source_task_id"),
        "source_assignee": value.get("source_assignee"),
        "parent_task_id": value.get("parent_task_id"),
        "parent_assignee": value.get("parent_assignee"),
        "tenant": value.get("tenant", "legacy"),
        "tenant_slug": value.get("tenant_slug", "legacy"),
        "gitnexus_report_path": value.get("gitnexus_report_path"),
        "requirement_snapshot_path": value.get("requirement_snapshot_path"),
        "dispatch_flow": value.get("dispatch_flow", "complete"),
        "worktree_path": value.get("worktree_path") if value.get("layout") == "separate" else None,
    })


def snapshot_matches_state(snapshot: TerminalSnapshot, value: Mapping[str, Any]) -> bool:
    return snapshot.tab_id == value.get("tab_id") and snapshot.leaf_id == value.get("leaf_id")


def is_resumable_shell(snapshot: TerminalSnapshot) -> bool:
    return (
        snapshot.connected
        and snapshot.writable
        and snapshot.agent_identity is None
        and bool(SHELL_PROMPT_PATTERN.search(snapshot.preview.rstrip()))
    )


def recover(
    config: Config,
    store: StateStore,
    orca: OrcaClient,
    task_id: str | None,
    force_unlock: bool,
    tenant_slug: str | None = None,
) -> dict[str, object]:
    with store.launch_lock(force_unlock):
        state = store.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        state_key: str | None = None
        if task_id is not None:
            if tenant_slug is not None:
                state_key = f"{task_id}::{require_tenant_slug(tenant_slug)}"
            elif task_id in tasks:
                state_key = task_id
            elif any(key.startswith(f"{task_id}::") for key in tasks):
                raise DispatcherError(
                    "assignment_ambiguous",
                    f"任务 {task_id} 存在多个租户状态，恢复时必须使用 --tenant-slug",
                )
            else:
                state_key = task_id
        selected = [
            (stored_task_id, value)
            for stored_task_id, value in tasks.items()
            if (state_key is None or stored_task_id == state_key)
            and isinstance(value, Mapping)
            and value.get("status") in {"dispatched", "launching"}
            and (value.get("status") == "dispatched" or value.get("layout") == "separate")
        ]
        if not selected:
            return {"results": []}

        retry_read(config, orca.status)
        repository_ids = retry_read(config, orca.repo_ids)
        results: list[dict[str, object]] = []
        recovered_snapshots: dict[str, TerminalSnapshot] = {}
        for stored_task_id, value in selected:
            try:
                assignment = recovery_assignment(stored_task_id, value)
                layout_mode = require_text(value.get("layout"), "恢复布局")
                if layout_mode not in {"split", "separate"}:
                    raise DispatcherError("recovery_metadata_missing", "任务恢复布局不受支持")
                pane_index = value.get("pane_index")
                if not isinstance(pane_index, int) or isinstance(pane_index, bool) or pane_index < 0:
                    raise DispatcherError("recovery_metadata_missing", "任务 pane_index 不合法")
                source_repository = repository_from_path(
                    assignment.repository,
                    assignment.repository_path,
                    config.projects_root,
                )
                validate_gitnexus_report_path(config, assignment)
                if assignment.requirement_snapshot_path is not None or assignment.tenant_slug != "legacy":
                    validate_requirement_snapshot_path(config, assignment)
                if layout_mode == "separate":
                    worktree_path = assignment.worktree_path
                    if (
                        worktree_path is None
                        or not is_within(worktree_path.resolve(), config.projects_root)
                        or not worktree_path.is_dir()
                        or not (worktree_path / ".git").exists()
                        or not is_linked_worktree(source_repository, worktree_path)
                    ):
                        raise DispatcherError("recovery_metadata_missing", "任务 worktree 元数据不合法")
                    repository = Repository(name=source_repository.name, path=worktree_path.resolve())
                else:
                    repository = source_repository
                repository_key = os.path.normcase(os.path.normpath(str(repository.path.resolve())))
                if repository_key not in repository_ids:
                    orca.repo_add(repository)
                    repository_ids[repository_key] = "registered"
            except DispatcherError as error:
                store.mark_requires_manual_reset(stored_task_id, error.code)
                append_history_safely(store, {"task": stored_task_id, "result": "requires_manual_reset", "reason": error.code})
                results.append({"task_id": stored_task_id, "status": "requires_manual_reset"})
                continue

            try:
                snapshots = retry_read(config, lambda: orca.terminal_list(repository))
            except DispatcherError as error:
                store.mark_requires_manual_reset(stored_task_id, error.code)
                append_history_safely(store, {"task": stored_task_id, "result": "requires_manual_reset", "reason": error.code})
                results.append({"task_id": stored_task_id, "status": "requires_manual_reset"})
                continue

            matches = tuple(snapshot for snapshot in snapshots if snapshot_matches_state(snapshot, value))
            if value.get("status") == "launching" and layout_mode == "separate":
                matches = tuple(
                    snapshot
                    for snapshot in snapshots
                    if terminal_creation_match(snapshot, repository, require_text(value.get("tab_title"), "恢复 tab_title"))
                )
                if len(matches) == 1:
                    snapshot = matches[0]
                    try:
                        if not snapshot.connected or not snapshot.writable:
                            raise DispatcherError("terminal_state_unverified", "无法确认 launching 终端处于可发送任务状态")
                        retry_ready_wait(orca, snapshot.handle, config)
                        retry_send(orca, snapshot.handle, config, command_for(config, assignment, layout_mode, recovery=True))
                        store.mark_recovered(stored_task_id, snapshot, "handle_recovered")
                    except DispatcherError as error:
                        store.mark_requires_manual_reset(stored_task_id, error.code)
                        append_history_safely(store, {"task": stored_task_id, "result": "requires_manual_reset", "reason": error.code})
                        results.append({"task_id": stored_task_id, "status": "requires_manual_reset"})
                        continue
                    result = {"task_id": stored_task_id, "status": "recovered", "terminal_handle": snapshot.handle}
                    workspace_status_error = set_worktree_in_progress_safely(orca, store, stored_task_id, snapshot)
                    if workspace_status_error:
                        result["workspace_status_error"] = workspace_status_error
                    append_history_safely(store, {"task": stored_task_id, "result": "handle_recovered", "terminal_handle": snapshot.handle})
                    results.append(result)
                    continue
            if len(matches) == 1:
                snapshot = matches[0]
                if (
                    snapshot.connected
                    and snapshot.writable
                    and snapshot.agent_identity == "claude"
                ):
                    store.mark_recovered(stored_task_id, snapshot, "native_recovered")
                    recovered_snapshots[stored_task_id] = snapshot
                    result = {"task_id": stored_task_id, "status": "native_recovered", "terminal_handle": snapshot.handle}
                    workspace_status_error = set_worktree_in_progress_safely(orca, store, stored_task_id, snapshot)
                    if workspace_status_error:
                        result["workspace_status_error"] = workspace_status_error
                    append_history_safely(store, {"task": stored_task_id, "result": "native_recovered", "terminal_handle": snapshot.handle})
                    results.append(result)
                    continue
                if is_resumable_shell(snapshot):
                    try:
                        bootstrap_agent(orca, snapshot.handle, config, resume=True)
                        retry_send(orca, snapshot.handle, config, command_for(config, assignment, layout_mode, recovery=True))
                        store.mark_recovered(stored_task_id, snapshot, "recovered")
                    except DispatcherError as error:
                        store.mark_requires_manual_reset(stored_task_id, error.code)
                        append_history_safely(store, {"task": stored_task_id, "result": "requires_manual_reset", "reason": error.code})
                        results.append({"task_id": stored_task_id, "status": "requires_manual_reset"})
                        continue
                    append_history_safely(store, {"task": stored_task_id, "result": "recovered", "terminal_handle": snapshot.handle})
                    recovered_snapshots[stored_task_id] = snapshot
                    result = {"task_id": stored_task_id, "status": "recovered", "terminal_handle": snapshot.handle}
                    workspace_status_error = set_worktree_in_progress_safely(orca, store, stored_task_id, snapshot)
                    if workspace_status_error:
                        result["workspace_status_error"] = workspace_status_error
                    results.append(result)
                    continue

                store.mark_requires_manual_reset(stored_task_id, "terminal_state_unverified")
                append_history_safely(store, {"task": stored_task_id, "result": "requires_manual_reset", "reason": "terminal_state_unverified"})
                results.append({"task_id": stored_task_id, "status": "requires_manual_reset"})
                continue

            if len(matches) > 1:
                store.mark_requires_manual_reset(stored_task_id, "terminal_identity_ambiguous")
                append_history_safely(store, {"task": stored_task_id, "result": "requires_manual_reset", "reason": "terminal_identity_ambiguous"})
                results.append({"task_id": stored_task_id, "status": "requires_manual_reset"})
                continue

            try:
                tab_title = require_text(value.get("tab_title"), "恢复 tab_title")
                if layout_mode == "split" and pane_index > 0:
                    parent = split_parent_snapshot(
                        tasks,
                        snapshots,
                        recovered_snapshots,
                        value.get("tab_id"),
                        pane_index,
                    )
                    direction = "horizontal" if pane_index == 1 else "vertical"
                    handle = orca.terminal_split(parent.handle, direction, config.shell_command)
                elif layout_mode == "separate":
                    handle = create_terminal_with_retry(
                        orca,
                        config,
                        repository,
                        f"path:{repository.path.as_posix()}",
                        tab_title,
                        agent_command_for(config),
                    )
                else:
                    handle = orca.terminal_create(
                        f"path:{repository.path.as_posix()}",
                        tab_title,
                        config.shell_command,
                    )
                snapshot = retry_read(config, lambda: orca.terminal_show(handle))
                if layout_mode == "separate":
                    retry_ready_wait(orca, handle, config)
                else:
                    bootstrap_agent(orca, handle, config, resume=True)
                retry_send(orca, handle, config, command_for(config, assignment, layout_mode, recovery=True))
                store.mark_recovered(stored_task_id, snapshot, "recreated")
            except DispatcherError as error:
                store.mark_requires_manual_reset(stored_task_id, error.code)
                append_history_safely(store, {"task": stored_task_id, "result": "requires_manual_reset", "reason": error.code})
                results.append({"task_id": stored_task_id, "status": "requires_manual_reset"})
                continue
            recovered_snapshots[stored_task_id] = snapshot
            result = {"task_id": stored_task_id, "status": "recovered", "terminal_handle": snapshot.handle}
            workspace_status_error = set_worktree_in_progress_safely(orca, store, stored_task_id, snapshot)
            if workspace_status_error:
                result["workspace_status_error"] = workspace_status_error
            append_history_safely(store, {"task": stored_task_id, "result": "recovered", "terminal_handle": snapshot.handle})
            results.append(result)

    return {"results": results}


def read_assignments(path: Path) -> tuple[Assignment, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DispatcherError("input_unreadable", f"无法读取任务分配文件：{path}") from error
    if not isinstance(raw, Mapping):
        raise DispatcherError("invalid_input", "输入 JSON 根节点必须是对象")
    if "assignments" in raw:
        raise DispatcherError("invalid_input", "输入 JSON 仅支持 tasks 列表")
    unknown_fields = set(raw) - {"tasks"}
    if unknown_fields:
        raise DispatcherError("invalid_input", f"输入 JSON 包含未知顶层字段：{sorted(unknown_fields)[0]}")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        raise DispatcherError("invalid_input", "输入 JSON 必须含 tasks 列表")
    assignments: list[Assignment] = []
    for item in tasks:
        if not isinstance(item, Mapping):
            raise DispatcherError("invalid_input", "tasks 每项必须是对象")
        assignments.append(Assignment.from_dict(item))
    return tuple(assignments)


def validate_decision_values(tasks: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(tasks, list):
        raise DispatcherError("invalid_input", "决策 JSON 必须含 tasks 列表")
    decision_fields = {
        "task_id", "title", "description", "task_url", "assignee", "tenant", "tenant_slug",
        "source_task_id", "source_assignee", "parent_task_id", "parent_assignee", "reference_plan",
        "dispatch_flow", "gitnexus_report_path", "requirement_snapshot_path", "repository", "base_branch", "worktree_path",
    }
    values: list[Mapping[str, Any]] = []
    task_ids: set[tuple[str, str]] = set()
    for item in tasks:
        if not isinstance(item, Mapping):
            raise DispatcherError("invalid_input", "tasks 每项必须是对象")
        unknown_task_fields = set(item) - decision_fields
        if unknown_task_fields:
            raise DispatcherError("invalid_input", f"决策任务包含未知字段：{sorted(unknown_task_fields)[0]}")
        task_id = require_task_id(item.get("task_id"))
        tenant_slug = require_tenant_slug(item.get("tenant_slug", "legacy"))
        tenant = item.get("tenant")
        if tenant_slug == "legacy" and isinstance(tenant, str) and tenant.strip() != "legacy":
            raise DispatcherError("invalid_input", "已指定 tenant 时不能使用保留 tenant_slug：legacy")
        identity = (task_id, tenant_slug)
        if identity in task_ids:
            raise DispatcherError("invalid_input", f"任务 ID 与租户重复：{task_id}/{tenant_slug}")
        task_ids.add(identity)
        values.append(item)
    return tuple(values)


def read_decision_input(path: Path) -> tuple[Mapping[str, Any], ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DispatcherError("input_unreadable", f"无法读取项目决策文件：{path}") from error
    if not isinstance(raw, Mapping):
        raise DispatcherError("invalid_input", "决策 JSON 根节点必须是对象")
    unknown_fields = set(raw) - {"version", "tasks"}
    if unknown_fields:
        raise DispatcherError("invalid_input", f"决策 JSON 包含未知顶层字段：{sorted(unknown_fields)[0]}")
    if raw.get("version") != 1:
        raise DispatcherError("invalid_input", "决策 JSON version 必须为 1")
    tasks = raw.get("tasks")
    return validate_decision_values(tasks)


def preferred_branch_for(config: Config, repository: Repository, tenants: Iterable[str]) -> str | None:
    project = config.projects.get(repository.name)
    if project is None:
        return None
    tenant_set = frozenset(tenants)
    for rule in project.branch_priority:
        when_all = rule["when_all"]
        assert isinstance(when_all, tuple)
        if frozenset(when_all) <= tenant_set:
            branch = rule["branch"]
            assert isinstance(branch, str)
            return branch
    return None

def decision_branch_candidates(config: Config, repository: Repository) -> list[dict[str, object]]:
    return [
        {
            "name": branch,
            "description": description,
            "valid": branch_exists(repository, branch) if config.validate_branch else None,
        }
        for branch, description in config.branch_map_for(repository.name).items()
    ]


def decide_values(config: Config, values: tuple[Mapping[str, Any], ...]) -> dict[str, object]:
    """校验外部调研后的显式路由，不执行语义匹配或任何外部调用。"""
    repositories = repositories_by_name(config)
    tenant_sets: dict[tuple[str, str], set[str]] = {}
    for value in values:
        repository_name = value.get("repository")
        tenant = value.get("tenant")
        if isinstance(repository_name, str) and isinstance(tenant, str):
            key = (require_task_id(value.get("task_id")), repository_name.strip())
            tenant_sets.setdefault(key, set()).add(tenant.strip())
    results: list[dict[str, object]] = []
    selected: list[dict[str, object]] = []
    for value in values:
        task = Task.from_dict(value)
        repository_name = value.get("repository")
        if repository_name is not None and (not isinstance(repository_name, str) or not repository_name.strip()):
            raise DispatcherError("invalid_input", "repository 必须是字符串或省略")
        repository_candidates = (
            [repositories[repository_name.strip()]]
            if isinstance(repository_name, str) and repository_name.strip() in repositories
            else list(repositories.values())
            if repository_name is None
            else []
        )
        if repository_name is None:
            results.append({
                "task_id": task.task_id,
                "status": "needs_confirmation",
                "reason": "缺少外部联合证据确认的项目决策",
                "candidates": {
                    "repositories": [repository.to_dict() for repository in repository_candidates],
                    "base_branches": [],
                },
            })
            continue
        if not repository_candidates:
            results.append({
                "task_id": task.task_id,
                "status": "needs_confirmation",
                "reason": f"未找到配置项目：{repository_name}",
                "candidates": {"repositories": [], "base_branches": []},
            })
            continue
        if len(repository_candidates) != 1:
            results.append({
                "task_id": task.task_id,
                "status": "needs_confirmation",
                "reason": "无法根据外部联合证据唯一确定项目",
                "candidates": {
                    "repositories": [repository.to_dict() for repository in repository_candidates],
                    "base_branches": [],
                },
            })
            continue

        repository = repository_candidates[0]
        tenant = require_optional_text(value.get("tenant"), "tenant") or "legacy"
        tenant_slug = require_tenant_slug(value.get("tenant_slug", "legacy"))
        project_tenants = config.tenants_for(repository.name)
        if tenant_slug != "legacy" and project_tenants.get(tenant) != tenant_slug:
            results.append({
                "task_id": task.task_id,
                "tenant": tenant,
                "tenant_slug": tenant_slug,
                "status": "needs_confirmation",
                "reason": f"租户不属于项目配置：{repository.name}/{tenant}",
                "candidates": {
                    "repositories": [repository.to_dict()],
                    "tenants": [{"name": name, "slug": slug} for name, slug in project_tenants.items()],
                    "base_branches": [],
                },
            })
            continue
        branches = decision_branch_candidates(config, repository)
        preferred_branch = preferred_branch_for(
            config,
            repository,
            tenant_sets.get((task.task_id, repository.name), {tenant}),
        )
        branch_value = value.get("base_branch", preferred_branch)
        if branch_value is not None and (not isinstance(branch_value, str) or not branch_value.strip()):
            raise DispatcherError("invalid_input", "base_branch 必须是字符串或 null")
        branch = branch_value.strip() if isinstance(branch_value, str) else None
        if branch is None:
            results.append({
                "task_id": task.task_id,
                "status": "needs_confirmation",
                "reason": "缺少外部联合证据确认的基础分支决策",
                "candidates": {"repositories": [repository.to_dict()], "base_branches": branches},
            })
            continue
        if branch is not None:
            matching = next((entry for entry in branches if entry["name"] == branch), None)
            if matching is None:
                results.append({
                    "task_id": task.task_id,
                    "status": "needs_confirmation",
                    "reason": f"基础分支不在 {repository.name} 的配置白名单中",
                    "candidates": {"repositories": [repository.to_dict()], "base_branches": branches},
                })
                continue
            if matching["valid"] is False:
                results.append({
                    "task_id": task.task_id,
                    "status": "needs_confirmation",
                    "reason": f"基础分支不存在：{repository.name}/{branch}",
                    "candidates": {"repositories": [repository.to_dict()], "base_branches": branches},
                })
                continue
        elif len(branches) > 1:
            results.append({
                "task_id": task.task_id,
                "status": "needs_confirmation",
                "reason": "无法唯一确定基础分支",
                "candidates": {"repositories": [repository.to_dict()], "base_branches": branches},
            })
            continue
        elif branches:
            branch = branches[0]["name"]
            if branches[0]["valid"] is False:
                results.append({
                    "task_id": task.task_id,
                    "status": "needs_confirmation",
                    "reason": f"基础分支不存在：{repository.name}/{branch}",
                    "candidates": {"repositories": [repository.to_dict()], "base_branches": branches},
                })
                continue

        assignment = Assignment.from_dict({
            **value,
            "repository": repository.name,
            "repository_path": repository.path.as_posix(),
            "base_branch": branch,
        })
        if assignment.tenant_slug != "legacy":
            snapshot_error: str | None = None
            if assignment.worktree_path is None:
                snapshot_error = "独立租户 assignment 要求提供 worktree_path"
            elif assignment.requirement_snapshot_path is None:
                snapshot_error = "requirement_snapshot_path 是必填项"
            else:
                try:
                    validate_requirement_snapshot_path(config, assignment)
                except DispatcherError as error:
                    snapshot_error = error.message
            if snapshot_error:
                results.append({
                    "task_id": task.task_id,
                    "tenant": tenant,
                    "tenant_slug": tenant_slug,
                    "status": "needs_confirmation",
                    "reason": snapshot_error,
                    "candidates": {"repositories": [repository.to_dict()], "base_branches": branches},
                })
                continue
        normalized = assignment.to_dict()
        results.append({"task_id": task.task_id, "status": "selected", "assignment": normalized})
        selected.append(normalized)

    status = "ready" if all(result["status"] == "selected" for result in results) else "needs_confirmation"
    return {
        "version": 1,
        "status": status,
        "tasks": selected if status == "ready" else results,
        "launch_input": {"tasks": selected},
    }


def decide(config: Config, path: Path) -> dict[str, object]:
    return decide_values(config, read_decision_input(path))


def task_source_prompt(
    config: Config,
    query_override: str | None = None,
    flow: str | None = None,
) -> dict[str, object]:
    selected_flow = flow or "complete"
    if selected_flow not in DISPATCH_FLOWS:
        raise DispatcherError("invalid_input", "flow 必须是 direct、complete 或 proposal")
    configured_flow = config.task_source_flows.get(selected_flow)
    if configured_flow is not None and not isinstance(configured_flow.get("next_steps"), list):
        raise DispatcherError("invalid_config", f"task_source.flows.{selected_flow}.next_steps 必须是列表")
    next_steps = configured_flow.get("next_steps") if configured_flow else None
    if not next_steps:
        if selected_flow == "direct":
            next_steps = [
                "根据实际任务编号和标题提炼语义 slug",
                "创建或复用对应 worktree",
                "在该 worktree 启动开发会话",
                "仅将 Jira 地址和 direct 开发指令交给实际任务",
            ]
        elif selected_flow == "proposal":
            next_steps = [
                "执行独立 Jira 节点并归档完整需求与附件",
                "执行独立 GitNexus 调研节点",
                "联合确定项目、租户与基础分支",
                "查找任一基础分支下同名 linked worktree",
                "不存在时调用 dev-spec-gen worktree CLI 创建或复用",
                "生成 version=1 decide 输入",
                "通过 Orca 绑定 worktree 并分发开发方案",
            ]
        else:
            next_steps = ["执行独立 Jira 节点并归档完整需求与附件", "执行独立 GitNexus 调研节点", "创建或复用 worktree", "生成 version=1 decide 输入", "通过 decide 后 launch"]
    if not all(isinstance(step, str) and step.strip() for step in next_steps):
        raise DispatcherError("invalid_config", f"task_source.flows.{selected_flow}.next_steps 必须是非空字符串列表")
    query = config.task_source_query if query_override is None else require_text(query_override, "jql")
    variables = {
        "query": query,
        "reference_plan_field": config.reference_plan_field or "未配置",
    }
    names = set(re.findall(r"\{\{([^{}]+)\}\}", config.fetch_prompt))
    unknown = names - variables.keys()
    if unknown:
        raise DispatcherError("invalid_config", f"task_source.fetch_prompt 包含未知变量：{sorted(unknown)[0]}")
    prompt = config.fetch_prompt
    if selected_flow == "direct":
        prompt = (
            "使用当前环境可用的任务查询工具，仅获取待分发任务的 task_id、title、task_url 和 assignee。\n"
            "查询条件（真实 Jira JQL，必须原样使用）：\n"
            f"{query}\n"
            "不要读取 Jira 详情、父需求、参考方案、原始需求、附件或图片；这些内容由实际开发任务按需处理。"
        )
    else:
        for name, value in variables.items():
            prompt = prompt.replace(f"{{{{{name}}}}}", value)
    return {
        "type": config.task_source_type,
        "fetch_prompt": prompt,
        "session_prompt": config.session_prompt,
        "task_url_template": config.task_url_template,
        "max_tasks": config.max_tasks,
        "reference_plan_field": config.reference_plan_field,
        "query": query,
        "jql_source": "cli" if query_override is not None else "config",
        "flow": selected_flow,
        "dispatch_flow": selected_flow,
        "proposal_command": config.command_templates.get("proposal") if selected_flow == "proposal" else None,
        "jql_semantics": "native_jql_then_parent_post_filter",
        "parent_lookup": {"relation": "parent", "field": config.reference_plan_field, "required": True},
        "post_filter": "child.reference_plan 非空 OR parent.reference_plan 非空",
        "next_steps": list(next_steps),
    }


def config_summary(config: Config) -> dict[str, object]:
    return {
        "skill_root": config.root.as_posix(),
        "projects_root": config.projects_root.as_posix(),
        "max_tasks": config.max_tasks,
        "max_agents": config.max_agents,
        "layout": config.layout_mode,
        "max_panes_per_tab": config.max_panes,
        "state_file": config.state_file.as_posix(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Orca 任务分发器")
    parser.add_argument("--config", type=Path, default=config_path_from_script())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="校验配置并列出可用仓库")
    commands.add_parser("repos", help="列出候选仓库")
    project = commands.add_parser("project", help="按名称查询显式配置项目")
    project.add_argument("--name", required=True)
    branches = commands.add_parser("branches", help="列出并校验仓库分支")
    branches.add_argument("--repository", required=True)
    commands.add_parser("state", help="读取长期分发状态")
    recover = commands.add_parser("recover", help="恢复已分发任务的 Orca 会话")
    recover.add_argument("--task-id")
    recover.add_argument("--tenant-slug", help="指定同一任务下要恢复的租户")
    recover.add_argument("--force-unlock", action="store_true")
    task_source_parser = commands.add_parser("task-source", help="输出已渲染的任务获取提示词")
    task_source_parser.add_argument("--jql", help="仅本次运行覆盖配置中的 JQL")
    task_source_parser.add_argument("--flow", choices=("direct", "complete", "proposal"), required=True, help="用户选择的任务流程")
    decide_parser = commands.add_parser(
        "decide",
        help="校验外部调研后的项目与分支决策",
        description=(
            "读取 version=1 的 JSON：顶层仅包含 version 与 tasks。每项任务必须提供 task_id、title、task_url，"
            "可选 description、assignee、tenant、tenant_slug、source_task_id、source_assignee、parent_task_id、parent_assignee、"
            "reference_plan、gitnexus_report_path，并由外部流程提供 repository 与 base_branch。\n"
            "该命令不调用 Jira、GitNexus 或 Orca，不创建 worktree，不写运行状态；无法确认的任务返回 needs_confirmation。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    decide_parser.add_argument("--input", type=Path)
    for name, kwargs in (
        ("task-id", {"dest": "task_id"}),
        ("title", {}), ("task-url", {"dest": "task_url"}), ("repository", {}),
        ("base-branch", {"dest": "base_branch"}), ("dispatch-flow", {"dest": "dispatch_flow", "choices": ("direct", "complete", "proposal")}),
        ("source-task-id", {"dest": "source_task_id"}), ("description", {}), ("assignee", {}),
        ("tenant", {}), ("tenant-slug", {"dest": "tenant_slug"}), ("source-assignee", {"dest": "source_assignee"}),
        ("parent-task-id", {"dest": "parent_task_id"}), ("parent-assignee", {"dest": "parent_assignee"}),
        ("reference-plan", {"dest": "reference_plan"}), ("gitnexus-report-path", {"dest": "gitnexus_report_path"}),
        ("requirement-snapshot-path", {"dest": "requirement_snapshot_path"}), ("worktree-path", {"dest": "worktree_path"}),
    ):
        decide_parser.add_argument(f"--{name}", **kwargs)
    launch_parser = commands.add_parser(
        "launch",
        help="创建、等待并发送开发请求",
        description=(
            "输入 JSON 顶层只能为 tasks 列表，每项任务字段：\n"
            "  task_id（必填，字符串）：任务唯一标识\n"
            "  title（必填，字符串）：任务标题\n"
            "  task_url（必填，HTTPS）：必须由 task_url_template 生成\n"
            "  repository（必填，字符串）：仓库名，来自 repos 输出\n"
            "  repository_path（必填，字符串）：仓库绝对路径\n"
            "  base_branch（可选，字符串或 null）：基础分支，须在仓库白名单且存在\n"
            "  worktree_path（可选，字符串或 null）：仅 separate 布局必填，须为源仓库已登记的 linked worktree 绝对路径；split 布局不接受\n"
            "  reference_plan（可选，字符串或 null）：参考方案文本，非空时随任务信息发送给下游会话\n"
            "  assignee（可选，字符串或 null）：Jira 当前负责人显示名称，作为项目定位与任务上下文证据\n"
            "  tenant（可选，字符串）：租户显示名称；同一 task_id 的不同租户可分别分发\n"
            "  tenant_slug（可选，字符串）：租户稳定安全标识；省略时为 legacy\n"
            "  source_task_id（可选，字符串或 null）：归一化前的 Jira 开发子任务编号\n"
            "  source_assignee（可选，字符串或 null）：归一化前开发子任务的 Jira 负责人\n"
            "  parent_task_id（可选，字符串或 null）：关联父产品需求编号\n"
            "  parent_assignee（可选，字符串或 null）：父产品需求负责人，仅作上下文\n"
            "  requirement_snapshot_path（可选，字符串或 null）：已校验的完整原始需求快照绝对路径，随任务上下文发送以复用正文与附件本体；缺失视为分发前置未完成，必须阻断该任务\n"
            "separate 布局的 worktree 由 dev-spec-gen 统一 worktree CLI 创建或复用独立 linked worktree。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    launch_parser.add_argument("--input", type=Path, required=True)
    launch_parser.add_argument("--force-unlock", action="store_true")
    reset = commands.add_parser("reset", help="允许任务重新分发")
    reset.add_argument("task_id")
    reset.add_argument("--force-unlock", action="store_true")
    reset.add_argument("--force", action="store_true", help="允许复位已分发的任务")
    reset.add_argument("--tenant-slug", help="指定同一任务下要复位的租户")
    return parser


def branch_entries(mapping: Mapping[str, str | None]) -> list[dict[str, str | None]]:
    return [{"name": branch, "description": description} for branch, description in mapping.items()]


def execute(arguments: argparse.Namespace) -> dict[str, object]:
    config = load_config(arguments.config)
    store = StateStore(config.state_file)

    if arguments.command == "validate":
        repositories = discover_repositories(config)
        return {"config": config_summary(config), "repositories": [repository.to_dict() for repository in repositories]}
    if arguments.command == "repos":
        return {"repositories": [repository.to_dict() for repository in discover_repositories(config)]}
    if arguments.command == "project":
        repository = configured_repository(config, arguments.name)
        if repository is not None:
            return {
                "match_mode": "configured",
                "repository": repository.to_dict(),
                "base_branches": branch_entries(config.branch_map_for(arguments.name)),
            }
        candidates = recursive_repositories(config, arguments.name)
        if not candidates:
            raise DispatcherError("repository_not_found", f"未找到项目：{arguments.name}")
        if len(candidates) == 1:
            return {
                "match_mode": "recursive",
                "repository": candidates[0].to_dict(),
                "base_branches": branch_entries(config.branch_options),
            }
        return {
            "match_mode": "ambiguous",
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
    if arguments.command == "branches":
        repository = repository_for_name(config, arguments.repository)
        return {
            "repository": repository.to_dict(),
            "tenants": [
                {"name": tenant, "slug": slug}
                for tenant, slug in config.tenants_for(repository.name).items()
            ],
            "branches": [
                {"name": branch, "description": description,
                 "valid": branch_exists(repository, branch) if config.validate_branch else None}
                for branch, description in config.branch_map_for(repository.name).items()
            ],
        }
    if arguments.command == "state":
        return {"state": store.snapshot()}
    if arguments.command == "recover":
        return recover(
            config=config,
            store=store,
            orca=OrcaClient(),
            task_id=arguments.task_id,
            force_unlock=arguments.force_unlock,
            tenant_slug=arguments.tenant_slug,
        )
    if arguments.command == "task-source":
        return task_source_prompt(config, arguments.jql, arguments.flow)
    if arguments.command == "decide":
        cli_fields = ("task_id", "title", "task_url", "repository", "base_branch", "dispatch_flow", "source_task_id", "description", "assignee", "tenant", "tenant_slug", "source_assignee", "parent_task_id", "parent_assignee", "reference_plan", "gitnexus_report_path", "requirement_snapshot_path", "worktree_path")
        supplied = {name: getattr(arguments, name) for name in cli_fields if getattr(arguments, name) is not None}
        if arguments.input is not None:
            if supplied:
                raise DispatcherError("invalid_input", "decide --input 不能与任务参数混用")
            return decide(config, arguments.input)
        required = ("task_id", "title", "task_url", "repository")
        missing = next((name for name in required if getattr(arguments, name) is None), None)
        if missing is not None:
            raise DispatcherError("invalid_input", f"缺少单任务参数：--{missing.replace('_', '-')}")
        return decide_values(config, validate_decision_values([supplied]))
    if arguments.command == "reset":
        removed = store.reset(
            arguments.task_id,
            arguments.force_unlock,
            arguments.force,
            arguments.tenant_slug,
        )
        if removed:
            append_history_safely(store, {
                "task": arguments.task_id,
                "result": "reset",
                "forced": arguments.force,
            })
        return {"task_id": arguments.task_id, "reset": removed}
    if arguments.command == "launch":
        return launch(
            config=config,
            assignments=read_assignments(arguments.input),
            store=store,
            orca=OrcaClient(),
            force_unlock=arguments.force_unlock,
        )
    raise DispatcherError("invalid_command", f"未知命令：{arguments.command}")


def emit(value: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
    except SystemExit as exit_error:
        if exit_error.code == 0:
            return 0
        emit({"ok": False, "error": {"code": "invalid_usage", "message": "参数不合法，使用 --help 查看用法"}})
        return 1
    try:
        emit({"ok": True, "result": execute(arguments)})
        return 0
    except DispatcherError as error:
        print(error.message, file=sys.stderr)
        emit({"ok": False, "error": {"code": error.code, "message": error.message}})
        return 1
    except Exception as error:  # pragma: no cover
        print(str(error), file=sys.stderr)
        emit({"ok": False, "error": {"code": "internal_error", "message": "Dispatcher 内部错误"}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
