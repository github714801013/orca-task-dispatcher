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
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping
from urllib.parse import quote, urlparse

import yaml


class DispatcherError(Exception):
    """可安全返回给 Skill 的业务错误。"""

    def __init__(self, code: str, message: str, *, orca_code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.orca_code = orca_code


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
TASK_STATUSES = frozenset({"launching", "dispatched", "requires_manual_reset"})
FLOW_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
LEGACY_FLOW_NAMES = ("direct", "complete", "separate", "proposal")
LEGACY_FLOW_ALIASES = {"separate": "complete"}
COMMAND_TEMPLATE_FIELDS = frozenset({"task_url", "task_id", "base_branch", "requirement_snapshot_path"})
TASK_CONTEXT_TEMPLATE_FIELDS = frozenset({
    "title", "description", "assignee", "tenant", "assignment_id",
    "reference_plan", "gitnexus_report_path", "requirement_snapshot_path", "source_task_id", "source_assignee",
})
COMMAND_TEMPLATE_ALLOWED_FIELDS = COMMAND_TEMPLATE_FIELDS | TASK_CONTEXT_TEMPLATE_FIELDS
ORCA_COMMAND_TIMEOUT_SECONDS = 30


def require_task_id(value: Any) -> str:
    task_id = require_text(value, "task_id")
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise DispatcherError("invalid_input", "task_id 格式不合法")
    return task_id


WORKTREE_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){0,1}\Z")
WORKTREE_SLUG_MAX_LENGTH = 32


def require_worktree_slug(value: Any) -> str:
    """任务工作区名里的语义 slug：1–2 个英文小写 kebab 词，杜绝路径与流程词。"""
    slug = require_text(value, "worktree_slug")
    if len(slug) > WORKTREE_SLUG_MAX_LENGTH or not WORKTREE_SLUG_PATTERN.match(slug):
        raise DispatcherError(
            "invalid_input",
            f"worktree_slug 必须是小写字母数字与短横线组成的 1–2 个词，最长 {WORKTREE_SLUG_MAX_LENGTH} 位",
        )
    return slug


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


def require_dispatch_flow(value: Any, field: str = "dispatch_flow") -> str:
    """校验流程名格式；是否为已注册流程由配置注册表另行判定。"""
    dispatch_flow = require_text(value, field)
    if not FLOW_NAME_PATTERN.fullmatch(dispatch_flow):
        raise DispatcherError(
            "invalid_input",
            f"{field} 只能包含字母、数字、点、下划线和连字符，且必须以字母或数字开头",
        )
    return dispatch_flow


def legacy_state_key(task_id: str, tenant_slug: str = "legacy") -> str:
    task = require_task_id(task_id)
    tenant = require_tenant_slug(tenant_slug)
    return task if tenant == "legacy" else f"{task}::{tenant}"


def canonical_state_key(task_id: str, tenant_slug: str = "legacy", dispatch_flow: str = "complete") -> str:
    return f"{require_task_id(task_id)}::{require_tenant_slug(tenant_slug)}::flow::{require_dispatch_flow(dispatch_flow)}"


def state_key_for(task_id: str, tenant_slug: str = "legacy", dispatch_flow: str = "complete") -> str:
    return canonical_state_key(task_id, tenant_slug, dispatch_flow)


def parse_state_key(state_key: str) -> tuple[str, str, str | None]:
    parts = state_key.split("::")
    if len(parts) == 1:
        return require_task_id(parts[0]), "legacy", None
    if len(parts) == 2:
        return require_task_id(parts[0]), require_tenant_slug(parts[1]), None
    if len(parts) == 4 and parts[2] == "flow":
        return require_task_id(parts[0]), require_tenant_slug(parts[1]), require_dispatch_flow(parts[3])
    raise DispatcherError("state_unreadable", "state.json 状态键格式不受支持")


@dataclass(frozen=True)
class StateEntry:
    state_key: str
    value: Mapping[str, object]
    task_id: str
    tenant_slug: str
    dispatch_flow: str
    assignment_id: str


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
    repository_id: str | None = None
    base_branch: str | None = None
    comment: str | None = None

    @property
    def selector(self) -> str:
        if self.worktree_id.startswith(("id:", "path:", "current")):
            return self.worktree_id
        return f"id:{self.worktree_id}"


@dataclass(frozen=True)
class OrcaTerminal:
    handle: str
    worktree_id: str
    agent_identity: str | None
    connected: bool
    writable: bool


@dataclass(frozen=True)
class Assignment:
    task: Task
    repository: str
    repository_path: Path
    base_branch: str | None
    tenant: str = "legacy"
    tenant_slug: str = "legacy"
    worktree_slug: str | None = None
    reference_plan: str | None = None
    assignee: str | None = None
    source_task_id: str | None = None
    source_assignee: str | None = None
    parent_task_id: str | None = None
    parent_assignee: str | None = None
    gitnexus_report_path: Path | None = None
    requirement_snapshot_path: Path | None = None
    # 直连数据类的内部构造（如 worktree create）不经过 decide，默认按"无候选资格要求"处理；
    # 任何要交给 launch 校验的路径都必须用 Assignment.from_dict 解析，禁止直连后伪造资格证据。
    candidate_eligible: bool | None = True
    candidate_eligibility_reason: str | None = "内部已完成候选资格校验"
    candidate_jql: str | None = "内部调用"
    reference_plan_source: str = "none"
    parent_reference_plan: str | None = None
    candidate_evidence_provided: bool = field(default=True, repr=False, compare=False)
    dispatch_flow: str = "complete"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], default_flow: str) -> "Assignment":
        unknown_fields = set(value) - {
            "task_id", "title", "description", "task_url", "repository", "repository_path",
            "base_branch", "worktree_slug", "reference_plan", "assignee", "tenant", "tenant_slug", "assignment_id",
            "source_task_id", "source_assignee", "parent_task_id", "parent_assignee", "gitnexus_report_path",
            "requirement_snapshot_path",
            "candidate_eligible", "candidate_eligibility_reason", "candidate_jql", "reference_plan_source", "parent_reference_plan",
            "dispatch_flow",
        }
        if unknown_fields:
            raise DispatcherError("invalid_input", f"任务包含未知字段：{sorted(unknown_fields)[0]}")
        branch = value.get("base_branch")
        if branch is not None and (not isinstance(branch, str) or not branch.strip()):
            raise DispatcherError("invalid_input", "base_branch 必须是字符串或 null")
        worktree_slug = value.get("worktree_slug")
        if worktree_slug is not None and (not isinstance(worktree_slug, str) or not worktree_slug.strip()):
            raise DispatcherError("invalid_input", "worktree_slug 必须是字符串或 null")
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
        canonical_task_id = require_task_id(value.get("task_id"))
        if parent_task_id is not None and parent_task_id != canonical_task_id:
            raise DispatcherError(
                "invalid_input",
                "task_id 必须是上游归一化后的实际需求编号；parent_task_id 必须省略或等于 task_id，Dispatcher 不会根据 parent_task_id 自动替换 task_id",
            )
        if parent_task_id is not None and source_task_id is None:
            raise DispatcherError(
                "invalid_input",
                "归一化后的实际需求必须保留原始开发子任务编号 source_task_id，否则下游会按父需求编号处理开发任务",
            )
        parent_assignee = require_optional_text(value.get("parent_assignee"), "parent_assignee")
        gitnexus_report_path = value.get("gitnexus_report_path")
        if gitnexus_report_path is not None and (not isinstance(gitnexus_report_path, str) or not gitnexus_report_path.strip()):
            raise DispatcherError("invalid_input", "gitnexus_report_path 必须是字符串或 null")
        requirement_snapshot_path = value.get("requirement_snapshot_path")
        if requirement_snapshot_path is not None and (not isinstance(requirement_snapshot_path, str) or not requirement_snapshot_path.strip()):
            raise DispatcherError("invalid_input", "requirement_snapshot_path 必须是字符串或 null")
        candidate_eligible = value.get("candidate_eligible")
        if candidate_eligible is not None and not isinstance(candidate_eligible, bool):
            raise DispatcherError("invalid_input", "candidate_eligible 必须是布尔值或 null")
        candidate_reason = value.get("candidate_eligibility_reason")
        if candidate_reason is not None and (not isinstance(candidate_reason, str) or not candidate_reason.strip()):
            raise DispatcherError("invalid_input", "candidate_eligibility_reason 必须是非空字符串或 null")
        candidate_jql = value.get("candidate_jql")
        if candidate_jql is not None and (not isinstance(candidate_jql, str) or not candidate_jql.strip()):
            raise DispatcherError("invalid_input", "candidate_jql 必须是非空字符串或 null")
        reference_plan_source = value.get("reference_plan_source", "none")
        if reference_plan_source not in {"task", "parent", "none"}:
            raise DispatcherError("invalid_input", "reference_plan_source 必须是 task、parent 或 none")
        parent_reference_plan = value.get("parent_reference_plan")
        if parent_reference_plan is not None and (not isinstance(parent_reference_plan, str) or not parent_reference_plan.strip()):
            raise DispatcherError("invalid_input", "parent_reference_plan 必须是字符串或 null")
        repository_path = Path(require_text(value.get("repository_path"), "repository_path"))
        if not repository_path.is_absolute():
            raise DispatcherError("invalid_input", "repository_path 必须是绝对路径")
        worktree_slug_value = worktree_slug.strip() if isinstance(worktree_slug, str) else None
        if worktree_slug_value is not None:
            require_worktree_slug(worktree_slug_value)
        report_value = gitnexus_report_path.strip() if isinstance(gitnexus_report_path, str) else None
        if report_value is not None and not Path(report_value).is_absolute():
            raise DispatcherError("invalid_input", "gitnexus_report_path 必须是绝对路径")
        snapshot_value = requirement_snapshot_path.strip() if isinstance(requirement_snapshot_path, str) else None
        if snapshot_value is not None and not Path(snapshot_value).is_absolute():
            raise DispatcherError("invalid_input", "requirement_snapshot_path 必须是绝对路径")
        dispatch_flow = require_dispatch_flow(value.get("dispatch_flow", default_flow))
        evidence_fields = (
            "candidate_eligible", "candidate_eligibility_reason", "candidate_jql", "reference_plan_source",
        )
        candidate_evidence_provided = all(field in value for field in evidence_fields)
        if any(field in value for field in evidence_fields) and not candidate_evidence_provided:
            raise DispatcherError("invalid_input", "任务缺少完整候选资格交接字段（含 candidate_jql）")
        return cls(
            task=Task.from_dict(value),
            repository=require_text(value.get("repository"), "repository"),
            repository_path=repository_path,
            base_branch=branch.strip() if isinstance(branch, str) else None,
            tenant=tenant,
            tenant_slug=tenant_slug,
            worktree_slug=worktree_slug_value,
            reference_plan=reference_plan.strip() if isinstance(reference_plan, str) else None,
            assignee=assignee.strip() if isinstance(assignee, str) else None,
            source_task_id=source_task_id,
            source_assignee=source_assignee,
            parent_task_id=parent_task_id,
            parent_assignee=parent_assignee,
            gitnexus_report_path=Path(report_value) if report_value is not None else None,
            requirement_snapshot_path=Path(snapshot_value) if snapshot_value is not None else None,
            candidate_eligible=candidate_eligible,
            candidate_eligibility_reason=candidate_reason.strip() if isinstance(candidate_reason, str) else None,
            candidate_jql=candidate_jql.strip() if isinstance(candidate_jql, str) else None,
            reference_plan_source=reference_plan_source,
            parent_reference_plan=parent_reference_plan.strip() if isinstance(parent_reference_plan, str) else None,
            candidate_evidence_provided=candidate_evidence_provided,
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
            "worktree_slug": self.worktree_slug,
            "reference_plan": self.reference_plan,
            "assignee": self.assignee,
            "source_task_id": self.source_task_id,
            "source_assignee": self.source_assignee,
            "parent_task_id": self.parent_task_id,
            "parent_assignee": self.parent_assignee,
            "gitnexus_report_path": self.gitnexus_report_path.as_posix() if self.gitnexus_report_path else None,
            "requirement_snapshot_path": self.requirement_snapshot_path.as_posix() if self.requirement_snapshot_path else None,
            "candidate_eligible": self.candidate_eligible,
            "candidate_eligibility_reason": self.candidate_eligibility_reason,
            "candidate_jql": self.candidate_jql,
            "reference_plan_source": self.reference_plan_source,
            "parent_reference_plan": self.parent_reference_plan,
            "dispatch_flow": self.dispatch_flow,
        }


@dataclass(frozen=True)
class SendReceipt:
    """终端投递回执：接受输入与观察到起步是两件事。"""

    accepted: bool
    request_id: str | None
    stages: tuple[str, ...]
    observation: str | None
    process_incarnation: str | None = None

    @property
    def turn_started(self) -> bool:
        return "turn_started" in self.stages


