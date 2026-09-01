from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Stage(str, Enum):
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    TASK_EXECUTION = "TASK_EXECUTION"
    TASK_INDEPENDENT_REVIEW = "TASK_INDEPENDENT_REVIEW"
    TASK_PATCH = "TASK_PATCH"
    PLAN_DEFECT_RESOLUTION = "PLAN_DEFECT_RESOLUTION"
    PLAN_REVISION_REVIEW = "PLAN_REVISION_REVIEW"
    HUMAN_PLAN_FREEZE = "HUMAN_PLAN_FREEZE"
    HUMAN_GROUP_APPROVAL = "HUMAN_GROUP_APPROVAL"
    FINAL_VERIFICATION = "FINAL_VERIFICATION"
    HUMAN_FINAL_ACCEPTANCE = "HUMAN_FINAL_ACCEPTANCE"
    HARD_STOP = "HARD_STOP"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Verdict(str, Enum):
    APPROVED = "APPROVED"
    REQUIRES_PATCH = "REQUIRES_PATCH"
    PLAN_TASK_DEFECT = "PLAN_TASK_DEFECT"
    OPEN_CONTRACT_ISSUE = "OPEN_CONTRACT_ISSUE"
    ARCHITECTURE_DECISION_REQUIRED = "ARCHITECTURE_DECISION_REQUIRED"
    PLAN_EXPANSION_REQUIRED = "PLAN_EXPANSION_REQUIRED"
    SECURITY_SENSITIVE_ACTION = "SECURITY_SENSITIVE_ACTION"
    DESTRUCTIVE_ACTION_REQUIRED = "DESTRUCTIVE_ACTION_REQUIRED"
    UNAUTHORIZED_EXTERNAL_SIDE_EFFECT = "UNAUTHORIZED_EXTERNAL_SIDE_EFFECT"
    FROZEN_SOURCE_MISMATCH = "FROZEN_SOURCE_MISMATCH"
    RUNNER_FAILURE = "RUNNER_FAILURE"
    INVALID_OUTCOME = "INVALID_OUTCOME"
    COMPLETED = "COMPLETED"


BLOCKING_VERDICTS = {
    Verdict.OPEN_CONTRACT_ISSUE, Verdict.ARCHITECTURE_DECISION_REQUIRED,
    Verdict.SECURITY_SENSITIVE_ACTION, Verdict.DESTRUCTIVE_ACTION_REQUIRED,
    Verdict.UNAUTHORIZED_EXTERNAL_SIDE_EFFECT, Verdict.FROZEN_SOURCE_MISMATCH,
    Verdict.PLAN_EXPANSION_REQUIRED,
}


class WorkflowStatus(str, Enum):
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    HARD_STOPPED = "HARD_STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class AuthoritativeSource:
    path: str
    sha256: str
    git_commit: str | None = None
    git_tag: str | None = None
    mutable_after_start: bool = False


@dataclass(frozen=True)
class SkillSpec:
    path: str
    expected_version: str | None = None


@dataclass(frozen=True)
class TaskSpec:
    id: str
    expected_outputs: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class GroupSpec:
    id: str
    tasks: tuple[TaskSpec, ...]


@dataclass(frozen=True)
class RunnerConfig:
    type: str = "mock"
    command: str | None = None
    timeout_seconds: int = 900
    mock_outcomes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    auto_patch: bool = True
    auto_rereview: bool = True
    auto_plan_defect_resolution: bool = True
    auto_plan_revision_review: bool = True
    auto_commit_checkpoint: bool = False
    auto_push: bool = False
    auto_tag: bool = False
    max_attempts_per_stage: int = 3
    max_total_steps: int = 100
    retry_backoff_seconds: float = 0.25
    retry_max_delay_seconds: float = 5.0


@dataclass(frozen=True)
class WorkflowConfig:
    version: str
    project_name: str
    project_path: str
    mode: str
    authoritative_sources: tuple[AuthoritativeSource, ...]
    skills: dict[str, SkillSpec]
    runner: RunnerConfig
    policy: Policy
    groups: tuple[GroupSpec, ...]
    hard_stops: tuple[str, ...] = ()
    workflow_file: str = ""
    digest: str = ""

    @property
    def tasks(self) -> tuple[tuple[str, TaskSpec], ...]:
        return tuple((group.id, task) for group in self.groups for task in group.tasks)

    def task_at(self, group_id: str | None, task_id: str | None) -> TaskSpec | None:
        return next((task for gid, task in self.tasks if gid == group_id and task.id == task_id), None)


@dataclass
class WorkflowState:
    schema_version: str = "1.0"
    project: str = ""
    project_path: str = ""
    workflow_file: str = ""
    workflow_digest: str = ""
    current_stage: str = Stage.INITIALIZING.value
    current_group: str | None = None
    current_task: str | None = None
    run_id: str | None = None
    attempt: int = 0
    total_steps: int = 0
    last_outcome: dict[str, Any] | None = None
    last_successful_stage: str | None = None
    pending_human_gate: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    status: str = WorkflowStatus.RUNNING.value
    stop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkflowState":
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: item for key, item in value.items() if key in allowed})


@dataclass(frozen=True)
class StepResult:
    state: WorkflowState
    action: str
    retry_delay: float = 0.0
