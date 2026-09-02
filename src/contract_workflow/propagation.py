from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import Stage, WorkflowConfig, WorkflowState
from .plan_graph import validate_plan_graph


PROPAGATION_STAGES = {
    Stage.CHANGE_PROPAGATION_PLANNING.value,
    Stage.CONTRACT_REVISION.value,
    Stage.CONTRACT_REVISION_REVIEW.value,
    Stage.PLAN_REVISION.value,
    Stage.PLAN_REVISION_REVIEW.value,
    Stage.PLAN_GRAPH_BUILD.value,
    Stage.TASK_REBASE_ANALYSIS.value,
}


def propagation_steps(change: dict[str, Any]) -> list[str]:
    required = set(change.get("required_propagation", ()))
    steps = [Stage.CHANGE_PROPAGATION_PLANNING.value]
    if "CONTRACT_REVISION_REQUIRED" in required:
        steps.extend([Stage.CONTRACT_REVISION.value, Stage.CONTRACT_REVISION_REVIEW.value])
    if "CONTRACT_REVISION_REQUIRED" in required or "PLAN_REVISION_REQUIRED" in required or "PLAN_GRAPH_REQUIRED" in required:
        steps.extend([Stage.PLAN_REVISION.value, Stage.PLAN_REVISION_REVIEW.value, Stage.PLAN_GRAPH_BUILD.value])
    elif "PLAN_GRAPH_REQUIRED" not in required:
        steps.append(Stage.PLAN_GRAPH_BUILD.value)
    if "TASK_REBASE_REQUIRED" in required or change.get("directly_affected_tasks"):
        steps.append(Stage.TASK_REBASE_ANALYSIS.value)
    return steps


def canonical_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def safe_project_path(project: Path, relative: str) -> Path | None:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        return None
    path = (project / relative).resolve()
    return path if path.is_relative_to(project.resolve()) else None


def validate_candidate_artifacts(config: WorkflowConfig, value: Any, *, required_path: str | None = None) -> tuple[list[dict[str, Any]] | None, list[str]]:
    if not isinstance(value, list):
        return None, ["candidate_artifacts must be an array of {path, content} objects"]
    errors: list[str] = []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    project = Path(config.project_path)
    for index, item in enumerate(value):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
            errors.append(f"candidate_artifacts[{index}] requires string path and content")
            continue
        path = item["path"]
        if safe_project_path(project, path) is None:
            errors.append(f"candidate artifact path is unsafe: {path}")
        if ".contract-workflow" in Path(path).parts:
            errors.append(f"candidate artifact cannot modify CWO control state: {path}")
        if path in seen:
            errors.append(f"duplicate candidate artifact: {path}")
        seen.add(path)
        result.append({"path": path, "content": item["content"], "sha256": hashlib.sha256(item["content"].encode()).hexdigest()})
    if required_path and required_path not in seen:
        errors.append(f"candidate artifact missing required path: {required_path}")
    return (result, errors) if not errors else (None, errors)


def source_path_for_role(config: WorkflowConfig, role: str) -> str | None:
    for source in config.authoritative_sources:
        if (source.role or "").upper() == role.upper():
            return source.path
    names = {"ENGINEERING_CONTRACT": ("contract", "契约"), "IMPLEMENTATION_PLAN": ("plan", "计划")}
    needles = names.get(role.upper(), ())
    for source in config.authoritative_sources:
        if any(needle in source.path.lower() for needle in needles):
            return source.path
    return None


def contract_text(config: WorkflowConfig, state: WorkflowState) -> str:
    chunks: list[str] = []
    root = Path(config.project_path)
    for source in config.authoritative_sources:
        if source.role and source.role.upper() == "ENGINEERING_CONTRACT" or "contract" in source.path.lower() or "契约" in source.path:
            path = Path(source.path) if Path(source.path).is_absolute() else root / source.path
            if path.is_file():
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    propagation = state.propagation.get(state.current_authority_change_id or "", {})
    for artifact in propagation.get("candidate_artifacts", []):
        if "contract" in str(artifact.get("path", "")).lower() and isinstance(artifact.get("content"), str):
            chunks.append(artifact["content"])
    return "\n".join(chunks)


def validate_propagation_plan(value: Any, steps: list[str]) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(value, dict):
        return None, ["propagation_plan must be an object"]
    declared = value.get("stages")
    if not isinstance(declared, list) or declared != steps:
        return None, [f"propagation_plan.stages must equal deterministic stages {steps}"]
    if not all(isinstance(item, str) for item in declared):
        return None, ["propagation_plan.stages must contain strings"]
    return {"schema_version": "1.0", "stages": steps, "summary": str(value.get("summary", ""))}, []


def validate_rebase(value: Any, affected: set[str]) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(value, dict):
        return None, ["task_rebase must be an object"]
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not all(isinstance(item, dict) and isinstance(item.get("task_id"), str) for item in tasks):
        return None, ["task_rebase.tasks must contain task_id objects"]
    ids = {item["task_id"] for item in tasks}
    unknown = ids - affected
    missing = affected - ids
    errors = [f"task rebase contains unaffected task: {item}" for item in sorted(unknown)] + [f"task rebase missing affected task: {item}" for item in sorted(missing)]
    for item in tasks:
        findings = item.get("preserved_review_findings")
        if not isinstance(findings, list) or not all(isinstance(finding, str) for finding in findings):
            errors.append(f"task rebase {item['task_id']} must preserve review findings as a string array")
    return (value, errors) if not errors else (None, errors)
