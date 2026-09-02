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
    AUTHORITY_CHANGE_ANALYSIS = "AUTHORITY_CHANGE_ANALYSIS"
    HUMAN_FINAL_ACCEPTANCE = "HUMAN_FINAL_ACCEPTANCE"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    WAITING_FOR_AUTHORITY_CHANGE = "WAITING_FOR_AUTHORITY_CHANGE"
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
    Verdict.SECURITY_SENSITIVE_ACTION, Verdict.DESTRUCTIVE_ACTION_REQUIRED,
    Verdict.UNAUTHORIZED_EXTERNAL_SIDE_EFFECT, Verdict.FROZEN_SOURCE_MISMATCH,
    Verdict.PLAN_EXPANSION_REQUIRED,
}

SCOPED_DECISION_VERDICTS = {Verdict.OPEN_CONTRACT_ISSUE, Verdict.ARCHITECTURE_DECISION_REQUIRED}


class WorkflowStatus(str, Enum):
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_AUTHORITY_CHANGE = "WAITING_AUTHORITY_CHANGE"
    HARD_STOPPED = "HARD_STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class WorkItemStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    REQUIRES_PATCH = "REQUIRES_PATCH"
    BLOCKED_BY_HUMAN_DECISION = "BLOCKED_BY_HUMAN_DECISION"
    BLOCKED_BY_AUTHORITY_CHANGE = "BLOCKED_BY_AUTHORITY_CHANGE"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    FAILED = "FAILED"
    RECOVERY_UNCERTAIN = "RECOVERY_UNCERTAIN"


class DecisionStatus(str, Enum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class AuthoritativeSource:
    path: str
    sha256: str
    git_commit: str | None = None
    git_tag: str | None = None
    mutable_after_start: bool = False
    source_id: str | None = None
    role: str | None = None


@dataclass
class AuthorityChange:
    """Durable, machine-readable change record; analysis never stores chain-of-thought."""

    change_id: str
    source_path: str
    source_role: str
    base_sha256: str
    candidate_sha256: str
    detected_at: str = field(default_factory=now_iso)
    classification: str | None = None
    semantic_change: bool | None = None
    affected_requirements: list[str] = field(default_factory=list)
    affected_contract_anchors: list[str] = field(default_factory=list)
    directly_affected_tasks: list[str] = field(default_factory=list)
    dependency_affected_tasks: list[str] = field(default_factory=list)
    unaffected_tasks: list[str] = field(default_factory=list)
    machine_resolvable: bool | None = None
    human_decision_required: bool | None = None
    human_decision_requests: list[dict[str, Any]] = field(default_factory=list)
    required_propagation: list[str] = field(default_factory=list)
    analysis_summary: str = ""
    status: str = "CHANGE_PENDING"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class SkillSpec:
    path: str
    expected_version: str | None = None


@dataclass(frozen=True)
class TaskSpec:
    id: str
    expected_outputs: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    contract_anchors: tuple[str, ...] = ()
    skill_role: str | None = None


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
    stop_code: str | None = None
    blocked_stage: str | None = None
    recoverable: bool = False
    work_items: dict[str, "WorkItemState"] = field(default_factory=dict)
    decisions: dict[str, "HumanDecision"] = field(default_factory=dict)
    adrs: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_authority_change_id: str | None = None
    authority_changes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = self.__dict__.copy()
        value["work_items"] = {key: item.to_dict() for key, item in self.work_items.items()}
        value["decisions"] = {key: item.to_dict() for key, item in self.decisions.items()}
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkflowState":
        allowed = set(cls.__dataclass_fields__)
        payload = {key: item for key, item in value.items() if key in allowed}
        payload["work_items"] = {
            key: WorkItemState.from_dict(item) if isinstance(item, dict) else item
            for key, item in (payload.get("work_items") or {}).items()
        }
        payload["decisions"] = {
            key: HumanDecision.from_dict(item) if isinstance(item, dict) else item
            for key, item in (payload.get("decisions") or {}).items()
        }
        return cls(**payload)


@dataclass(frozen=True)
class StepResult:
    state: WorkflowState
    action: str
    retry_delay: float = 0.0


@dataclass
class WorkItemState:
    id: str
    group: str
    status: str = WorkItemStatus.WAITING_DEPENDENCY.value
    dependencies: tuple[str, ...] = ()
    blocking_decision_ids: list[str] = field(default_factory=list)
    dependency_blocked_by_decision_ids: list[str] = field(default_factory=list)
    blocking_authority_change_ids: list[str] = field(default_factory=list)
    dependency_blocked_by_authority_change_ids: list[str] = field(default_factory=list)
    last_outcome: dict[str, Any] | None = None
    attempt: int = 0

    def to_dict(self) -> dict[str, Any]:
        value = self.__dict__.copy()
        value["dependencies"] = list(self.dependencies)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkItemState":
        return cls(
            id=str(value.get("id", "")),
            group=str(value.get("group", "")),
            status=str(value.get("status", WorkItemStatus.WAITING_DEPENDENCY.value)),
            dependencies=tuple(value.get("dependencies", ()) or ()),
            blocking_decision_ids=list(value.get("blocking_decision_ids", ()) or ()),
            dependency_blocked_by_decision_ids=list(value.get("dependency_blocked_by_decision_ids", ()) or ()),
            blocking_authority_change_ids=list(value.get("blocking_authority_change_ids", ()) or ()),
            dependency_blocked_by_authority_change_ids=list(value.get("dependency_blocked_by_authority_change_ids", ()) or ()),
            last_outcome=value.get("last_outcome"),
            attempt=int(value.get("attempt", 0)),
        )


@dataclass(frozen=True)
class HumanDecision:
    decision_id: str
    status: str = DecisionStatus.PENDING.value
    created_at: str = field(default_factory=now_iso)
    resolved_at: str | None = None
    category: str = "ARCHITECTURE"
    question: str = ""
    context: str = ""
    why_human_required: str = ""
    options: tuple[str, ...] = ()
    recommended_option: str | None = None
    allow_freeform: bool = True
    source_change: str = ""
    source_stage: str = ""
    affected_requirements: tuple[str, ...] = ()
    affected_contract_anchors: tuple[str, ...] = ()
    affected_tasks: tuple[str, ...] = ()
    affected_work_items: tuple[str, ...] = ()
    directly_blocked_items: tuple[str, ...] = ()
    dependency_blocked_items: tuple[str, ...] = ()
    unaffected_items: tuple[str, ...] = ()
    decision: Any = None
    decision_rationale: str | None = None
    adr_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = self.__dict__.copy()
        for key in (
            "options", "affected_requirements", "affected_contract_anchors", "affected_tasks",
            "affected_work_items", "directly_blocked_items", "dependency_blocked_items", "unaffected_items",
        ):
            value[key] = list(value[key])
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "HumanDecision":
        sequence_fields = {
            "options", "affected_requirements", "affected_contract_anchors", "affected_tasks",
            "affected_work_items", "directly_blocked_items", "dependency_blocked_items", "unaffected_items",
        }
        payload = {key: item for key, item in value.items() if key in cls.__dataclass_fields__}
        for key in sequence_fields:
            payload[key] = tuple(payload.get(key, ()) or ())
        return cls(**payload)