@dataclass(frozen=True)
class DispatchRecord:
    assignment: Assignment
    worktree: OrcaWorktree
    terminal_handle: str
    receipt: SendReceipt
    initial_prompt_digest: str
    duplicate_of_state_key: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedWorktree:
    worktree: OrcaWorktree
    active_terminals: tuple[OrcaTerminal, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Project:
    path: str
    base_branches: Mapping[str, str | None]
    description: str | None = None
    tenants: Mapping[str, str] = field(default_factory=dict)
    branch_priority: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class FlowStage:
    """流程节点：可被任意流程按名复用的一段分发定义。"""

    name: str
    command_template: str | None = None
    session_prompt: str | None = None
    fetch_prompt: str | None = None
    query: str | None = None
    next_steps: tuple[str, ...] = ()
    requires_worktree: bool = True
    requires_snapshot: bool = True
    requires_candidate_eligibility: bool = False
    requires_reference_plan_candidate: bool = False


@dataclass(frozen=True)
class DispatchFlow:
    """分发流程：按顺序引用节点，并派生出实际下发的命令与提示词。"""

    name: str
    stage_names: tuple[str, ...]
    is_default: bool
    command_template: str
    session_prompt: str | None
    fetch_prompt: str | None
    next_steps: tuple[str, ...]
    requires_worktree: bool
    requires_snapshot: bool
    requires_candidate_eligibility: bool = False
    requires_reference_plan_candidate: bool = False
    query: str | None = None


@dataclass(frozen=True)
class Config:
    root: Path
    projects_root: Path
    projects: Mapping[str, Project]
    branch_options: Mapping[str, str | None]
    default_branch: str | None
    validate_branch: bool
    max_tasks: int
    max_agents: int
    read_retry_attempts: int
    read_retry_delay_ms: int
    state_file: Path
    task_url_template: str
    task_source_type: str
    task_source_query: str
    fetch_prompt: str
    stages: Mapping[str, FlowStage] = field(default_factory=dict)
    flows: Mapping[str, DispatchFlow] = field(default_factory=dict)
    default_flow: str = "complete"
    reference_plan_field: str | None = None
    session_prompt: str = ""
    recovery_session_prompt: str = ""
    deprecation_warnings: tuple[str, ...] = ()

    @property
    def runtime_dir(self) -> Path:
        return self.state_file.parent

    def flow_for(self, name: str, field: str = "dispatch_flow") -> DispatchFlow:
        """取已注册流程；未注册时给出明确错误而不是回退默认流程。"""
        flow_name = require_dispatch_flow(name, field)
        flow = self.flows.get(flow_name)
        if flow is None:
            available = "、".join(self.flows) or "无"
            raise DispatcherError(
                "invalid_input",
                f"{field} 未注册：{flow_name}；当前注册表包含：{available}",
            )
        return flow

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


CONFIG_DIR_ENVIRONMENT = "ORCA_DISPATCHER_CONFIG_DIR"


def config_path_from_script() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "dispatcher.yaml"


def config_path_from_environment() -> Path | None:
    directory = os.environ.get(CONFIG_DIR_ENVIRONMENT, "").strip()
    return Path(directory) / "dispatcher.yaml" if directory else None


def resolve_config_path(config_file: Path | None) -> Path:
    if config_file is not None:
        return config_file
    environment_path = config_path_from_environment()
    if environment_path is not None and not environment_path.parent.is_dir():
        return config_default_path_from_script()
    return environment_path or config_path_from_script()


def config_root_path(config_file: Path | None, resolved_config_file: Path) -> Path:
    if config_file is None and not resolved_config_file.is_file():
        return config_path_from_script()
    return resolved_config_file


def child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop(CONFIG_DIR_ENVIRONMENT, None)
    return environment


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


def read_config_yaml(
    config_file: Path,
    *,
    required: bool,
    error_label: str | None = None,
) -> Mapping[str, Any] | None:
    target = error_label or str(config_file)
    try:
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return None
        raise DispatcherError("config_unreadable", f"无法读取配置：{target}")
    except OSError as error:
        raise DispatcherError("config_unreadable", f"无法读取配置：{target}") from error
    except UnicodeError as error:
        raise DispatcherError("invalid_config", f"配置文件编码错误：{target}") from error
    except yaml.YAMLError as error:
        message = f"YAML 格式错误：{error_label or '配置文件'}"
        raise DispatcherError("invalid_config", message) from error
    return require_mapping(raw, "配置根")


def config_layers(config_file: Path | None) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """返回合并后的配置与用户覆盖层，便于识别仅在用户层出现的旧字段。"""
    default_file = config_default_path_from_script()
    default = read_config_yaml(default_file, required=False)
    user_file = resolve_config_path(config_file)
    environment_path = config_path_from_environment()
    user_label = "客户 dispatcher.yaml" if user_file == environment_path else None
    user = read_config_yaml(user_file, required=False, error_label=user_label)
    if default is None and user is None:
        raise DispatcherError("config_unreadable", f"默认配置不存在：{default_file}")
    if default is None:
        return user or {}, user or {}
    return merge_config_values(default, user or {}), user or {}

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


def nested_mapping(value: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def session_prompt_for(session_prompt: Mapping[str, Any], user_session_prompt: Mapping[str, Any]) -> str:
    """取唯一的完整开发会话说明；用户层旧 separate 键仍按 complete 沿用。"""
    candidates = (
        user_session_prompt.get("complete"),
        user_session_prompt.get("separate"),
        session_prompt.get("complete"),
        session_prompt.get("separate"),
    )
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise DispatcherError("invalid_config", "task_source.session_prompt 缺少完整开发会话说明（complete）")


def deprecation_warnings_for(root_data: Mapping[str, Any]) -> tuple[str, ...]:
    """列出已失效但仍被忽略的旧配置字段。"""
    warnings: list[str] = []
    dispatch = root_data.get("dispatch")
    if isinstance(dispatch, Mapping):
        if isinstance(dispatch.get("layout"), Mapping):
            warnings.append("dispatch.layout 已失效并被忽略：分发固定为每个任务独立终端")
        terminal = dispatch.get("terminal")
        if isinstance(terminal, Mapping) and "shell_commands" in terminal:
            warnings.append("dispatch.terminal.shell_commands 已失效并被忽略：不再创建普通 shell pane")
        skill = dispatch.get("skill")
        if isinstance(skill, Mapping):
            templates = skill.get("command_templates")
            if isinstance(templates, Mapping) and "split" in templates:
                warnings.append("dispatch.skill.command_templates.split 已失效并被忽略")
            if isinstance(templates, Mapping) and "separate" in templates:
                warnings.append("dispatch.skill.command_templates.separate 已更名为 complete；仅在未提供 complete 时沿用其内容")
            if isinstance(templates, Mapping) and any(name in templates for name in LEGACY_FLOW_NAMES):
                warnings.append(
                    "dispatch.skill.command_templates 已由顶层 stages/flows 注册表取代；"
                    "未提供注册表时自动映射为等价节点并继续生效"
                )
    task_source = root_data.get("task_source")
    if isinstance(task_source, Mapping):
        session_prompt = task_source.get("session_prompt")
        if isinstance(session_prompt, Mapping) and "split" in session_prompt:
            warnings.append("task_source.session_prompt.split 已失效并被忽略")
        legacy_flows = task_source.get("flows")
        if isinstance(legacy_flows, Mapping) and legacy_flows:
            warnings.append(
                "task_source.flows 已由顶层 stages/flows 注册表取代；"
                "未提供注册表时其 next_steps 自动映射为等价节点步骤"
            )
    return tuple(warnings)


def require_flow_name(value: Any, field: str) -> str:
    name = require_text(value, field)
    if not FLOW_NAME_PATTERN.fullmatch(name):
        raise DispatcherError(
            "invalid_config",
            f"{field} 只能包含字母、数字、点、下划线和连字符，且必须以字母或数字开头",
        )
    return name


def require_flow_steps(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(step, str) and step.strip() for step in value):
        raise DispatcherError("invalid_config", f"{field} 必须是非空字符串列表")
    return tuple(step.strip() for step in value)


def parse_flow_stage(value: Any, index: int) -> FlowStage:
    field = f"stages[{index}]"
    stage = require_mapping(value, field)
    unknown = set(stage) - {
        "name", "command_template", "session_prompt", "fetch_prompt", "query",
        "next_steps", "requires_worktree", "requires_snapshot", "requires_candidate_eligibility", "requires_reference_plan_candidate",
    }
    if unknown:
        raise DispatcherError("invalid_config", f"{field} 包含未知字段：{sorted(unknown)[0]}")
    command_template = stage.get("command_template")
    if command_template is not None:
        command_template = require_text(command_template, f"{field}.command_template")
        validate_command_template(command_template)
    return FlowStage(
        name=require_flow_name(stage.get("name"), f"{field}.name"),
        command_template=command_template,
        session_prompt=require_optional_text(stage.get("session_prompt"), f"{field}.session_prompt"),
        fetch_prompt=require_optional_text(stage.get("fetch_prompt"), f"{field}.fetch_prompt"),
        query=require_optional_text(stage.get("query"), f"{field}.query"),
        next_steps=require_flow_steps(stage.get("next_steps"), f"{field}.next_steps"),
        requires_worktree=require_bool(stage.get("requires_worktree", True), f"{field}.requires_worktree"),
        requires_snapshot=require_bool(stage.get("requires_snapshot", True), f"{field}.requires_snapshot"),
        requires_candidate_eligibility=require_bool(stage.get("requires_candidate_eligibility", False), f"{field}.requires_candidate_eligibility"),
        requires_reference_plan_candidate=require_bool(stage.get("requires_reference_plan_candidate", False), f"{field}.requires_reference_plan_candidate"),
    )


def parse_flow_stages(value: Any) -> Mapping[str, FlowStage]:
    if not isinstance(value, list) or not value:
        raise DispatcherError("invalid_config", "stages 必须是非空数组")
    stages: dict[str, FlowStage] = {}
    for index, item in enumerate(value):
        stage = parse_flow_stage(item, index)
        if stage.name in stages:
            raise DispatcherError("invalid_config", f"stages 包含重复节点名：{stage.name}")
        stages[stage.name] = stage
    return MappingProxyType(stages)


def compose_flow(
    name: str,
    stage_names: tuple[str, ...],
    stages: Mapping[str, FlowStage],
    is_default: bool,
) -> DispatchFlow:
    if not stage_names:
        raise DispatcherError("invalid_config", f"流程 {name} 必须引用至少一个节点")
    missing = next((stage_name for stage_name in stage_names if stage_name not in stages), None)
    if missing is not None:
        raise DispatcherError("invalid_config", f"流程 {name} 引用了未定义节点：{missing}")
    resolved = tuple(stages[stage_name] for stage_name in stage_names)
    command_template = next((stage.command_template for stage in reversed(resolved) if stage.command_template), None)
    requires_candidate_eligibility = any(stage.requires_candidate_eligibility for stage in resolved)
    requires_reference_plan_candidate = any(stage.requires_reference_plan_candidate for stage in resolved)
    if requires_reference_plan_candidate and not requires_candidate_eligibility:
        raise DispatcherError("invalid_config", f"流程 {name} 要求参考方案候选时必须同时要求候选资格")
    if command_template is None:
        raise DispatcherError("invalid_config", f"流程 {name} 引用的节点都未声明 command_template")
    return DispatchFlow(
        name=name,
        stage_names=stage_names,
        is_default=is_default,
        command_template=command_template,
        session_prompt=next((stage.session_prompt for stage in reversed(resolved) if stage.session_prompt), None),
        fetch_prompt=next((stage.fetch_prompt for stage in resolved if stage.fetch_prompt), None),
        next_steps=tuple(step for stage in resolved for step in stage.next_steps),
        requires_worktree=all(stage.requires_worktree for stage in resolved),
        requires_snapshot=all(stage.requires_snapshot for stage in resolved),
        requires_candidate_eligibility=requires_candidate_eligibility,
        requires_reference_plan_candidate=requires_reference_plan_candidate,
        query=next((stage.query for stage in resolved if stage.query), None),
    )


def parse_dispatch_flows(value: Any, stages: Mapping[str, FlowStage]) -> tuple[Mapping[str, DispatchFlow], str]:
    if not isinstance(value, list) or not value:
        raise DispatcherError("invalid_config", "flows 必须是非空数组")
    parsed: list[tuple[str, tuple[str, ...], bool]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        field = f"flows[{index}]"
        flow = require_mapping(item, field)
        unknown = set(flow) - {"name", "stages", "default"}
        if unknown:
            raise DispatcherError("invalid_config", f"{field} 包含未知字段：{sorted(unknown)[0]}")
        name = require_flow_name(flow.get("name"), f"{field}.name")
        if name in seen:
            raise DispatcherError("invalid_config", f"flows 包含重复流程名：{name}")
        seen.add(name)
        stage_names = require_flow_steps(flow.get("stages"), f"{field}.stages")
        is_default = require_bool(flow.get("default", False), f"{field}.default")
        parsed.append((name, stage_names, is_default))
    defaults = [name for name, _, is_default in parsed if is_default]
    if len(defaults) != 1:
        raise DispatcherError(
            "invalid_config",
            "flows 必须且只能有一个 default: true" if not defaults else "flows 只能有一个 default: true",
        )
    flows = {
        name: compose_flow(name, stage_names, stages, name == defaults[0])
        for name, stage_names, _ in parsed
    }
    return MappingProxyType(flows), defaults[0]


def legacy_flow_registry(
    command_templates: Mapping[str, Any],
    task_source_flows: Mapping[str, Any],
    session_prompt: str | None,
    base: tuple[Mapping[str, FlowStage], Mapping[str, DispatchFlow], str] | None = None,
) -> tuple[Mapping[str, FlowStage], Mapping[str, DispatchFlow], str]:
    """把旧的 command_templates / task_source.flows 映射为注册表；已有注册表时按流程覆盖。"""
    base_stages, base_flows, base_default = base or ({}, {}, "complete")
    stages: dict[str, FlowStage] = dict(base_stages)
    flows: dict[str, DispatchFlow] = dict(base_flows)
    ordered_templates = sorted(
        command_templates.items(),
        key=lambda item: 0 if LEGACY_FLOW_ALIASES.get(item[0]) == "complete" else 1,
    )
    for index, (name, template) in enumerate(ordered_templates):
        if name not in LEGACY_FLOW_NAMES:
            continue
        flow_name = require_flow_name(LEGACY_FLOW_ALIASES.get(name, name), f"dispatch.skill.command_templates 键[{index}]")
        legacy_steps = task_source_flows.get(name)
        if legacy_steps is None:
            legacy_steps = task_source_flows.get(flow_name)
        steps: tuple[str, ...] = ()
        if isinstance(legacy_steps, Mapping):
            steps = require_flow_steps(legacy_steps.get("next_steps"), f"task_source.flows.{name}.next_steps")
        command_template = require_text(template, f"dispatch.skill.command_templates.{name}")
        validate_command_template(command_template)
        existing = flows.get(flow_name)
        stage_name = existing.stage_names[0] if existing is not None else f"{flow_name}_stage"
        previous = stages.get(stage_name)
        stages[stage_name] = FlowStage(
            name=stage_name,
            command_template=command_template,
            session_prompt=(previous.session_prompt if previous else None)
            or (session_prompt if flow_name == "complete" else None),
            fetch_prompt=previous.fetch_prompt if previous else None,
            next_steps=steps or (previous.next_steps if previous else ()),
            requires_worktree=previous.requires_worktree if previous else True,
            requires_snapshot=previous.requires_snapshot if previous else True,
            requires_candidate_eligibility=previous.requires_candidate_eligibility if previous else False,
            requires_reference_plan_candidate=previous.requires_reference_plan_candidate if previous else False,
        )
        flows[flow_name] = compose_flow(
            flow_name,
            (stage_name,),
            MappingProxyType(stages),
            existing.is_default if existing is not None else flow_name == "complete",
        )
    if "complete" not in flows:
        raise DispatcherError("invalid_config", "未配置 flows 注册表，且旧配置缺少 complete 命令模板")
    return MappingProxyType(stages), MappingProxyType(flows), base_default


def parse_registry(
    root_data: Mapping[str, Any],
) -> tuple[Mapping[str, FlowStage], Mapping[str, DispatchFlow], str]:
    stages = parse_flow_stages(root_data.get("stages"))
    flows, default_flow = parse_dispatch_flows(root_data.get("flows"), stages)
    return stages, flows, default_flow


def resolve_flow_registry(
    root_data: Mapping[str, Any],
    user_data: Mapping[str, Any],
    session_prompt: str | None,
) -> tuple[Mapping[str, FlowStage], Mapping[str, DispatchFlow], str]:
    """新键优先；用户层仍使用旧键时自动映射为等价注册表，保证既有定制继续生效。"""
    user_uses_registry = "stages" in user_data or "flows" in user_data
    base = parse_registry(root_data) if ("stages" in root_data or "flows" in root_data) else None
    if not user_uses_registry:
        legacy_templates = nested_mapping(user_data, "dispatch", "skill", "command_templates")
        legacy_user_flows = nested_mapping(user_data, "task_source", "flows")
        if legacy_templates or legacy_user_flows:
            merged_templates = dict(nested_mapping(root_data, "dispatch", "skill", "command_templates"))
            merged_templates.update(legacy_templates)
            merged_flows = dict(nested_mapping(root_data, "task_source", "flows"))
            merged_flows.update(legacy_user_flows)
            return legacy_flow_registry(merged_templates, merged_flows, session_prompt, base)
    if base is not None:
        return base
    return legacy_flow_registry(
        dict(nested_mapping(root_data, "dispatch", "skill", "command_templates")),
        dict(nested_mapping(root_data, "task_source", "flows")),
        session_prompt,
    )


def load_config(config_file: Path | None = None) -> Config:
    resolved_config_file = resolve_config_path(config_file)
    root_data, user_data = config_layers(resolved_config_file)

    workspace = require_mapping(root_data.get("workspace"), "workspace")
    base_branch = require_mapping(root_data.get("base_branch"), "base_branch")
    task_source = require_mapping(root_data.get("task_source"), "task_source")
    interaction = require_mapping(root_data.get("interaction"), "interaction")
    dispatch = require_mapping(root_data.get("dispatch"), "dispatch")
    skill_value = dispatch.get("skill")
    skill = require_mapping(skill_value, "dispatch.skill") if skill_value is not None else {}
    terminal = require_mapping(dispatch.get("terminal"), "dispatch.terminal")
    concurrency = require_mapping(dispatch.get("concurrency"), "dispatch.concurrency")
    dedup = require_mapping(root_data.get("dedup"), "dedup")

    root = skill_root_from_config(config_root_path(config_file, resolved_config_file))
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
    if not require_bool(dedup.get("enabled"), "dedup.enabled"):
        raise DispatcherError("invalid_config", "dedup.enabled 必须为 true")

    task_url_template = require_text(task_source.get("task_url_template"), "task_source.task_url_template")
    task_url_for(task_url_template, "template-check")
    resolved_session_prompt = session_prompt_for(session_prompt, nested_mapping(user_data, "task_source", "session_prompt"))
    stages, flows, default_flow = resolve_flow_registry(root_data, user_data, resolved_session_prompt)
    deprecation_warnings = deprecation_warnings_for(root_data)

    return Config(
        root=root,
        projects_root=projects_root,
        projects=projects,
        branch_options=require_branch_mapping(base_branch.get("options", []), "base_branch.options"),
        default_branch=require_optional_text(base_branch.get("default"), "base_branch.default"),
        validate_branch=require_bool(base_branch.get("validate"), "base_branch.validate"),
        max_tasks=require_integer(task_source.get("max_tasks"), "task_source.max_tasks", 1, 12),
        max_agents=require_integer(concurrency.get("max_agents"), "dispatch.concurrency.max_agents", 1, 12),
        read_retry_attempts=require_integer(terminal.get("read_retry_attempts"), "dispatch.terminal.read_retry_attempts", 1, 3),
        read_retry_delay_ms=require_integer(terminal.get("read_retry_delay_ms"), "dispatch.terminal.read_retry_delay_ms", 0, 5_000),
        state_file=relative_to_root(root, dedup.get("state_file"), "dedup.state_file"),
        task_url_template=task_url_template,
        task_source_type=task_source_type,
        task_source_query=task_source_query,
        fetch_prompt=fetch_prompt,
        reference_plan_field=reference_plan_field,
        stages=stages,
        flows=flows,
        default_flow=default_flow,
        session_prompt=resolved_session_prompt,
        recovery_session_prompt=recovery_session_prompt,
        deprecation_warnings=deprecation_warnings,
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


def branch_exists(repository: Repository, branch: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repository.path), "rev-parse", "--verify", f"{branch}^{{commit}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=child_environment(),
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


def validate_requirement_snapshot_path(config: Config, assignment: Assignment, docs_root: Path) -> None:
    path = assignment.requirement_snapshot_path
    if path is None:
        raise DispatcherError("invalid_input", "requirement_snapshot_path 是必填项")
    if path.is_symlink():
        raise DispatcherError("invalid_input", "requirement_snapshot_path 必须是现有普通文件")
    resolved = path.resolve()
    specs_root = (docs_root.resolve() / "specs").resolve()
    attachments_root = (docs_root.resolve() / "attachments" / assignment.task.task_id).resolve()
    if not resolved.is_file() or not is_within(resolved, specs_root):
        raise DispatcherError("invalid_input", "requirement_snapshot_path 必须位于 docs/engineering/specs 目录")
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


def validate_gitnexus_report_path(config: Config, assignment: Assignment, docs_root: Path) -> None:
    if assignment.gitnexus_report_path is None:
        return
    report_path = assignment.gitnexus_report_path.resolve()
    report_root = (docs_root.resolve() / "research").resolve()
    if not is_within(report_path, report_root) or not report_path.is_file():
        raise DispatcherError("invalid_input", "gitnexus_report_path 必须位于 docs/engineering/research 下的现有文件")


def staged_docs_root(assignment: Assignment) -> Path:
    """任务制品在源仓库 .runtime 下的暂存根，与任务工作区内的 docs/engineering 同构。"""
    return (assignment.repository_path.resolve() / STAGING_DIRECTORY / assignment.task.task_id / "docs" / "engineering").resolve()


def validate_candidate_eligibility(assignment: Assignment, flow: DispatchFlow) -> None:
    """候选资格是进入任意分发流程的硬门槛；仓库映射兜底不得替代它。"""
    if not flow.requires_candidate_eligibility:
        return
    if not assignment.candidate_evidence_provided:
        raise DispatcherError("candidate_evidence_missing", "任务缺少完整候选资格交接字段（含 candidate_jql）")
    if assignment.candidate_eligible is not True or not assignment.candidate_eligibility_reason or not assignment.candidate_jql:
        raise DispatcherError(
            "candidate_ineligible",
            "任务缺少已确认的候选资格；查询失败或资格未确认时不得进入仓库映射兜底",
        )
    if flow.requires_reference_plan_candidate:
        plan = assignment.reference_plan if assignment.reference_plan_source == "task" else assignment.parent_reference_plan
        if assignment.reference_plan_source not in {"task", "parent"} or not plan:
            raise DispatcherError(
                "proposal_candidate_ineligible",
                "proposal 任务必须有任务或父任务参考方案，父子参考方案均为空时不得分发",
            )
    if assignment.reference_plan_source == "task" and not assignment.reference_plan:
        raise DispatcherError("invalid_input", "reference_plan_source=task 时必须提供 reference_plan")
    if assignment.reference_plan_source == "parent" and not assignment.parent_reference_plan:
        raise DispatcherError("invalid_input", "reference_plan_source=parent 时必须提供 parent_reference_plan")
    if assignment.reference_plan_source == "none" and (assignment.reference_plan or assignment.parent_reference_plan):
        raise DispatcherError("invalid_input", "reference_plan_source=none 时不得提供参考方案字段")


def validate_assignment(config: Config, assignment: Assignment, repositories: Mapping[str, Repository]) -> None:
    repository = repositories[assignment.repository]
    if repository.path.resolve() != assignment.repository_path.resolve():
        raise DispatcherError("invalid_input", f"repository_path 与配置中的仓库不一致：{assignment.repository}")
    project = config.projects.get(assignment.repository)
    if assignment.tenant_slug != "legacy":
        if project is None or project.tenants.get(assignment.tenant) != assignment.tenant_slug:
            raise DispatcherError("invalid_input", f"租户不属于项目配置：{assignment.repository}/{assignment.tenant}")
    expected_task_url = task_url_for(config.task_url_template, assignment.task.task_id)
    if assignment.task.task_url != expected_task_url:
        raise DispatcherError("invalid_input", "task_url 必须由 task_url_template 生成")
    flow = config.flow_for(assignment.dispatch_flow)
    validate_candidate_eligibility(assignment, flow)
    docs_root = staged_docs_root(assignment)
    validate_gitnexus_report_path(config, assignment, docs_root)
    if assignment.requirement_snapshot_path is None:
        if flow.requires_snapshot:
            raise DispatcherError("invalid_input", f"流程 {flow.name} 要求提供 requirement_snapshot_path")
    else:
        validate_requirement_snapshot_path(config, assignment, docs_root)
    if assignment.base_branch is None:
        return
    if assignment.base_branch not in config.branches_for(repository.name):
        raise DispatcherError("invalid_branch", f"{assignment.base_branch} 不在 {repository.name} 的配置白名单中")
    if config.validate_branch and not branch_exists(repository, assignment.base_branch):
        raise DispatcherError("branch_not_found", f"{assignment.base_branch} 在 {repository.name} 中不存在")


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

    def _entry(self, state_key: str, value: Mapping[str, object]) -> StateEntry:
        try:
            key_task_id, key_tenant_slug, key_flow = parse_state_key(state_key)
            value_task_id = value.get("task_id")
            task_id = require_task_id(value_task_id) if value_task_id is not None else key_task_id
            value_tenant_slug = value.get("tenant_slug")
            tenant_slug = (
                require_tenant_slug(value_tenant_slug)
                if value_tenant_slug is not None
                else key_tenant_slug
            )
            value_flow = value.get("dispatch_flow")
            dispatch_flow = (
                require_dispatch_flow(value_flow, "state.dispatch_flow")
                if value_flow is not None
                else key_flow or "complete"
            )
        except DispatcherError as error:
            raise DispatcherError("state_unreadable", "state.json 任务 identity 不受支持") from error
        if task_id != key_task_id or tenant_slug != key_tenant_slug or (key_flow is not None and dispatch_flow != key_flow):
            raise DispatcherError("state_unreadable", "state.json 任务 identity 与状态键不一致")
        assignment_id = legacy_state_key(task_id, tenant_slug)
        stored_assignment_id = value.get("assignment_id")
        if stored_assignment_id is not None and stored_assignment_id != assignment_id:
            raise DispatcherError("state_unreadable", "state.json assignment_id 与状态键不一致")
        return StateEntry(state_key, value, task_id, tenant_slug, dispatch_flow, assignment_id)

    def _entries(self, tasks: Mapping[str, object]) -> tuple[StateEntry, ...]:
        return tuple(
            self._entry(state_key, value)
            for state_key, value in tasks.items()
            if isinstance(value, Mapping)
        )

    @staticmethod
    def _state_values_equivalent(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
        ignored = {"task_id", "tenant_slug", "assignment_id", "dispatch_flow", "state_key", "updated_at", "dispatched_at"}
        return {
            key: value for key, value in left.items() if key not in ignored
        } == {
            key: value for key, value in right.items() if key not in ignored
        }

    @classmethod
    def _choose_entry(cls, entries: tuple[StateEntry, ...]) -> StateEntry | None:
        if not entries:
            return None
        if len(entries) == 1:
            return entries[0]
        canonical = state_key_for(entries[0].task_id, entries[0].tenant_slug, entries[0].dispatch_flow)
        canonical_entry = next((entry for entry in entries if entry.state_key == canonical), None)
        if canonical_entry is not None and all(
            cls._state_values_equivalent(entry.value, canonical_entry.value) for entry in entries
        ):
            return canonical_entry
        raise DispatcherError("state_conflict", "同一任务流程存在冲突的新旧状态，请人工复位")

    def _resolved_entries(self, tasks: Mapping[str, object]) -> tuple[StateEntry, ...]:
        grouped: dict[tuple[str, str, str], list[StateEntry]] = {}
        for entry in self._entries(tasks):
            grouped.setdefault((entry.task_id, entry.tenant_slug, entry.dispatch_flow), []).append(entry)
        return tuple(
            entry
            for values in grouped.values()
            for entry in (self._choose_entry(tuple(values)),)
            if entry is not None
        )

    def resolve_entry(
        self,
        tasks: Mapping[str, object],
        task_id: str,
        tenant_slug: str | None = "legacy",
        dispatch_flow: str | None = None,
    ) -> StateEntry | None:
        task = require_task_id(task_id)
        tenant = require_tenant_slug(tenant_slug) if tenant_slug is not None else None
        flow = require_dispatch_flow(dispatch_flow) if dispatch_flow is not None else None
        entries = tuple(
            entry
            for entry in self._entries(tasks)
            if entry.task_id == task
            and (tenant is None or entry.tenant_slug == tenant)
            and (flow is None or entry.dispatch_flow == flow)
        )
        if tenant is None and any(entry.tenant_slug != "legacy" for entry in entries):
            raise DispatcherError("assignment_ambiguous", f"任务 {task} 存在租户状态，请指定 --tenant-slug")
        if flow is not None:
            tenant_matches = {entry.tenant_slug for entry in entries}
            if len(tenant_matches) > 1:
                raise DispatcherError("assignment_ambiguous", f"任务 {task} 存在多个租户状态，请指定 --tenant-slug")
            return self._choose_entry(entries)
        by_identity: dict[tuple[str, str], list[StateEntry]] = {}
        for entry in entries:
            by_identity.setdefault((entry.tenant_slug, entry.dispatch_flow), []).append(entry)
        if tenant is not None:
            if len(by_identity) > 1:
                raise DispatcherError("assignment_ambiguous", f"任务 {task}/{tenant} 存在多个流程状态，请使用 --dispatch-flow")
        elif len(by_identity) > 1:
            if len({entry.tenant_slug for entry in entries}) == 1:
                raise DispatcherError("assignment_ambiguous", f"任务 {task} 存在多个流程状态，请使用 --dispatch-flow")
            raise DispatcherError("assignment_ambiguous", f"任务 {task} 存在多个租户或流程状态，请指定筛选条件")
        return self._choose_entry(tuple(next(iter(by_identity.values()), ())))

    def _matching_entries(
        self,
        tasks: Mapping[str, object],
        entry: StateEntry,
    ) -> tuple[StateEntry, ...]:
        return tuple(
            candidate
            for candidate in self._entries(tasks)
            if (
                candidate.task_id == entry.task_id
                and candidate.tenant_slug == entry.tenant_slug
                and candidate.dispatch_flow == entry.dispatch_flow
            )
        )

    def terminal_records_for_worktree(self, worktree_id: str) -> tuple[tuple[str, Mapping[str, object]], ...]:
        """只读返回 state 中与物理工作区关联的终端证据，跨流程但不合并状态。"""
        tasks = self.snapshot()["tasks"]
        assert isinstance(tasks, dict)
        records: list[tuple[str, Mapping[str, object]]] = []
        for entry in self._resolved_entries(tasks):
            if (
                entry.value.get("worktree_id") == worktree_id
                and entry.value.get("terminal_handle")
            ):
                records.append((entry.state_key, entry.value))
        return tuple(records)

    def status(self, task_id: str, tenant_slug: str = "legacy", dispatch_flow: str | None = "complete") -> str | None:
        tasks = self.snapshot()["tasks"]
        assert isinstance(tasks, dict)
        entry = self.resolve_entry(tasks, task_id, tenant_slug, dispatch_flow)
        if entry is None:
            return None
        status = entry.value.get("status")
        return status if isinstance(status, str) else None

    def state_view(self, dispatch_flow: str | None = None) -> dict[str, object]:
        flow = require_dispatch_flow(dispatch_flow) if dispatch_flow is not None else None
        state = self.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        entries = self._resolved_entries(tasks)
        return {
            "version": 1,
            "tasks": {
                entry.state_key: {
                    **entry.value,
                    "task_id": entry.task_id,
                    "tenant": entry.value.get("tenant", entry.tenant_slug),
                    "tenant_slug": entry.tenant_slug,
                    "assignment_id": entry.assignment_id,
                    "dispatch_flow": entry.dispatch_flow,
                    "state_key": entry.state_key,
                }
                for entry in entries
                if flow is None or entry.dispatch_flow == flow
            },
        }

    def mark_launching(self, assignment: Assignment) -> None:
        state = self.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        state_key = state_key_for(assignment.task.task_id, assignment.tenant_slug, assignment.dispatch_flow)
        next_tasks = {
            **tasks,
            state_key: {
                "task_id": assignment.task.task_id,
                "repository": assignment.repository,
                "repository_path": assignment.repository_path.resolve().as_posix(),
                "worktree_path": None,
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
                "candidate_eligible": assignment.candidate_eligible,
                "candidate_eligibility_reason": assignment.candidate_eligibility_reason,
                "candidate_jql": assignment.candidate_jql,
                "reference_plan_source": assignment.reference_plan_source,
                "parent_reference_plan": assignment.parent_reference_plan,
                "gitnexus_report_path": assignment.gitnexus_report_path.as_posix() if assignment.gitnexus_report_path else None,
                "requirement_snapshot_path": assignment.requirement_snapshot_path.as_posix() if assignment.requirement_snapshot_path else None,
                "dispatch_flow": assignment.dispatch_flow,
                "worktree_name": worktree_name_for(assignment),
                "status": "launching",
                "dispatch_state": "pending",
                "updated_at": utc_now(),
            },
        }
        atomic_write_json(self.state_file, {"version": 1, "tasks": next_tasks})

    def _update_launching(self, assignment: Assignment, **fields: object) -> None:
        """启动中记录的局部更新；不在 launching 状态时不写，避免覆盖已分发结果。"""
        state = self.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        state_key = state_key_for(assignment.task.task_id, assignment.tenant_slug, assignment.dispatch_flow)
        existing = tasks.get(state_key)
        if not isinstance(existing, Mapping) or existing.get("status") != "launching":
            raise DispatcherError("state_unreadable", "任务启动状态缺失或不处于 launching")
        next_tasks = {
            **tasks,
            state_key: {**existing, **fields, "updated_at": utc_now()},
        }
        atomic_write_json(self.state_file, {"version": 1, "tasks": next_tasks})

    def mark_worktree_prepared(self, assignment: Assignment, worktree: OrcaWorktree) -> None:
        self._update_launching(
            assignment,
            worktree_path=worktree.path.as_posix(),
            worktree_id=worktree.worktree_id,
            worktree_comment=worktree.comment,
            dispatch_state="worktree_ready",
        )

    def mark_terminal_created(self, assignment: Assignment, terminal_handle: str) -> None:
        self._update_launching(
            assignment,
            terminal_handle=terminal_handle,
            dispatch_state="terminal_ready",
        )

    def mark_send_started(self, assignment: Assignment, initial_prompt_digest: str) -> None:
        """发送前落盘首次投递摘要，避免把完整任务文本写入状态。"""
        self._update_launching(
            assignment,
            initial_prompt_digest=initial_prompt_digest,
            dispatch_state="sending",
            send_started_at=utc_now(),
        )

    def mark_dispatched(self, record: DispatchRecord) -> None:
        state = self.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        state_key = state_key_for(
            record.assignment.task.task_id,
            record.assignment.tenant_slug,
            record.assignment.dispatch_flow,
        )
        existing = tasks.get(state_key)
        if not isinstance(existing, Mapping):
            raise DispatcherError("state_unreadable", "任务启动状态缺失")
        assignment = record.assignment
        next_tasks = {
            **tasks,
            state_key: {
                **existing,
                "task_id": assignment.task.task_id,
                "tenant_slug": assignment.tenant_slug,
                "assignment_id": assignment.assignment_id,
                "dispatch_flow": assignment.dispatch_flow,
                "status": "dispatched",
                "worktree_path": record.worktree.path.as_posix(),
                "worktree_id": record.worktree.worktree_id,
                "terminal_handle": record.terminal_handle,
                "initial_prompt_digest": record.initial_prompt_digest,
                "duplicate_of_state_key": record.duplicate_of_state_key,
                "send_request_id": record.receipt.request_id,
                "send_accepted": record.receipt.accepted,
                "send_stages": list(record.receipt.stages),
                "send_observation": record.receipt.observation,
                "send_process_incarnation": record.receipt.process_incarnation,
                "dispatch_state": "turn_started" if record.receipt.turn_started else "input_accepted",
                "candidate_eligible": assignment.candidate_eligible,
                "candidate_eligibility_reason": assignment.candidate_eligibility_reason,
                "candidate_jql": assignment.candidate_jql,
                "reference_plan_source": assignment.reference_plan_source,
                "parent_reference_plan": assignment.parent_reference_plan,
                "gitnexus_report_path": assignment.gitnexus_report_path.as_posix() if assignment.gitnexus_report_path else None,
                "requirement_snapshot_path": assignment.requirement_snapshot_path.as_posix() if assignment.requirement_snapshot_path else None,
                "dispatched_at": utc_now(),
                "updated_at": utc_now(),
                "recovery_history": [],
            },
        }
        atomic_write_json(self.state_file, {"version": 1, "tasks": next_tasks})

    def mark_recovered(self, state_key: str, dispatch_state: str, result: str, **fields: object) -> None:
        state = self.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        existing = tasks.get(state_key)
        if not isinstance(existing, Mapping):
            raise DispatcherError("state_unreadable", "待恢复任务状态缺失")
        raw_entry = self._entry(state_key, existing)
        entry = self.resolve_entry(
            tasks,
            raw_entry.task_id,
            raw_entry.tenant_slug,
            raw_entry.dispatch_flow,
        )
        if entry is None:
            raise DispatcherError("state_unreadable", "待恢复任务状态缺失")
        previous_history = entry.value.get("recovery_history")
        history = list(previous_history) if isinstance(previous_history, list) else []
        event = {"at": utc_now(), "result": result, "dispatch_state": dispatch_state, **fields}
        updated = {
            candidate.state_key: {
                **candidate.value,
                **fields,
                "task_id": entry.task_id,
                "tenant_slug": entry.tenant_slug,
                "assignment_id": entry.assignment_id,
                "dispatch_flow": entry.dispatch_flow,
                "status": "dispatched",
                "dispatch_state": dispatch_state,
                "updated_at": event["at"],
                "recovery_history": [*history, event],
            }
            for candidate in self._matching_entries(tasks, entry)
        }
        atomic_write_json(self.state_file, {"version": 1, "tasks": {**tasks, **updated}})

    def mark_requires_manual_reset(
        self,
        assignment: Assignment | str,
        reason: str,
        tenant_slug: str = "legacy",
        dispatch_flow: str = "complete",
    ) -> None:
        state = self.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        if isinstance(assignment, Assignment):
            entry = self.resolve_entry(
                tasks,
                assignment.task.task_id,
                assignment.tenant_slug,
                assignment.dispatch_flow,
            )
        elif assignment in tasks and isinstance(tasks[assignment], Mapping):
            entry = self._entry(assignment, tasks[assignment])
        else:
            entry = self.resolve_entry(tasks, assignment, tenant_slug, dispatch_flow)
        if entry is None:
            raise DispatcherError("state_unreadable", "待恢复任务状态缺失")
        entry = self.resolve_entry(
            tasks,
            entry.task_id,
            entry.tenant_slug,
            entry.dispatch_flow,
        )
        if entry is None:
            raise DispatcherError("state_unreadable", "待恢复任务状态缺失")
        event = {"at": utc_now(), "result": "requires_manual_reset", "reason": reason}
        updated = {}
        for candidate in self._matching_entries(tasks, entry):
            previous_history = candidate.value.get("recovery_history")
            history = list(previous_history) if isinstance(previous_history, list) else []
            updated[candidate.state_key] = {
                **candidate.value,
                "status": "requires_manual_reset",
                "updated_at": event["at"],
                "recovery_history": [*history, event],
            }
        atomic_write_json(self.state_file, {"version": 1, "tasks": {**tasks, **updated}})

    def reset_entry(
        self,
        task_id: str,
        force_unlock: bool,
        force: bool = False,
        tenant_slug: str | None = None,
        dispatch_flow: str | None = None,
    ) -> StateEntry | None:
        with self.launch_lock(force_unlock):
            state = self.snapshot()
            tasks = state["tasks"]
            assert isinstance(tasks, dict)
            entry = self.resolve_entry(tasks, task_id, tenant_slug, dispatch_flow)
            if entry is None:
                return None
            status = entry.value.get("status")
            if status in {"launching", "requires_manual_reset"}:
                pass
            elif status == "dispatched" and force:
                pass
            else:
                raise DispatcherError(
                    "reset_not_allowed",
                    "仅允许复位 launching 或 requires_manual_reset 状态；复位 dispatched 状态需使用 --force",
                )
            matching_keys = {
                candidate.state_key for candidate in self._matching_entries(tasks, entry)
            }
            next_tasks = {
                key: value for key, value in tasks.items() if key not in matching_keys
            }
            atomic_write_json(self.state_file, {"version": 1, "tasks": next_tasks})
            return entry

    def reset(
        self,
        task_id: str,
        force_unlock: bool,
        force: bool = False,
        tenant_slug: str | None = None,
        dispatch_flow: str | None = None,
    ) -> bool:
        return self.reset_entry(task_id, force_unlock, force, tenant_slug, dispatch_flow) is not None

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
        # ponytail：state.json 是整文件读改写；保留全局锁，未来改为事务化状态存储后再支持跨进程按流程并行写入。
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
        child_env = child_environment()
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
            raise DispatcherError(
                "orca_timeout",
                f"Orca CLI 调用超时：{' '.join(arguments)}",
                orca_code="runtime_timeout",
            ) from error
        except OSError as error:
            raise DispatcherError("orca_unavailable", f"无法调用 Orca CLI：{error}") from error
        stdout = completed.stdout.strip()
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise DispatcherError(
                "orca_invalid_json", f"Orca CLI 未返回 JSON：{stdout[:200]}", orca_code="runtime_unavailable"
            ) from error
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            error = payload.get("error") if isinstance(payload, Mapping) else None
            safe_message = orca_error_message(error)
            error_code = error.get("code") if isinstance(error, Mapping) else None
            if completed.stderr.strip():
                retry_log(f"orca command stderr received code={error_code or 'unknown'}")
            raise DispatcherError(
                "orca_command_failed", safe_message,
                orca_code=error_code if isinstance(error_code, str) else None,
            )
        if completed.returncode != 0 and not allow_nonzero:
            raise DispatcherError("orca_command_failed", completed.stderr.strip() or "Orca CLI 返回非零退出码")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少对象 result")
        return result

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

    @staticmethod
    def _worktree_from_payload(value: Mapping[str, Any]) -> OrcaWorktree:
        worktree_id = require_text(value.get("id"), "Orca worktree.id")
        repository_id = value.get("repoId")
        if not isinstance(repository_id, str) or not repository_id.strip():
            prefix, separator, _ = worktree_id.partition("::")
            repository_id = prefix if separator else None
        return OrcaWorktree(
            worktree_id=worktree_id,
            path=Path(require_text(value.get("path"), "Orca worktree.path")).resolve(),
            repository_id=repository_id.strip() if isinstance(repository_id, str) else None,
            base_branch=(
                value["baseRef"].strip()
                if isinstance(value.get("baseRef"), str) and value["baseRef"].strip()
                else None
            ),
            comment=(
                value["comment"].strip()
                if isinstance(value.get("comment"), str) and value["comment"].strip()
                else None
            ),
        )

    def worktree_set_in_progress(self, worktree_path: Path) -> None:
        self._call(
            "worktree",
            "set",
            "--worktree",
            f"path:{worktree_path.resolve().as_posix()}",
            "--workspace-status",
            "in-progress",
        )

    def worktrees(self, repository_id: str) -> tuple[OrcaWorktree, ...]:
        result = self._call("worktree", "list", "--repo", f"id:{repository_id}")
        values = result.get("worktrees")
        if not isinstance(values, list):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 worktrees 列表")
        if result.get("truncated") or result.get("totalCount", len(values)) > len(values):
            raise DispatcherError("orca_incomplete_list", "Orca worktrees 列表被截断，不能据此确认工作区不存在")
        worktrees: list[OrcaWorktree] = []
        for value in values:
            if not isinstance(value, Mapping):
                raise DispatcherError("orca_invalid_json", "Orca worktree 项格式不合法")
            worktrees.append(self._worktree_from_payload(value))
        return tuple(worktrees)

    def terminals(self, worktree: OrcaWorktree) -> tuple[OrcaTerminal, ...]:
        result = self._call("terminal", "list", "--worktree", worktree.selector)
        values = result.get("terminals")
        if not isinstance(values, list):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 terminals 列表")
        if result.get("truncated") or result.get("totalCount", len(values)) > len(values):
            raise DispatcherError("orca_incomplete_list", "Orca terminals 列表被截断，不能确认终端唯一性")
        terminals: list[OrcaTerminal] = []
        for value in values:
            if not isinstance(value, Mapping):
                raise DispatcherError("orca_invalid_json", "Orca terminal 项格式不合法")
            terminals.append(self._terminal_from_payload(value))
        return tuple(terminals)

    @staticmethod
    def _terminal_from_payload(value: Mapping[str, Any]) -> OrcaTerminal:
        agent_identity = value.get("agentIdentity")
        return OrcaTerminal(
            handle=require_text(value.get("handle"), "Orca terminal.handle"),
            worktree_id=require_text(value.get("worktreeId"), "Orca terminal.worktreeId"),
            agent_identity=(
                agent_identity.strip()
                if isinstance(agent_identity, str) and agent_identity.strip()
                else None
            ),
            connected=value.get("connected") is True,
            writable=value.get("writable") is True,
        )

    def terminal_show(self, handle: str) -> OrcaTerminal:
        result = self._call("terminal", "show", "--terminal", handle)
        terminal = result.get("terminal")
        if not isinstance(terminal, Mapping):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 terminal 对象")
        return self._terminal_from_payload(terminal)

    @staticmethod
    def _terminal_handle(result: Mapping[str, Any]) -> str:
        """兼容不同版本的创建回执结构，取其中的 terminal handle。"""
        candidates = (result, result.get("terminal"), result.get("createdTerminal"), result.get("split"))
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                handle = candidate.get("handle")
                if isinstance(handle, str) and handle.strip():
                    return handle.strip()
        raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 terminal handle")

    def terminal_create(self, worktree: OrcaWorktree, title: str, command: str) -> str:
        result = self._call(
            "terminal", "create",
            "--worktree", worktree.selector,
            "--title", title,
            "--command", command,
        )
        return self._terminal_handle(result)

    def terminal_wait(self, handle: str, timeout_ms: int) -> Mapping[str, Any]:
        result = self._call(
            "terminal", "wait", "--terminal", handle, "--for", "tui-idle",
            "--timeout-ms", str(timeout_ms),
            timeout_seconds=max(ORCA_COMMAND_TIMEOUT_SECONDS, timeout_ms / 1000 + 15),
        )
        wait = result.get("wait")
        return wait if isinstance(wait, Mapping) else {}

    def terminal_send(self, handle: str, text: str) -> Mapping[str, Any]:
        """普通投递；--wait-submit 只观察已接受的输入，不会因此重发。"""
        return self._call(
            "terminal", "send", "--terminal", handle,
            "--text", text, "--enter",
            "--wait-submit", str(SEND_OBSERVE_SECONDS),
            timeout_seconds=ORCA_SEND_TIMEOUT_SECONDS,
        )

    def worktree_remove(self, worktree: OrcaWorktree) -> None:
        self._call("worktree", "rm", "--worktree", worktree.selector)

    def repo_show_by_path(self, path: Path) -> str | None:
        """按路径查已注册的 Orca 仓库；未注册时返回 None。"""
        key = os.path.normcase(os.path.normpath(str(path.resolve())))
        return self.repo_ids().get(key)

    def repo_base_branch(self, repository_id: str) -> str | None:
        result = self._call("repo", "show", "--repo", f"id:{repository_id}")
        repository = result.get("repo")
        if not isinstance(repository, Mapping):
            raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 repo 对象")
        value = repository.get("worktreeBaseRef")
        return require_optional_text(value, "Orca repo.worktreeBaseRef")

    def worktree_create(
        self,
        name: str,
        repository_id: str,
        base_branch: str | None,
        comment: str,
    ) -> OrcaWorktree:
        """只创建工作区；开发会话由后续的 terminal create 显式启动。"""
        arguments = [
            "worktree", "create", "--name", name, "--repo", f"id:{repository_id}", "--no-parent",
            "--comment", comment,
        ]
        if base_branch:
            arguments.extend(["--base-branch", base_branch])
        try:
            result = self._call(*arguments, timeout_seconds=ORCA_WORKTREE_TIMEOUT_SECONDS)
        except DispatcherError as error:
            if error.code == "orca_invalid_json":
                raise DispatcherError(
                    error.code, error.message, orca_code="runtime_unavailable"
                ) from error
            raise
        worktree = result.get("worktree")
        if not isinstance(worktree, Mapping):
            raise DispatcherError(
                "orca_invalid_json", "Orca CLI 结果缺少 worktree 对象", orca_code="runtime_unavailable"
            )
        try:
            return self._worktree_from_payload(worktree)
        except DispatcherError as error:
            raise DispatcherError(
                "orca_invalid_json", error.message, orca_code="runtime_unavailable"
            ) from error

def parse_send_receipt(result: Mapping[str, Any]) -> SendReceipt:
    """只读回执本身判定接受与起步，不把顶层 ok 当作接受证明。"""
    send = result.get("send")
    if not isinstance(send, Mapping):
        raise DispatcherError("orca_invalid_json", "Orca CLI 结果缺少 send 对象")
    prompt = send.get("prompt")
    prompt = prompt if isinstance(prompt, Mapping) else {}
    stages = prompt.get("stages")
    return SendReceipt(
        accepted=send.get("accepted") is True,
        request_id=require_optional_text(prompt.get("requestId"), "Orca send.prompt.requestId"),
        stages=tuple(str(stage) for stage in stages) if isinstance(stages, list) else (),
        observation=require_optional_text(prompt.get("observation"), "Orca send.prompt.observation"),
        process_incarnation=require_optional_text(
            prompt.get("processIncarnation"), "Orca send.prompt.processIncarnation"
        ),
    )


def initial_prompt_digest(prompt: str) -> str:
    """保存首次投递内容的摘要，不把任务正文写入运行状态。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def receipt_from_state(value: Mapping[str, object]) -> SendReceipt:
    stages = value.get("send_stages")
    return SendReceipt(
        accepted=value.get("send_accepted") is True,
        request_id=value.get("send_request_id") if isinstance(value.get("send_request_id"), str) else None,
        stages=tuple(str(stage) for stage in stages) if isinstance(stages, list) else (),
        observation=value.get("send_observation") if isinstance(value.get("send_observation"), str) else None,
        process_incarnation=(
            value.get("send_process_incarnation")
            if isinstance(value.get("send_process_incarnation"), str)
            else None
        ),
    )


def duplicate_terminal_for_prompt(
    store: StateStore,
    worktree: OrcaWorktree,
    active_terminals: Iterable[OrcaTerminal],
    digest: str,
) -> tuple[str, Mapping[str, object], OrcaTerminal] | None:
    active_by_handle = {terminal.handle: terminal for terminal in active_terminals}
    for state_key, value in store.terminal_records_for_worktree(worktree.worktree_id):
        if value.get("initial_prompt_digest") != digest:
            continue
        handle = value.get("terminal_handle")
        if isinstance(handle, str) and handle in active_by_handle:
            return state_key, value, active_by_handle[handle]
    return None


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


def safe_task_context_value(value: str) -> str:
    return " ".join(value.split()).replace("`", "'")


def task_context_values(assignment: Assignment) -> Mapping[str, str]:
    source_assignee = assignment.source_assignee
    if (
        not source_assignee
        and assignment.source_task_id in (None, assignment.task.task_id)
        and assignment.parent_task_id != assignment.task.task_id
    ):
        source_assignee = assignment.assignee
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
        "source_assignee": safe_task_context_value(source_assignee or ""),
    }


def command_for(config: Config, assignment: Assignment, recovery: bool = False) -> str:
    template = config.flow_for(assignment.dispatch_flow).command_template
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


def assignment_result(assignment: Assignment, status: str, **extra: object) -> dict[str, object]:
    return {
        "task_id": assignment.task.task_id,
        "tenant": assignment.tenant,
        "tenant_slug": assignment.tenant_slug,
        "assignment_id": assignment.assignment_id,
        "dispatch_flow": assignment.dispatch_flow,
        "status": status,
        **extra,
    }


def assignment_history(assignment: Assignment, result: str, **extra: object) -> dict[str, object]:
    return {
        "task": assignment.task.task_id,
        "tenant_slug": assignment.tenant_slug,
        "assignment_id": assignment.assignment_id,
        "dispatch_flow": assignment.dispatch_flow,
        "result": result,
        **extra,
    }


def mark_manual_reset_safely(store: StateStore, assignment: Assignment, reason: str) -> str | None:
    try:
        store.mark_requires_manual_reset(assignment, reason)
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
    assignment: Assignment,
    worktree_path: Path,
) -> str | None:
    try:
        orca.worktree_set_in_progress(worktree_path)
    except DispatcherError as error:
        append_history_safely(store, assignment_history(
            assignment,
            "workspace_status_failed",
            worktree_path=worktree_path.as_posix(),
            reason=error.message,
        ))
        return error.message
    return None


ORCA_WORKTREE_TIMEOUT_SECONDS = 600
ORCA_WORKTREE_POLL_SECONDS = 5
ORCA_TRANSPORT_ERRORS = frozenset({"runtime_unavailable", "runtime_timeout"})
WORKTREE_RECEIPT_RECOVERED = "创建回执未收到；已只读确认原名工作区归属，未重复创建"
TERMINAL_AGENT = "claude"
TERMINAL_READY_TIMEOUT_MS = 120_000
SEND_OBSERVE_SECONDS = 10
TURN_START_UNOBSERVED = "任务文本已接受，但未观察到开发会话起步"
ORCA_SEND_TIMEOUT_SECONDS = SEND_OBSERVE_SECONDS + 30
STAGING_DIRECTORY = ".runtime"


def ensure_repository_registered(orca: OrcaClient, repository: Repository) -> str:
    """按路径确认源仓库已在 Orca 注册；未注册时补注册一次。"""
    repository_id = orca.repo_show_by_path(repository.path)
    if repository_id is None:
        orca.repo_add(repository)
        repository_id = orca.repo_show_by_path(repository.path)
    if repository_id is None:
        raise DispatcherError("orca_repository_not_registered", f"Orca 未注册源仓库：{repository.path.as_posix()}")
    return repository_id


def git_user_name(repository: Repository) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository.path), "config", "user.name"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.stdout.strip()


def pinyin_username(name: str) -> str:
    """中文用户名转拼音；缺少 pypinyin 时原样返回并记录告警。"""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        retry_log("pypinyin 不可用，分支用户名使用 git user.name 原样值")
        return name
    return "".join(lazy_pinyin(name)).lower()


def worktree_name_for(assignment: Assignment) -> str:
    """Orca 的 --name 同时决定工作区目录名与初始分支名。"""
    return f"{assignment.task.task_id}-{assignment.worktree_slug}" if assignment.worktree_slug else assignment.task.task_id


def worktree_branch_for(assignment: Assignment, repository: Repository) -> str:
    """目标分支：<用户名>/<工作区名>；用户名转拼音并过滤 Git 分支名不接受的字符。"""
    raw = pinyin_username(git_user_name(repository)) or repository.name
    user = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.") or repository.name
    return f"{user}/{worktree_name_for(assignment)}"


def worktree_comment_for(assignment: Assignment) -> str:
    """Orca comment 是物理工作区的稳定归属标记，不能由 flow 改写名称。"""
    return f"orca-task-dispatcher:{assignment.assignment_id}"


def git_current_branch(worktree_path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "--show-current"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=child_environment(),
    )
    if completed.returncode != 0:
        raise DispatcherError("worktree_branch_unreadable", completed.stderr.strip() or "无法读取工作区分支")
    return completed.stdout.strip()


def worktree_is_clean(worktree_path: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=child_environment(),
    )
    if completed.returncode != 0:
        raise DispatcherError("worktree_status_unreadable", completed.stderr.strip() or "无法检查工作区状态")
    return not completed.stdout.strip()


def active_agent_terminals(
    config: Config, orca: OrcaClient, worktree: OrcaWorktree
) -> tuple[OrcaTerminal, ...]:
    """返回活动 Claude 会话；查询失败按异常交给调用方决定是否停手。"""
    terminals = retry_read(config, lambda: orca.terminals(worktree))
    return tuple(
        terminal
        for terminal in terminals
        if terminal.worktree_id == worktree.worktree_id
        and terminal.agent_identity == TERMINAL_AGENT
        and terminal.connected
        and terminal.writable
    )


def has_live_agent_terminal(config: Config, orca: OrcaClient, worktree: OrcaWorktree) -> bool:
    """后缀清理保留原保护，不能把不可写或暂时断连的 Claude 当作可删除。"""
    try:
        terminals = retry_read(config, lambda: orca.terminals(worktree))
    except DispatcherError:
        return True
    return any(
        terminal.worktree_id == worktree.worktree_id and terminal.agent_identity == TERMINAL_AGENT
        for terminal in terminals
    )


def require_unique_worktree_names(config: Config, assignments: Iterable[Assignment]) -> None:
    seen: dict[tuple[str, str], Assignment] = {}
    for assignment in assignments:
        if not config.flow_for(assignment.dispatch_flow).requires_worktree:
            continue
        key = (
            os.path.normcase(os.path.normpath(str(assignment.repository_path.resolve()))),
            worktree_name_for(assignment),
        )
        previous = seen.get(key)
        if previous is not None and previous.assignment_id != assignment.assignment_id:
            raise DispatcherError(
                "invalid_input",
                "不同任务或租户不能使用相同 worktree_name；请显式提供不同 worktree_slug",
            )
        seen[key] = assignment


def dev_spec_gen_script_path() -> Path:
    """dev-spec-gen 统一 worktree CLI 的安装路径，与 Claude 技能安装约定一致。"""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return (Path(base) / "skills" / "dev-spec-gen" / "scripts" / "worktree.py").resolve()


def sync_worktree_files(repository: Repository, worktree_path: Path) -> tuple[str, ...]:
    """复用 dev-spec-gen 的同步能力：未托管内容与 IDE 配置。"""
    script = dev_spec_gen_script_path()
    if not script.is_file():
        raise DispatcherError("dev_spec_gen_missing", f"未找到 dev-spec-gen worktree CLI：{script.as_posix()}")
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--sync-only",
            "--worktree",
            str(worktree_path),
            "--workspace",
            str(repository.path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=child_environment(),
    )
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as error:
        raise DispatcherError("worktree_sync_failed", f"worktree sync 未返回 JSON：{completed.stdout.strip()[:200]}") from error
    if not isinstance(payload, Mapping) or payload.get("status") != "success":
        detail = payload.get("error") if isinstance(payload, Mapping) else None
        raise DispatcherError("worktree_sync_failed", str(detail or "worktree sync 失败"))
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        return ()
    return tuple(str(warning) for warning in warnings)


def rename_worktree_branch(worktree_path: Path, branch: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "-m", branch],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise DispatcherError("worktree_branch_rename_failed", completed.stderr.strip() or "重命名目标分支失败")


def base_branch_matches(worktree: OrcaWorktree, base_branch: str | None) -> bool:
    """Orca 记录的 baseRef 可能是裸引用，也可能是 refs/heads/ 或 refs/remotes/ 全名。"""
    if base_branch is None:
        return True
    return worktree.base_branch in {
        base_branch,
        f"refs/heads/{base_branch}",
        f"refs/remotes/{base_branch}",
    }


def validate_task_worktree(
    worktree: OrcaWorktree,
    assignment: Assignment,
    repository_id: str,
) -> None:
    expected_name = worktree_name_for(assignment)
    if worktree.repository_id != repository_id:
        raise DispatcherError("worktree_identity_mismatch", "Orca 工作区不属于目标仓库")
    if worktree.path.name != expected_name:
        raise DispatcherError("worktree_name_mismatch", "Orca 返回的工作区名称不是任务稳定名称")
    if worktree.comment != worktree_comment_for(assignment):
        raise DispatcherError(
            "worktree_identity_mismatch",
            "工作区归属标记与任务或租户不一致（可能是旧版工具创建的工作区）；请人工确认后删除或改名该工作区再重试",
        )
    if not base_branch_matches(worktree, assignment.base_branch):
        raise DispatcherError("worktree_base_branch_mismatch", "工作区基础分支与任务分支不一致")


def resolve_worktree_base_branch(
    config: Config,
    orca: OrcaClient,
    repository_id: str,
    assignment: Assignment,
) -> Assignment:
    if assignment.base_branch is not None:
        return assignment
    resolver = getattr(orca, "repo_base_branch", None)
    if not callable(resolver):
        raise DispatcherError("worktree_base_branch_unknown", "无法确认 Orca 仓库默认基础分支")
    default_branch = retry_read(config, lambda: resolver(repository_id))
    if not default_branch:
        raise DispatcherError("worktree_base_branch_unknown", "Orca 未返回仓库默认基础分支")
    return replace(assignment, base_branch=default_branch)


def ensure_worktree_branch(repository: Repository, assignment: Assignment, worktree: OrcaWorktree) -> None:
    """把分支收敛到 <用户名>/<工作区名>；Orca 的初始名视为创建未完成的中间态。"""
    expected = worktree_branch_for(assignment, repository)
    current = git_current_branch(worktree.path)
    if current == expected:
        return
    if current != worktree_name_for(assignment):
        raise DispatcherError("worktree_branch_mismatch", "工作区当前分支不是任务目标分支")
    rename_worktree_branch(worktree.path, expected)
    if git_current_branch(worktree.path) != expected:
        raise DispatcherError("worktree_branch_mismatch", "无法确认工作区目标分支")


def settle_worktree_branch(
    repository: Repository,
    assignment: Assignment,
    worktree: OrcaWorktree,
    active_terminals: tuple[OrcaTerminal, ...],
) -> None:
    if active_terminals:
        return
    ensure_worktree_branch(repository, assignment, worktree)


def cleanup_suffix_worktrees(
    config: Config,
    orca: OrcaClient,
    worktrees: Iterable[OrcaWorktree],
    repository_id: str,
    assignment: Assignment,
) -> None:
    expected_name = worktree_name_for(assignment)
    suffix_pattern = re.compile(rf"{re.escape(expected_name)}-\d+\Z")
    suffixes = tuple(worktree for worktree in worktrees if suffix_pattern.fullmatch(worktree.path.name))
    for worktree in suffixes:
        if worktree.repository_id != repository_id or not base_branch_matches(worktree, assignment.base_branch):
            raise DispatcherError("worktree_suffix_unsafe", "发现身份或基础分支不符的自动后缀工作区")
        if has_live_agent_terminal(config, orca, worktree):
            raise DispatcherError("worktree_suffix_active", "发现仍有开发会话的自动后缀工作区")
        if not worktree_is_clean(worktree.path):
            raise DispatcherError("worktree_suffix_dirty", "发现含未提交改动的自动后缀工作区")
    for worktree in suffixes:
        orca.worktree_remove(worktree)


def verify_created_terminal(
    config: Config,
    orca: OrcaClient,
    worktree: OrcaWorktree,
    handle: str,
) -> OrcaTerminal:
    """确认本次创建的终端确实落在目标任务工作区；不复用任何既有终端。"""
    terminal = retry_read(config, lambda: orca.terminal_show(handle))
    if terminal.worktree_id != worktree.worktree_id:
        raise DispatcherError("terminal_worktree_mismatch", "新建终端不属于目标任务工作区")
    return terminal


def creation_conflict_error(
    worktrees: Iterable[OrcaWorktree],
    assignment: Assignment,
    repository_id: str,
    expected_worktree_id: str | None,
) -> DispatcherError | None:
    """等待创建结果期间的冲突判定：只接纳唯一精确同名工作区。"""
    expected_name = worktree_name_for(assignment)
    suffix_pattern = re.compile(rf"{re.escape(expected_name)}-\d+\Z")
    if any(suffix_pattern.fullmatch(worktree.path.name) for worktree in worktrees):
        return DispatcherError("worktree_name_conflict", "等待期间出现自动后缀工作区，停止分发且不清理")
    exact = tuple(worktree for worktree in worktrees if worktree.path.name == expected_name)
    if len(exact) > 1:
        return DispatcherError("worktree_identity_mismatch", "存在多个同名任务工作区")
    if not exact:
        return None
    worktree = exact[0]
    if expected_worktree_id is not None and worktree.worktree_id != expected_worktree_id:
        return DispatcherError("worktree_identity_mismatch", "工作区 id 与创建回执不一致")
    if (
        worktree.repository_id != repository_id
        or worktree.comment != worktree_comment_for(assignment)
        or not base_branch_matches(worktree, assignment.base_branch)
    ):
        return DispatcherError("worktree_identity_mismatch", "工作区归属标记与任务或租户不一致")
    return None


def worktree_git_ready(worktree_path: Path) -> bool:
    """确认 Orca 侧检出已完成：路径已是可读的 Git 工作区。"""
    if not worktree_path.is_dir():
        return False
    completed = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=child_environment(),
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def observe_created_worktree(
    config: Config,
    orca: OrcaClient,
    store: StateStore,
    repository: Repository,
    assignment: Assignment,
    repository_id: str,
    expected_worktree_id: str | None,
    receipt_lost: bool,
) -> PreparedWorktree:
    """创建请求已发出但回执未确认：只读等待原名工作区检出就绪，绝不重发创建。"""
    name = worktree_name_for(assignment)
    deadline = time.monotonic() + ORCA_WORKTREE_TIMEOUT_SECONDS
    warnings: tuple[str, ...] = (WORKTREE_RECEIPT_RECOVERED,) if receipt_lost else ()
    while True:
        try:
            worktrees = retry_read(config, lambda: orca.worktrees(repository_id))
            conflict = creation_conflict_error(worktrees, assignment, repository_id, expected_worktree_id)
            if conflict is not None:
                raise conflict
            exact = tuple(worktree for worktree in worktrees if worktree.path.name == name)
            if exact:
                worktree = exact[0]
                if not worktree_git_ready(worktree.path):
                    retry_log("同名工作区已出现但检出尚未完成；继续只读等待")
                else:
                    # 资源一确认就先落状态，之后任何失败都能人工核对到实际工作区。
                    store.mark_worktree_prepared(assignment, worktree)
                    active_terminals = active_agent_terminals(config, orca, worktree)
                    settle_worktree_branch(repository, assignment, worktree, active_terminals)
                    return PreparedWorktree(worktree, active_terminals, (
                        *(() if active_terminals else sync_worktree_files(repository, worktree.path)),
                        *warnings,
                    ))
        except DispatcherError as error:
            if error.orca_code not in ORCA_TRANSPORT_ERRORS:
                raise
        if time.monotonic() >= deadline:
            raise DispatcherError(
                "worktree_creation_unconfirmed",
                f"创建回执未收到且未在期限内确认工作区 {name}；资源保持不动，请人工核对后复位",
            )
        time.sleep(ORCA_WORKTREE_POLL_SECONDS)


def prepare_task_worktree(
    config: Config,
    orca: OrcaClient,
    store: StateStore,
    repository: Repository,
    assignment: Assignment,
    repository_id: str,
) -> PreparedWorktree:
    """严格解析稳定工作区：清理安全后缀、复用已验证工作区或创建新工作区。"""
    worktrees = retry_read(config, lambda: orca.worktrees(repository_id))
    cleanup_suffix_worktrees(config, orca, worktrees, repository_id, assignment)
    if any(re.compile(rf"{re.escape(worktree_name_for(assignment))}-\d+\Z").fullmatch(worktree.path.name) for worktree in worktrees):
        worktrees = retry_read(config, lambda: orca.worktrees(repository_id))
    expected_name = worktree_name_for(assignment)
    matching = tuple(worktree for worktree in worktrees if worktree.path.name == expected_name)
    if len(matching) > 1:
        raise DispatcherError("worktree_identity_mismatch", "存在多个同名任务工作区")
    if matching:
        worktree = matching[0]
        validate_task_worktree(worktree, assignment, repository_id)
        active_terminals = active_agent_terminals(config, orca, worktree)
        store.mark_worktree_prepared(assignment, worktree)
        settle_worktree_branch(repository, assignment, worktree, active_terminals)
        return PreparedWorktree(
            worktree,
            active_terminals,
            () if active_terminals else sync_worktree_files(repository, worktree.path),
        )

    try:
        created = orca.worktree_create(
            name=expected_name,
            repository_id=repository_id,
            base_branch=assignment.base_branch,
            comment=worktree_comment_for(assignment),
        )
    except DispatcherError as error:
        if error.orca_code not in ORCA_TRANSPORT_ERRORS:
            raise
        retry_log(f"创建回执未收到（{error.orca_code}）；只读等待原名工作区就绪，不重发创建")
        return observe_created_worktree(
            config, orca, store, repository, assignment, repository_id,
            expected_worktree_id=None,
            receipt_lost=True,
        )
    # 回执已拿到：先落状态，再等检出就绪，避免工作区已存在却没有任何记录。
    store.mark_worktree_prepared(assignment, created)
    listed = retry_read(config, lambda: orca.worktrees(repository_id))
    if not any(worktree.worktree_id == created.worktree_id for worktree in listed):
        retry_log("创建回执未在列表中确认；只读等待原名工作区就绪，不重发创建")
        return observe_created_worktree(
            config, orca, store, repository, assignment, repository_id,
            expected_worktree_id=created.worktree_id,
            receipt_lost=True,
        )
    return observe_created_worktree(
        config, orca, store, repository, assignment, repository_id,
        expected_worktree_id=created.worktree_id,
        receipt_lost=False,
    )


def copy_missing_artifact(source: str, destination: str) -> str:
    """共享工作区仅补缺失制品，不覆盖活动会话已有内容。"""
    target = Path(destination)
    if target.exists():
        return destination
    try:
        with target.open("xb") as output, Path(source).open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
    except FileExistsError:
        pass
    return destination


def relocate_requirement_artifacts(
    assignment: Assignment, worktree_path: Path, *, preserve_existing: bool = False
) -> Assignment:
    """迁入暂存制品；共享活动工作区时仅补充缺失文件。"""
    docs_root = staged_docs_root(assignment)
    target_root = worktree_path / "docs" / "engineering"
    snapshot_path = assignment.requirement_snapshot_path
    report_path = assignment.gitnexus_report_path
    relocated_snapshot: Path | None = None
    relocated_report: Path | None = None
    if snapshot_path is not None:
        resolved = snapshot_path.resolve()
        if not resolved.is_file() or not is_within(resolved, docs_root):
            raise DispatcherError("invalid_input", "requirement_snapshot_path 必须位于源仓库 .runtime 暂存目录下")
        relocated_snapshot = target_root / "specs" / resolved.name
    if report_path is not None:
        resolved_report = report_path.resolve()
        if not resolved_report.is_file() or not is_within(resolved_report, docs_root):
            raise DispatcherError("invalid_input", "gitnexus_report_path 必须位于源仓库 .runtime 暂存目录下")
        relocated_report = target_root / "research" / resolved_report.name
    if docs_root.is_dir():
        try:
            shutil.copytree(
                docs_root, target_root, dirs_exist_ok=True,
                copy_function=copy_missing_artifact if preserve_existing else shutil.copy2,
            )
        except (OSError, shutil.Error) as error:
            raise DispatcherError("artifact_relocation_failed", "无法迁入任务需求制品") from error
    return replace(
        assignment,
        requirement_snapshot_path=relocated_snapshot,
        gitnexus_report_path=relocated_report,
    )


def worktree_create_assignment(
    config: Config,
    task_id: str,
    repository_name: str,
    base_branch: str | None,
    worktree_slug: str | None,
) -> tuple[Repository, Assignment]:
    repository_name = require_text(repository_name, "repository")
    repository = configured_repository(config, repository_name)
    if repository is None:
        raise DispatcherError("repository_not_found", f"未找到配置仓库：{repository_name}")
    branch = base_branch.strip() if isinstance(base_branch, str) and base_branch.strip() else config.default_branch
    if branch is not None:
        if branch not in config.branches_for(repository.name):
            raise DispatcherError("invalid_branch", f"{branch} 不在 {repository.name} 的配置白名单中")
        if config.validate_branch and not branch_exists(repository, branch):
            raise DispatcherError("branch_not_found", f"{branch} 在 {repository.name} 中不存在")
    slug = require_worktree_slug(worktree_slug) if worktree_slug is not None else None
    task = require_task_id(task_id)
    return repository, Assignment(
        task=Task(task, task, task_url_for(config.task_url_template, task)),
        repository=repository.name,
        repository_path=repository.path,
        base_branch=branch,
        worktree_slug=slug,
    )


def create_or_reuse_task_worktree(
    config: Config,
    orca: OrcaClient,
    repository: Repository,
    assignment: Assignment,
) -> dict[str, object]:
    store = StateStore(config.state_file)
    with store.launch_lock(force_unlock=False):
        return _create_or_reuse_task_worktree(config, orca, repository, assignment)


def _create_or_reuse_task_worktree(
    config: Config,
    orca: OrcaClient,
    repository: Repository,
    assignment: Assignment,
) -> dict[str, object]:
    repository_id = ensure_repository_registered(orca, repository)
    assignment = resolve_worktree_base_branch(config, orca, repository_id, assignment)
    expected_name = worktree_name_for(assignment)
    worktrees = retry_read(config, lambda: orca.worktrees(repository_id))
    conflict = creation_conflict_error(worktrees, assignment, repository_id, None)
    if conflict is not None:
        raise conflict
    matching = tuple(worktree for worktree in worktrees if worktree.path.name == expected_name)
    if len(matching) > 1:
        raise DispatcherError("worktree_identity_mismatch", "存在多个同名任务工作区")
    warnings: tuple[str, ...] = ()
    reused = bool(matching)
    if reused:
        worktree = matching[0]
        validate_task_worktree(worktree, assignment, repository_id)
    else:
        try:
            worktree = orca.worktree_create(
                name=expected_name,
                repository_id=repository_id,
                base_branch=assignment.base_branch,
                comment=worktree_comment_for(assignment),
            )
        except DispatcherError as error:
            if error.orca_code not in ORCA_TRANSPORT_ERRORS:
                raise
            retry_log(f"创建回执未收到（{error.orca_code}）；只读等待原名工作区就绪，不重发创建")
            worktree = observe_created_worktree_resource(
                config, orca, repository, assignment, repository_id, None
            )
            warnings = (WORKTREE_RECEIPT_RECOVERED,)
        else:
            worktree = observe_created_worktree_resource(
                config, orca, repository, assignment, repository_id, worktree.worktree_id
            )
    active_terminals = active_agent_terminals(config, orca, worktree)
    settle_worktree_branch(repository, assignment, worktree, active_terminals)
    if not active_terminals:
        warnings = (*sync_worktree_files(repository, worktree.path), *warnings)
    return {
        "task_id": assignment.task.task_id,
        "repository": repository.to_dict(),
        "worktree_name": expected_name,
        "worktree_id": worktree.worktree_id,
        "worktree_path": worktree.path.as_posix(),
        "branch": git_current_branch(worktree.path),
        "base_branch": assignment.base_branch,
        "reused": reused,
        "warnings": list(warnings),
    }


def observe_created_worktree_resource(
    config: Config,
    orca: OrcaClient,
    repository: Repository,
    assignment: Assignment,
    repository_id: str,
    expected_worktree_id: str | None,
) -> OrcaWorktree:
    name = worktree_name_for(assignment)
    deadline = time.monotonic() + ORCA_WORKTREE_TIMEOUT_SECONDS
    while True:
        worktrees = retry_read(config, lambda: orca.worktrees(repository_id))
        conflict = creation_conflict_error(worktrees, assignment, repository_id, expected_worktree_id)
        if conflict is not None:
            raise conflict
        exact = tuple(worktree for worktree in worktrees if worktree.path.name == name)
        if exact and worktree_git_ready(exact[0].path):
            return exact[0]
        if time.monotonic() >= deadline:
            raise DispatcherError(
                "worktree_creation_unconfirmed",
                f"未在期限内确认工作区 {name}；资源保持不动，请人工核对",
            )
        time.sleep(ORCA_WORKTREE_POLL_SECONDS)


def launch(
    config: Config,
    assignments: tuple[Assignment, ...],
    store: StateStore,
    orca: OrcaClient,
    force_unlock: bool,
) -> dict[str, object]:
    task_ids = [
        (assignment.task.task_id, assignment.tenant_slug, assignment.dispatch_flow)
        for assignment in assignments
    ]
    if len(task_ids) != len(set(task_ids)):
        raise DispatcherError("invalid_input", "同一输入中不能包含重复 task_id、tenant 和 dispatch_flow")

    repositories = repositories_for_assignments(config, assignments)
    for assignment in assignments:
        validate_assignment(config, assignment, repositories)
    require_unique_worktree_names(config, assignments)

    with store.launch_lock(force_unlock):
        dispatched = tuple(
            assignment
            for assignment in assignments
            if store.status(assignment.task.task_id, assignment.tenant_slug, assignment.dispatch_flow) == "dispatched"
        )
        uncertain = tuple(
            assignment
            for assignment in assignments
            if store.status(assignment.task.task_id, assignment.tenant_slug, assignment.dispatch_flow)
            in {"launching", "requires_manual_reset"}
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
            assignment_result(assignment, "skipped_dispatched") for assignment in dispatched
        ] + [
            assignment_result(assignment, "requires_manual_reset") for assignment in uncertain
        ]

        if not selected:
            return {
                "results": results,
                "ignored_task_ids": [assignment.task.task_id for assignment in ignored],
                "ignored_assignments": [assignment_result(assignment, "ignored") for assignment in ignored],
                "plans": [],
            }

        retry_read(config, orca.status)
        repository_ids = retry_read(config, orca.repo_ids)
        for assignment in selected:
            repository = repositories[assignment.repository]
            try:
                store.mark_launching(assignment)
            except DispatcherError as error:
                results.append(assignment_result(assignment, "failed_state", message=error.message))
                continue
            terminal_handle: str | None = None
            receipt: SendReceipt | None = None
            try:
                repository_id = repository_ids.get(
                    os.path.normcase(os.path.normpath(str(repository.path.resolve())))
                )
                if repository_id is None:
                    repository_id = ensure_repository_registered(orca, repository)
                    repository_ids[os.path.normcase(os.path.normpath(str(repository.path.resolve())))] = repository_id
                if config.flow_for(assignment.dispatch_flow).requires_worktree:
                    prepared = prepare_task_worktree(config, orca, store, repository, assignment, repository_id)
                    worktree = prepared.worktree
                    active_terminals = prepared.active_terminals
                    create_warnings = prepared.warnings
                    settled = relocate_requirement_artifacts(
                        assignment, worktree.path, preserve_existing=bool(active_terminals)
                    )
                    worktree_docs = worktree.path / "docs" / "engineering"
                else:
                    # 不要求独立工作树的流程：开发会话落在源仓库 checkout，制品留在暂存目录。
                    worktree = OrcaWorktree(
                        worktree_id=f"path:{repository.path.resolve().as_posix()}",
                        path=repository.path.resolve(),
                    )
                    active_terminals = active_agent_terminals(config, orca, worktree)
                    create_warnings = ()
                    settled = assignment
                    worktree_docs = staged_docs_root(assignment)
                if settled.requirement_snapshot_path is not None:
                    validate_requirement_snapshot_path(config, settled, worktree_docs)
                validate_gitnexus_report_path(config, settled, worktree_docs)
                prompt = command_for(config, settled)
                prompt_digest = initial_prompt_digest(prompt)
                duplicate = duplicate_terminal_for_prompt(store, worktree, active_terminals, prompt_digest)
                if duplicate is not None:
                    duplicate_state_key, duplicate_value, duplicate_terminal = duplicate
                    terminal_handle = duplicate_terminal.handle
                    receipt = receipt_from_state(duplicate_value)
                    if not receipt.accepted or receipt.observation == "unsupported":
                        raise DispatcherError(
                            "duplicate_session_unconfirmed",
                            "相同内容的活动会话首次投递结果未确认；不新建、不重发，请人工核对",
                        )
                    record = DispatchRecord(
                        assignment=settled,
                        worktree=worktree,
                        terminal_handle=terminal_handle,
                        receipt=receipt,
                        initial_prompt_digest=prompt_digest,
                        duplicate_of_state_key=duplicate_state_key,
                        warnings=(
                            *create_warnings,
                            *(() if receipt.turn_started else (TURN_START_UNOBSERVED,)),
                        ),
                    )
                    store.mark_dispatched(record)
                    result = assignment_result(
                        settled,
                        "skipped_duplicate_session",
                        terminal_handle=terminal_handle,
                        send_request_id=receipt.request_id,
                        dispatch_state=("turn_started" if receipt.turn_started else "input_accepted"),
                        duplicate_of_state_key=duplicate_state_key,
                    )
                    if record.warnings:
                        result["warnings"] = list(record.warnings)
                    results.append(result)
                    append_history_safely(store, assignment_history(
                        settled,
                        "skipped_duplicate_session",
                        terminal_handle=terminal_handle,
                        duplicate_of_state_key=duplicate_state_key,
                    ))
                    continue
                # 制品校验且未发现相同首次投递后才新建开发会话。
                terminal_handle = orca.terminal_create(
                    worktree,
                    worktree_name_for(assignment),
                    TERMINAL_AGENT,
                )
                terminal = verify_created_terminal(config, orca, worktree, terminal_handle)
                store.mark_terminal_created(assignment, terminal.handle)
                wait = orca.terminal_wait(terminal.handle, TERMINAL_READY_TIMEOUT_MS)
                if wait.get("satisfied") is not True:
                    raise DispatcherError(
                        "terminal_not_ready",
                        "开发会话未在预算内进入空闲状态；未发送任务文本，请人工核对后复位",
                    )
                store.mark_send_started(settled, prompt_digest)
                receipt = parse_send_receipt(orca.terminal_send(
                    terminal.handle,
                    prompt,
                ))
                if not receipt.accepted:
                    raise DispatcherError(
                        "dispatch_not_accepted",
                        "Orca 未接受任务文本；不重发，请人工核对终端后复位",
                    )
                if receipt.observation == "unsupported":
                    # 旧主机能收下文本却不返回可核验回执：不静默降级，也不重发。
                    raise DispatcherError(
                        "dispatch_unverifiable",
                        "当前 Orca 主机不支持投递观察，无法核验任务文本是否执行；"
                        "不重发，请人工核对终端后复位",
                    )
            except DispatcherError as error:
                detail: dict[str, object] = {}
                if terminal_handle:
                    detail["terminal_handle"] = terminal_handle
                if receipt is not None and receipt.request_id:
                    detail["send_request_id"] = receipt.request_id
                results.append(assignment_result(
                    assignment,
                    "requires_manual_reset",
                    message=error.message,
                    **detail,
                ))
                append_history_safely(store, assignment_history(
                    assignment, "requires_manual_reset", reason=error.code, **detail
                ))
                state_error = mark_manual_reset_safely(store, assignment, error.code)
                if state_error:
                    results[-1]["state_error"] = state_error
                continue
            record = DispatchRecord(
                assignment=settled,
                worktree=worktree,
                terminal_handle=terminal_handle,
                receipt=receipt,
                initial_prompt_digest=prompt_digest,
                warnings=(
                    *create_warnings,
                    *(() if receipt.turn_started else (TURN_START_UNOBSERVED,)),
                ),
            )
            try:
                store.mark_dispatched(record)
            except DispatcherError as error:
                results.append(assignment_result(
                    settled,
                    "requires_manual_reset",
                    message=error.message,
                    terminal_handle=terminal_handle,
                    send_request_id=receipt.request_id,
                ))
                append_history_safely(store, assignment_history(
                    settled,
                    "uncertain",
                    terminal_handle=terminal_handle,
                    send_request_id=receipt.request_id,
                    reason="state_write_after_send",
                ))
                state_error = mark_manual_reset_safely(store, settled, "state_write_after_send")
                if state_error:
                    results[-1]["state_error"] = state_error
                continue
            dispatch_state = "turn_started" if receipt.turn_started else "input_accepted"
            result = assignment_result(
                settled,
                "dispatched",
                terminal_handle=terminal_handle,
                send_request_id=receipt.request_id,
                dispatch_state=dispatch_state,
                initial_prompt_digest=record.initial_prompt_digest,
            )
            if record.warnings:
                result["warnings"] = list(record.warnings)
            workspace_status_error = set_worktree_in_progress_safely(
                orca,
                store,
                settled,
                record.worktree.path,
            )
            if workspace_status_error:
                result["workspace_status_error"] = workspace_status_error
            results.append(result)
            append_history_safely(store, assignment_history(
                settled,
                "dispatched",
                repo=settled.repository,
                terminal_handle=terminal_handle,
                dispatch_state=dispatch_state,
            ))

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
        "ignored_assignments": [assignment_result(assignment, "ignored") for assignment in ignored],
        "plans": [
            {
                "repository": repositories[assignment.repository].to_dict(),
                "task_ids": [assignment.task.task_id],
                "assignments": [
                    {
                        "task_id": assignment.task.task_id,
                        "tenant_slug": assignment.tenant_slug,
                        "assignment_id": assignment.assignment_id,
                        "dispatch_flow": assignment.dispatch_flow,
                    }
                ],
                "worktree_name": worktree_name_for(assignment),
            }
            for assignment in selected
        ],
        **({"current_run_error": current_run_error} if current_run_error else {}),
    }


def state_entry_result(entry: StateEntry, status: str, **extra: object) -> dict[str, object]:
    return {
        "task_id": entry.task_id,
        "tenant": entry.value.get("tenant", entry.tenant_slug),
        "tenant_slug": entry.tenant_slug,
        "assignment_id": entry.assignment_id,
        "dispatch_flow": entry.dispatch_flow,
        "status": status,
        **extra,
    }


def state_entry_history(entry: StateEntry, result: str, **extra: object) -> dict[str, object]:
    return {
        "task": entry.task_id,
        "tenant_slug": entry.tenant_slug,
        "assignment_id": entry.assignment_id,
        "dispatch_flow": entry.dispatch_flow,
        "state_key": entry.state_key,
        "result": result,
        **extra,
    }


def recover(
    config: Config,
    store: StateStore,
    orca: OrcaClient,
    task_id: str | None,
    force_unlock: bool,
    tenant_slug: str | None = None,
    dispatch_flow: str | None = None,
) -> dict[str, object]:
    with store.launch_lock(force_unlock):
        state = store.snapshot()
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        entries = store._resolved_entries(tasks)
        if task_id is not None:
            task = require_task_id(task_id)
            candidates = tuple(entry for entry in entries if entry.task_id == task)
            if tenant_slug is None:
                tenant_slugs = {entry.tenant_slug for entry in candidates}
                if tenant_slugs and tenant_slugs != {"legacy"}:
                    raise DispatcherError(
                        "assignment_ambiguous",
                        f"任务 {task} 存在多个租户状态，恢复时必须使用 --tenant-slug",
                    )
            else:
                tenant = require_tenant_slug(tenant_slug)
                candidates = tuple(entry for entry in candidates if entry.tenant_slug == tenant)
            if dispatch_flow is not None:
                flow = require_dispatch_flow(dispatch_flow)
                candidates = tuple(entry for entry in candidates if entry.dispatch_flow == flow)
            elif len({entry.dispatch_flow for entry in candidates}) > 1:
                raise DispatcherError(
                    "assignment_ambiguous",
                    f"任务 {task} 存在多个流程状态，恢复时必须使用 --dispatch-flow",
                )
            selected = candidates
        else:
            tenant = require_tenant_slug(tenant_slug) if tenant_slug is not None else None
            flow = require_dispatch_flow(dispatch_flow) if dispatch_flow is not None else None
            selected = tuple(
                entry
                for entry in entries
                if (tenant is None or entry.tenant_slug == tenant)
                and (flow is None or entry.dispatch_flow == flow)
            )
        selected = tuple(
            entry
            for entry in selected
            if entry.value.get("status") in {"dispatched", "launching"}
        )
        if not selected:
            return {"results": []}

        retry_read(config, orca.status)
        results: list[dict[str, object]] = []
        for entry in selected:
            value = entry.value
            handle = value.get("terminal_handle")
            if not isinstance(handle, str) or not handle:
                # 历史 orchestration 记录与未落盘终端句柄的记录都只报告，不迁移、不重发。
                store.mark_requires_manual_reset(entry.state_key, "terminal_handle_missing")
                append_history_safely(store, state_entry_history(
                    entry, "requires_manual_reset", reason="terminal_handle_missing"
                ))
                results.append(state_entry_result(entry, "requires_manual_reset"))
                continue
            if value.get("send_accepted") is not True:
                # 没有已落盘的接受证据：不重发，交人工核对终端实际状态。
                store.mark_requires_manual_reset(entry.state_key, "dispatch_not_confirmed")
                append_history_safely(store, state_entry_history(
                    entry, "requires_manual_reset", reason="dispatch_not_confirmed"
                ))
                results.append(state_entry_result(entry, "requires_manual_reset"))
                continue
            try:
                terminal = orca.terminal_show(handle)
            except DispatcherError as error:
                store.mark_requires_manual_reset(entry.state_key, error.code)
                append_history_safely(store, state_entry_history(
                    entry, "requires_manual_reset", reason=error.code
                ))
                results.append(state_entry_result(entry, "requires_manual_reset"))
                continue
            worktree_id = value.get("worktree_id")
            if isinstance(worktree_id, str) and worktree_id and terminal.worktree_id != worktree_id:
                store.mark_requires_manual_reset(entry.state_key, "terminal_worktree_mismatch")
                append_history_safely(store, state_entry_history(
                    entry, "requires_manual_reset", reason="terminal_worktree_mismatch"
                ))
                results.append(state_entry_result(entry, "requires_manual_reset"))
                continue
            dispatch_state = str(value.get("dispatch_state") or "")
            store.mark_recovered(entry.state_key, dispatch_state, "recovered", terminal_handle=handle)
            append_history_safely(store, state_entry_history(
                entry, "recovered", terminal_handle=handle, dispatch_state=dispatch_state
            ))
            results.append(state_entry_result(
                entry, "recovered", terminal_handle=handle, dispatch_state=dispatch_state
            ))

    return {"results": results}


def read_assignments(path: Path, default_flow: str) -> tuple[Assignment, ...]:
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
        assignments.append(Assignment.from_dict(item, default_flow))
    return tuple(assignments)


def validate_decision_values(tasks: object, default_flow: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(tasks, list):
        raise DispatcherError("invalid_input", "决策 JSON 必须含 tasks 列表")
    decision_fields = {
        "task_id", "title", "description", "task_url", "assignee", "tenant", "tenant_slug",
        "source_task_id", "source_assignee", "parent_task_id", "parent_assignee", "reference_plan",
        "candidate_eligible", "candidate_eligibility_reason", "candidate_jql", "reference_plan_source", "parent_reference_plan",
        "dispatch_flow", "gitnexus_report_path", "requirement_snapshot_path", "repository", "base_branch", "worktree_slug",
    }
    values: list[Mapping[str, Any]] = []
    task_ids: set[tuple[str, str, str]] = set()
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
        identity = (task_id, tenant_slug, require_dispatch_flow(item.get("dispatch_flow", default_flow)))
        if identity in task_ids:
            raise DispatcherError("invalid_input", f"任务 ID、租户和流程重复：{task_id}/{tenant_slug}")
        task_ids.add(identity)
        values.append(item)
    return tuple(values)


def read_decision_input(path: Path, default_flow: str) -> tuple[Mapping[str, Any], ...]:
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
    return validate_decision_values(tasks, default_flow)


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


def decision_result(identity: Mapping[str, object], status: str, **extra: object) -> dict[str, object]:
    return {**identity, "status": status, **extra}


def decide_values(config: Config, values: tuple[Mapping[str, Any], ...]) -> dict[str, object]:
    """校验外部调研后的显式路由，不执行语义匹配或任何外部调用。"""
    values = tuple(dict(value) for value in values)
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
        tenant = require_optional_text(value.get("tenant"), "tenant") or "legacy"
        tenant_slug = require_tenant_slug(value.get("tenant_slug", "legacy"))
        dispatch_flow = require_dispatch_flow(value.get("dispatch_flow", config.default_flow))
        identity = {
            "task_id": task.task_id,
            "tenant": tenant,
            "tenant_slug": tenant_slug,
            "assignment_id": legacy_state_key(task.task_id, tenant_slug),
            "dispatch_flow": dispatch_flow,
        }
        flow = config.flow_for(dispatch_flow)
        if flow.requires_candidate_eligibility:
            evidence_fields = ("candidate_eligible", "candidate_eligibility_reason", "candidate_jql", "reference_plan_source")
            if not all(field in value for field in evidence_fields):
                results.append(decision_result(identity, "needs_confirmation", reason="任务缺少完整候选资格交接字段（含 candidate_jql）"))
                continue
            eligible = value.get("candidate_eligible")
            reason = value.get("candidate_eligibility_reason")
            jql = value.get("candidate_jql")
            if eligible is not True or not isinstance(reason, str) or not reason.strip() or not isinstance(jql, str) or not jql.strip():
                results.append(decision_result(identity, "needs_confirmation", reason="任务缺少已确认的候选资格；查询失败或资格未确认时不得进入仓库映射兜底"))
                continue
        if flow.requires_reference_plan_candidate:
            source = value.get("reference_plan_source")
            task_plan = value.get("reference_plan")
            parent_plan = value.get("parent_reference_plan")
            has_plan = (
                source == "task" and isinstance(task_plan, str) and bool(task_plan.strip())
            ) or (
                source == "parent" and isinstance(parent_plan, str) and bool(parent_plan.strip())
            )
            if not has_plan:
                results.append(decision_result(identity, "needs_confirmation", reason="proposal 任务必须有任务或父任务参考方案，且来源字段必须一致"))
                continue
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
            results.append(decision_result(
                identity,
                "needs_confirmation",
                reason="缺少外部联合证据确认的项目决策",
                candidates={
                    "repositories": [repository.to_dict() for repository in repository_candidates],
                    "base_branches": [],
                },
            ))
            continue
        if not repository_candidates:
            results.append(decision_result(
                identity,
                "needs_confirmation",
                reason=f"未找到配置项目：{repository_name}",
                candidates={"repositories": [], "base_branches": []},
            ))
            continue
        if len(repository_candidates) != 1:
            results.append(decision_result(
                identity,
                "needs_confirmation",
                reason="无法根据外部联合证据唯一确定项目",
                candidates={
                    "repositories": [repository.to_dict() for repository in repository_candidates],
                    "base_branches": [],
                },
            ))
            continue

        repository = repository_candidates[0]
        project_tenants = config.tenants_for(repository.name)
        if tenant_slug != "legacy" and project_tenants.get(tenant) != tenant_slug:
            results.append(decision_result(
                identity,
                "needs_confirmation",
                reason=f"租户不属于项目配置：{repository.name}/{tenant}",
                candidates={
                    "repositories": [repository.to_dict()],
                    "tenants": [{"name": name, "slug": slug} for name, slug in project_tenants.items()],
                    "base_branches": [],
                },
            ))
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
            results.append(decision_result(
                identity,
                "needs_confirmation",
                reason="缺少外部联合证据确认的基础分支决策",
                candidates={"repositories": [repository.to_dict()], "base_branches": branches},
            ))
            continue
        if branch is not None:
            matching = next((entry for entry in branches if entry["name"] == branch), None)
            if matching is None:
                results.append(decision_result(
                    identity,
                    "needs_confirmation",
                    reason=f"基础分支不在 {repository.name} 的配置白名单中",
                    candidates={"repositories": [repository.to_dict()], "base_branches": branches},
                ))
                continue
            if matching["valid"] is False:
                results.append(decision_result(
                    identity,
                    "needs_confirmation",
                    reason=f"基础分支不存在：{repository.name}/{branch}",
                    candidates={"repositories": [repository.to_dict()], "base_branches": branches},
                ))
                continue
        elif len(branches) > 1:
            results.append(decision_result(
                identity,
                "needs_confirmation",
                reason="无法唯一确定基础分支",
                candidates={"repositories": [repository.to_dict()], "base_branches": branches},
            ))
            continue
        elif branches:
            branch = branches[0]["name"]
            if branches[0]["valid"] is False:
                results.append(decision_result(
                    identity,
                    "needs_confirmation",
                    reason=f"基础分支不存在：{repository.name}/{branch}",
                    candidates={"repositories": [repository.to_dict()], "base_branches": branches},
                ))
                continue

        try:
            assignment = Assignment.from_dict({
                **value,
                "repository": repository.name,
                "repository_path": repository.path.as_posix(),
                "base_branch": branch,
            }, config.default_flow)
        except DispatcherError as error:
            results.append(decision_result(
                identity,
                "needs_confirmation",
                reason=error.message,
                candidates={"repositories": [repository.to_dict()], "base_branches": branches},
            ))
            continue
        flow = config.flow_for(assignment.dispatch_flow)
        try:
            validate_candidate_eligibility(assignment, flow)
        except DispatcherError as error:
            results.append(decision_result(
                identity,
                "needs_confirmation",
                reason=error.message,
                candidates={"repositories": [repository.to_dict()], "base_branches": branches},
            ))
            continue
        if assignment.requirement_snapshot_path is None and flow.requires_snapshot:
            results.append(decision_result(
                identity,
                "needs_confirmation",
                reason=f"流程 {flow.name} 要求提供 requirement_snapshot_path",
                candidates={"repositories": [repository.to_dict()], "base_branches": branches},
            ))
            continue
        try:
            if assignment.requirement_snapshot_path is not None:
                validate_requirement_snapshot_path(config, assignment, staged_docs_root(assignment))
        except DispatcherError as error:
            results.append(decision_result(
                identity,
                "needs_confirmation",
                reason=error.message,
                candidates={"repositories": [repository.to_dict()], "base_branches": branches},
            ))
            continue
        normalized = assignment.to_dict()
        results.append(decision_result(identity, "selected", assignment=normalized))
        selected.append(normalized)

    status = "ready" if all(result["status"] == "selected" for result in results) else "needs_confirmation"
    return {
        "version": 1,
        "status": status,
        "tasks": selected if status == "ready" else results,
        "launch_input": {"tasks": selected},
    }


def decide(config: Config, path: Path) -> dict[str, object]:
    return decide_values(config, read_decision_input(path, config.default_flow))


def task_source_prompt(
    config: Config,
    query_override: str | None = None,
    flow: str | None = None,
) -> dict[str, object]:
    selected_flow = flow or config.default_flow
    resolved = config.flow_for(selected_flow, "flow")
    if query_override is not None:
        query, jql_source = require_text(query_override, "jql"), "cli"
    elif resolved.query is not None:
        query, jql_source = resolved.query, "flow"
    else:
        query, jql_source = config.task_source_query, "config"
    variables = {
        "query": query,
        "reference_plan_field": config.reference_plan_field or "未配置",
    }
    prompt_template = resolved.fetch_prompt or config.fetch_prompt
    names = set(re.findall(r"\{\{([^{}]+)\}\}", prompt_template))
    unknown = names - variables.keys()
    if unknown:
        raise DispatcherError("invalid_config", f"流程 {resolved.name} 的 fetch_prompt 包含未知变量：{sorted(unknown)[0]}")
    prompt = prompt_template
    for name, value in variables.items():
        prompt = prompt.replace(f"{{{{{name}}}}}", value)
    return {
        "type": config.task_source_type,
        "fetch_prompt": prompt,
        "session_prompt": resolved.session_prompt or config.session_prompt,
        "task_url_template": config.task_url_template,
        "max_tasks": config.max_tasks,
        "reference_plan_field": config.reference_plan_field,
        "query": query,
        "jql_source": jql_source,
        "flow": resolved.name,
        "dispatch_flow": resolved.name,
        "proposal_command": resolved.command_template if resolved.name == "proposal" else None,
        "jql_semantics": "native_jql_then_parent_post_filter",
        # 父项字段不写死：各流程的查询条件可能指向不同字段，统一取配置的参考方案字段。
        "parent_lookup": {"relation": "parent", "field": config.reference_plan_field or "未配置", "required": True},
        "post_filter": "该字段在子任务或父任务上非空",
        "next_steps": list(resolved.next_steps),
    }


def config_summary(config: Config) -> dict[str, object]:
    return {
        "skill_root": config.root.as_posix(),
        "projects_root": config.projects_root.as_posix(),
        "max_tasks": config.max_tasks,
        "max_agents": config.max_agents,
        "state_file": config.state_file.as_posix(),
        "default_flow": config.default_flow,
        "flows": [
            {"name": flow.name, "stages": list(flow.stage_names), "default": flow.is_default,
             "requires_candidate_eligibility": flow.requires_candidate_eligibility,
             "requires_reference_plan_candidate": flow.requires_reference_plan_candidate}
            for flow in config.flows.values()
        ],
        "stages": [{"name": stage.name} for stage in config.stages.values()],
        "deprecation_warnings": list(config.deprecation_warnings),
    }


def build_parser(flow_names: tuple[str, ...] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Orca 任务分发器")
    parser.add_argument("--config", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="校验配置并列出可用仓库")
    commands.add_parser("repos", help="列出候选仓库")
    project = commands.add_parser("project", help="按名称查询显式配置项目")
    project.add_argument("--name", required=True)
    branches = commands.add_parser("branches", help="列出并校验仓库分支")
    branches.add_argument("--repository", required=True)
    state = commands.add_parser("state", help="读取长期分发状态")
    state.add_argument("--dispatch-flow", metavar="<流程>", help="按流程筛选状态；允许已从注册表删除的流程名")
    recover = commands.add_parser("recover", help="恢复已分发任务的 Orca 会话")
    recover.add_argument("--task-id")
    recover.add_argument("--tenant-slug", help="指定同一任务下要恢复的租户")
    recover.add_argument("--dispatch-flow", metavar="<流程>", help="指定要恢复的流程；允许已从注册表删除的流程名")
    recover.add_argument("--force-unlock", action="store_true")
    task_source_parser = commands.add_parser("task-source", help="输出已渲染的任务获取提示词")
    task_source_parser.add_argument("--jql", help="仅本次运行覆盖配置中的 JQL")
    task_source_parser.add_argument(
        "--flow", choices=flow_names, metavar="<流程>", required=True,
        help="用户选择的任务流程；候选项来自配置注册表",
    )
    decide_parser = commands.add_parser(
        "decide",
        help="校验外部调研后的项目与分支决策",
        description=(
            "读取 version=1 的 JSON：顶层仅包含 version 与 tasks。每项任务必须提供 task_id、title、task_url，"
            "可选 description、assignee、tenant、tenant_slug、source_task_id、source_assignee、parent_task_id、parent_assignee、"
            "reference_plan、gitnexus_report_path，并由外部流程提供 repository 与 base_branch。\n"
            "流程要求候选资格时（节点声明 requires_candidate_eligibility: true）还必须提供完整的 "
            "candidate_eligible、candidate_eligibility_reason、candidate_jql、reference_plan_source；"
            "proposal 还要求 reference_plan 或 parent_reference_plan 与 reference_plan_source 一致。\n"
            "不用 --input 时是单任务模式，必须提供 --task-id、--title、--task-url、--repository，例如：\n"
            "  dispatcher.py decide --task-id CW-7622 --title \"任务标题\" --task-url https://jira.example/CW-7622"
            " --repository finance --base-branch origin/release --dispatch-flow direct --source-task-id CW-7624"
            " --candidate-eligible --candidate-eligibility-reason \"JQL 通过\" --candidate-jql \"status = 待开发\""
            " --reference-plan-source none\n"
            "该命令不调用 Jira、GitNexus 或 Orca，不创建 worktree，不写运行状态；无法确认的任务返回 needs_confirmation。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    decide_parser.add_argument("--input", type=Path)
    for name, kwargs in (
        ("task-id", {"dest": "task_id"}),
        ("title", {}), ("task-url", {"dest": "task_url"}), ("repository", {}),
        ("base-branch", {"dest": "base_branch"}), ("dispatch-flow", {"dest": "dispatch_flow", "choices": flow_names, "metavar": "<流程>"}),
        ("source-task-id", {"dest": "source_task_id"}), ("description", {}), ("assignee", {}),
        ("tenant", {}), ("tenant-slug", {"dest": "tenant_slug"}), ("source-assignee", {"dest": "source_assignee"}),
        ("parent-task-id", {"dest": "parent_task_id"}), ("parent-assignee", {"dest": "parent_assignee"}),
        ("reference-plan", {"dest": "reference_plan"}),
        ("candidate-eligible", {"dest": "candidate_eligible", "action": "store_true", "default": None}),
        ("candidate-eligibility-reason", {"dest": "candidate_eligibility_reason"}),
        ("candidate-jql", {"dest": "candidate_jql"}),
        ("reference-plan-source", {"dest": "reference_plan_source"}),
        ("parent-reference-plan", {"dest": "parent_reference_plan"}),
        ("gitnexus-report-path", {"dest": "gitnexus_report_path"}),
        ("requirement-snapshot-path", {"dest": "requirement_snapshot_path"}), ("worktree-slug", {"dest": "worktree_slug"}),
    ):
        decide_parser.add_argument(f"--{name}", **kwargs)
    launch_parser = commands.add_parser(
        "launch",
        help="建工作区、同步文件并启动普通 Claude 会话",
        description=(
            "输入 JSON 顶层只能为 tasks 列表，每项任务字段：\n"
            "  task_id（必填，字符串）：任务唯一标识\n"
            "  title（必填，字符串）：任务标题\n"
            "  task_url（必填，HTTPS）：必须由 task_url_template 生成\n"
            "  repository（必填，字符串）：仓库名，来自 repos 输出\n"
            "  repository_path（必填，字符串）：仓库绝对路径\n"
            "  base_branch（可选，字符串或 null）：基础分支，须在仓库白名单且存在\n"
            "  worktree_slug（可选，字符串或 null）：工作区目录名里的语义 slug；与任务编号构成 Orca 工作区名\n"
            "  reference_plan（可选，字符串或 null）：参考方案文本，非空时随任务信息发送给下游会话\n"
            "  assignee（可选，字符串或 null）：Jira 当前负责人显示名称，作为项目定位与任务上下文证据\n"
            "  tenant（可选，字符串）：租户显示名称；同一 task_id 的不同租户可分别分发\n"
            "  tenant_slug（可选，字符串）：租户稳定安全标识；省略时为 legacy\n"
            "  dispatch_flow（可选，字符串）：分发流程；省略时为 complete，状态去重 identity 包含该字段\n"
            "  source_task_id（可选，字符串或 null）：归一化前的 Jira 开发子任务编号\n"
            "  source_assignee（可选，字符串或 null）：归一化前开发子任务的 Jira 负责人\n"
            "  parent_task_id（可选，字符串或 null）：关联父产品需求编号；必须省略或等于 task_id，提供时须同时给出 source_task_id\n"
            "  parent_assignee（可选，字符串或 null）：父产品需求负责人，仅作上下文\n"
            "  requirement_snapshot_path（流程声明 requires_snapshot: true 时必填，字符串）：已校验的完整原始需求快照绝对路径，随任务上下文发送以复用正文与附件本体；缺失或不完整必须阻断该任务\n"
            "  candidate_eligible / candidate_eligibility_reason / candidate_jql / reference_plan_source（流程要求候选资格时必填）：\n"
            "    由 Jira 节点逐任务交接的候选资格证据；缺任一字段时该流程的任务被拒绝\n"
            "  reference_plan / parent_reference_plan（可选，字符串或 null）：参考方案文本，必须与 reference_plan_source 一致\n"
            "每个任务都在独立 linked worktree 中启动普通 Claude 会话；工作区由 Orca 创建、dev-spec-gen 同步。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    launch_parser.add_argument("--input", type=Path, required=True)
    launch_parser.add_argument("--force-unlock", action="store_true")
    worktree_parser = commands.add_parser("worktree", help="管理任务工作区")
    worktree_commands = worktree_parser.add_subparsers(dest="worktree_command", required=True)
    worktree_create_parser = worktree_commands.add_parser(
        "create",
        help="通过 Orca 创建或复用任务 worktree，并执行 dev-spec-gen 同步",
    )
    worktree_create_parser.add_argument("--task-id", required=True, help="任务编号；用于稳定工作区名称和归属标记")
    worktree_create_parser.add_argument("--repository", required=True, help="配置中的仓库名")
    worktree_create_parser.add_argument("--base-branch", help="可选基础分支；省略时使用配置默认值或 Orca 仓库默认")
    worktree_create_parser.add_argument("--worktree-slug", help="可选的 1–2 个英文小写 kebab 词")
    reset = commands.add_parser("reset", help="允许任务重新分发")
    reset.add_argument("task_id")
    reset.add_argument("--force-unlock", action="store_true")
    reset.add_argument("--force", action="store_true", help="允许复位已分发的任务")
    reset.add_argument("--tenant-slug", help="指定同一任务下要复位的租户")
    reset.add_argument("--dispatch-flow", metavar="<流程>", help="指定要复位的流程；允许已从注册表删除的流程名")
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
        return {"state": store.state_view(arguments.dispatch_flow)}
    if arguments.command == "recover":
        return recover(
            config=config,
            store=store,
            orca=OrcaClient(),
            task_id=arguments.task_id,
            force_unlock=arguments.force_unlock,
            tenant_slug=arguments.tenant_slug,
            dispatch_flow=arguments.dispatch_flow,
        )
    if arguments.command == "task-source":
        return task_source_prompt(config, arguments.jql, arguments.flow)
    if arguments.command == "decide":
        cli_fields = ("task_id", "title", "task_url", "repository", "base_branch", "dispatch_flow", "source_task_id", "description", "assignee", "tenant", "tenant_slug", "source_assignee", "parent_task_id", "parent_assignee", "reference_plan", "candidate_eligible", "candidate_eligibility_reason", "candidate_jql", "reference_plan_source", "parent_reference_plan", "gitnexus_report_path", "requirement_snapshot_path", "worktree_slug")
        supplied = {name: getattr(arguments, name) for name in cli_fields if getattr(arguments, name) is not None}
        if arguments.input is not None:
            if supplied:
                raise DispatcherError("invalid_input", "decide --input 不能与任务参数混用")
            return decide(config, arguments.input)
        required = ("task_id", "title", "task_url", "repository")
        missing = next((name for name in required if getattr(arguments, name) is None), None)
        if missing is not None:
            raise DispatcherError("invalid_input", f"缺少单任务参数：--{missing.replace('_', '-')}")
        return decide_values(config, validate_decision_values([supplied], config.default_flow))
    if arguments.command == "worktree":
        if arguments.worktree_command == "create":
            orca = OrcaClient()
            repository, assignment = worktree_create_assignment(
                config,
                arguments.task_id,
                arguments.repository,
                arguments.base_branch,
                arguments.worktree_slug,
            )
            retry_read(config, orca.status)
            return create_or_reuse_task_worktree(config, orca, repository, assignment)
        raise DispatcherError("invalid_command", f"未知 worktree 子命令：{arguments.worktree_command}")
    if arguments.command == "reset":
        removed = store.reset_entry(
            arguments.task_id,
            arguments.force_unlock,
            arguments.force,
            arguments.tenant_slug,
            dispatch_flow=arguments.dispatch_flow,
        )
        if removed is not None:
            append_history_safely(store, state_entry_history(
                removed,
                "reset",
                forced=arguments.force,
            ))
        return {
            "task_id": arguments.task_id,
            "tenant_slug": removed.tenant_slug if removed is not None else arguments.tenant_slug or "legacy",
            "dispatch_flow": removed.dispatch_flow if removed is not None else arguments.dispatch_flow,
            "reset": removed is not None,
        }
    if arguments.command == "launch":
        return launch(
            config=config,
            assignments=read_assignments(arguments.input, config.default_flow),
            store=store,
            orca=OrcaClient(),
            force_unlock=arguments.force_unlock,
        )
    raise DispatcherError("invalid_command", f"未知命令：{arguments.command}")


def emit(value: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")


def registered_flow_names(argv: list[str] | None) -> tuple[str, ...] | None:
    """尽力从配置读取流程名，用于生成 CLI 候选项；配置不可用时退回不做 choices 限制。"""
    try:
        pre_parser = argparse.ArgumentParser(add_help=False)
        pre_parser.add_argument("--config", type=Path, default=None)
        known, _ = pre_parser.parse_known_args(argv)
        return tuple(load_config(known.config).flows)
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser(registered_flow_names(argv)).parse_args(argv)
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
