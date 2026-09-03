from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .artifacts import validate_artifact_graph
from .models import ARTIFACT_KINDS, ArtifactSpec, AuthoritativeSource, GroupSpec, Policy, RunnerConfig, SkillSpec, TaskSpec, WorkflowConfig


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


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise WorkflowConfigError("task list fields must contain strings")
    return tuple(value)


def _task(value: Any, group: str, index: int) -> TaskSpec:
    if isinstance(value, str):
        return TaskSpec(id=value)
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise WorkflowConfigError(f"groups[{group}].tasks[{index}] requires an id")
    return TaskSpec(
        id=value["id"],
        expected_outputs=_strings(value.get("expected_outputs")),
        allowed_paths=_strings(value.get("allowed_paths")),
        dependencies=_strings(value.get("dependencies", value.get("depends_on"))),
        requirement_ids=_strings(value.get("requirement_ids")),
        contract_anchors=_strings(value.get("contract_anchors")),
        skill_role=value.get("skill_role"),
        engineering_spec_anchors=_strings(value.get("engineering_spec_anchors")),
        machine_contract_refs=_strings(value.get("machine_contract_refs")),
        conformance_ids=_strings(value.get("conformance_ids")),
        implementation_design_refs=_strings(value.get("implementation_design_refs")),
    )


def _safe_branch(value: str) -> bool:
    return bool(value) and not value.startswith("/") and not value.endswith("/") and ".." not in value.split("/") and not any(char.isspace() or char in "~^\\:" for char in value)


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
    authority_raw = _mapping(raw.get("authority"), "authority")
    authority_remote = authority_raw.get("remote", raw.get("authority_remote", "origin"))
    authority_branch = authority_raw.get("branch", raw.get("authority_branch", "main"))
    if not isinstance(authority_remote, str) or not authority_remote:
        raise WorkflowConfigError("authority.remote must be a non-empty string")
    if not isinstance(authority_branch, str) or not _safe_branch(authority_branch):
        raise WorkflowConfigError("authority.branch must be a safe non-empty branch name")
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
        sources.append(AuthoritativeSource(path=source["path"], sha256=source["sha256"], git_commit=source.get("git_commit"), git_tag=source.get("git_tag"), mutable_after_start=bool(source.get("mutable_after_start", False)), source_id=source.get("source_id"), role=source.get("role")))
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
    task_ids = [task.id for _, task in ((group.id, task) for group in groups for task in group.tasks)]
    if len(task_ids) != len(set(task_ids)):
        raise WorkflowConfigError("task ids must be globally unique")
    known_tasks = set(task_ids)
    for group in groups:
        for task in group.tasks:
            unknown = set(task.dependencies) - known_tasks
            if unknown:
                raise WorkflowConfigError(f"task {task.id} depends on unknown task(s): {', '.join(sorted(unknown))}")
    artifact_raw = raw.get("artifact_pipeline")
    artifact_explicit = artifact_raw is not None
    artifact_values = artifact_raw.get("artifacts", artifact_raw.get("nodes", [])) if isinstance(artifact_raw, dict) else artifact_raw
    artifact_specs: list[ArtifactSpec] = []
    if artifact_values is not None:
        if not isinstance(artifact_values, list):
            raise WorkflowConfigError("artifact_pipeline must be an array or an object containing artifacts")
        for index, item in enumerate(artifact_values):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("kind"), str):
                raise WorkflowConfigError(f"artifact_pipeline[{index}] requires id and kind")
            artifact_specs.append(ArtifactSpec(
                id=item["id"], kind=item["kind"], dependencies=_strings(item.get("dependencies", item.get("depends_on"))),
                optional=bool(item.get("optional", False)), enabled=bool(item.get("enabled", True)), skill_role=item.get("skill_role"),
                review_required=bool(item.get("review_required", True)), validator_role=item.get("validator_role"),
                candidate_path=item.get("candidate_path"), accepted_path=item.get("accepted_path"), derived_from=_strings(item.get("derived_from")),
            ))
        graph_errors = validate_artifact_graph(artifact_specs)
        if graph_errors:
            raise WorkflowConfigError("; ".join(graph_errors))
    return WorkflowConfig(version=str(raw.get("version", "1")), project_name=project_name, project_path=str(project_path), mode=mode, authoritative_sources=tuple(sources), skills=skills, runner=runner, policy=policy, groups=tuple(groups), hard_stops=tuple(raw.get("hard_stops", ()) or ()), authority_remote=authority_remote, authority_branch=authority_branch, artifact_pipeline=tuple(artifact_specs), artifact_pipeline_explicit=artifact_explicit, workflow_file=str(workflow_path), digest=digest_file(workflow_path))


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
