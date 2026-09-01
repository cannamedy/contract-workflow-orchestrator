from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .models import AuthoritativeSource, GroupSpec, Policy, RunnerConfig, SkillSpec, TaskSpec, WorkflowConfig


class WorkflowConfigError(ValueError):
    pass


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkflowConfigError(f"{name} must be a mapping")
    return value


def _task(value: Any, group: str, index: int) -> TaskSpec:
    if isinstance(value, str):
        return TaskSpec(id=value)
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise WorkflowConfigError(f"groups[{group}].tasks[{index}] requires an id")
    return TaskSpec(id=value["id"], expected_outputs=tuple(value.get("expected_outputs", ()) or ()), allowed_paths=tuple(value.get("allowed_paths", ()) or ()))


def load_workflow(path: str | Path, project_override: str | Path | None = None) -> WorkflowConfig:
    workflow_path = Path(path).expanduser().resolve()
    if not workflow_path.is_file():
        raise WorkflowConfigError(f"workflow file does not exist: {workflow_path}")
    try:
        raw = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise WorkflowConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkflowConfigError("workflow root must be a mapping")
    project = _mapping(raw.get("project"), "project")
    project_name = project.get("name")
    if not isinstance(project_name, str) or not project_name:
        raise WorkflowConfigError("project.name is required")
    project_path = Path(project_override or project.get("path") or workflow_path.parent).expanduser().resolve()
    mode = raw.get("mode", "gated")
    if isinstance(mode, dict):
        mode = mode.get("value", "gated")
    if mode not in {"gated", "autonomous"}:
        raise WorkflowConfigError("mode must be gated or autonomous")
    sources: list[AuthoritativeSource] = []
    for index, item in enumerate(raw.get("authoritative_sources", []) or []):
        source = _mapping(item, f"authoritative_sources[{index}]")
        if not isinstance(source.get("path"), str) or not isinstance(source.get("sha256"), str):
            raise WorkflowConfigError("authoritative source requires path and sha256")
        sources.append(AuthoritativeSource(path=source["path"], sha256=source["sha256"], git_commit=source.get("git_commit"), git_tag=source.get("git_tag"), mutable_after_start=bool(source.get("mutable_after_start", False))))
    skill_raw = _mapping(raw.get("skills"), "skills")
    skills: dict[str, SkillSpec] = {}
    for role, item in skill_raw.items():
        spec = _mapping(item, f"skills.{role}")
        if not isinstance(spec.get("path"), str):
            raise WorkflowConfigError(f"skills.{role}.path is required")
        skills[role] = SkillSpec(spec["path"], spec.get("expected_version"))
    runner_raw = _mapping(raw.get("runner"), "runner")
    runner_type = runner_raw.get("type", "mock")
    if runner_type not in {"mock", "codex_cli"}:
        raise WorkflowConfigError("runner.type must be mock or codex_cli")
    runner = RunnerConfig(type=runner_type, command=runner_raw.get("command"), timeout_seconds=int(runner_raw.get("timeout_seconds", 900)), mock_outcomes=runner_raw.get("mock_outcomes", {}) or {})
    policy_raw = _mapping(raw.get("policy"), "policy")
    policy = Policy(**{key: policy_raw[key] for key in Policy.__dataclass_fields__ if key in policy_raw})
    if policy.max_attempts_per_stage < 1 or policy.max_total_steps < 1:
        raise WorkflowConfigError("retry and step limits must be positive")
    groups: list[GroupSpec] = []
    for index, item in enumerate(raw.get("groups", []) or []):
        group = _mapping(item, f"groups[{index}]")
        if not isinstance(group.get("id"), str):
            raise WorkflowConfigError(f"groups[{index}].id is required")
        tasks = tuple(_task(value, group["id"], task_index) for task_index, value in enumerate(group.get("tasks", []) or []))
        groups.append(GroupSpec(group["id"], tasks))
    if not groups or not any(group.tasks for group in groups):
        raise WorkflowConfigError("at least one task is required")
    return WorkflowConfig(version=str(raw.get("version", "1")), project_name=project_name, project_path=str(project_path), mode=mode, authoritative_sources=tuple(sources), skills=skills, runner=runner, policy=policy, groups=tuple(groups), hard_stops=tuple(raw.get("hard_stops", ()) or ()), workflow_file=str(workflow_path), digest=digest_file(workflow_path))


def default_workflow(project: Path) -> str:
    return f'''version: "1"
project:
  name: "{project.name}"
  path: "{project}"
mode: gated
authoritative_sources: []
skills: {{}}
runner:
  type: mock
  timeout_seconds: 900
  mock_outcomes:
    TASK_EXECUTION:
      verdict: APPROVED
    TASK_INDEPENDENT_REVIEW:
      verdict: APPROVED
    FINAL_VERIFICATION:
      verdict: COMPLETED
policy:
  auto_patch: true
  auto_rereview: true
  auto_plan_defect_resolution: true
  auto_plan_revision_review: true
  auto_commit_checkpoint: false
  auto_push: false
  auto_tag: false
  max_attempts_per_stage: 3
  max_total_steps: 100
groups:
  - id: default
    tasks:
      - id: sample-task
'''


def workflow_schema_errors(path: str | Path) -> list[str]:
    """Validate the raw YAML against the checked-in JSON Schema when available."""
    workflow_path = Path(path)
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "workflow.schema.json"
    if not schema_path.is_file():
        return []
    try:
        from jsonschema import Draft202012Validator
        document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        return [error.message for error in Draft202012Validator(schema).iter_errors(document)]
    except ImportError:
        # The loader performs the same required-field/type gate without the optional validator.
        return []
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return [f"schema validation unavailable: {exc}"]
