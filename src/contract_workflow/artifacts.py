from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

from .models import ARTIFACT_KINDS, ArtifactSpec, ArtifactStatus, EngineeringArtifact, WorkflowConfig, WorkflowState


def validate_artifact_graph(specs: Iterable[ArtifactSpec]) -> list[str]:
    nodes = tuple(specs)
    errors: list[str] = []
    ids = [spec.id for spec in nodes]
    if len(ids) != len(set(ids)):
        errors.append("artifact ids must be globally unique")
    known = set(ids)
    for spec in nodes:
        if not spec.id:
            errors.append("artifact id must be non-empty")
        if spec.kind not in ARTIFACT_KINDS:
            errors.append(f"unsupported artifact kind: {spec.kind}")
        for dependency in spec.dependencies:
            if dependency not in known:
                errors.append(f"unknown artifact dependency {dependency} for {spec.id}")
        if spec.accepted_path and (Path(spec.accepted_path).is_absolute() or ".." in Path(spec.accepted_path).parts):
            errors.append(f"unsafe artifact accepted_path: {spec.accepted_path}")
        if spec.candidate_path and (Path(spec.candidate_path).is_absolute() or ".." in Path(spec.candidate_path).parts):
            errors.append(f"unsafe artifact candidate_path: {spec.candidate_path}")
    indegree = {item: 0 for item in known}
    edges = {item: [] for item in known}
    for spec in nodes:
        for dependency in spec.dependencies:
            if dependency in known:
                indegree[spec.id] += 1
                edges[dependency].append(spec.id)
    queue = [item for item, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for child in edges[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(known):
        errors.append("artifact graph contains a dependency cycle")
    return errors


def artifact_specs(config: WorkflowConfig) -> tuple[ArtifactSpec, ...]:
    return config.artifact_pipeline if config.artifact_pipeline_explicit else ()


def effective_artifact_specs(config: WorkflowConfig) -> tuple[ArtifactSpec, ...]:
    """Return the configured graph or a descriptive legacy projection.

    The legacy projection is reported for traceability only; the scheduler
    activates typed artifact scheduling only for an explicit graph.
    """
    return artifact_specs(config) if config.artifact_pipeline_explicit else legacy_artifact_specs(config)


def missing_skill_roles(config: WorkflowConfig) -> list[str]:
    return sorted({spec.skill_role for spec in artifact_specs(config) if spec.skill_role and spec.skill_role not in config.skills})


def legacy_artifact_specs(config: WorkflowConfig) -> tuple[ArtifactSpec, ...]:
    """Describe the old Contract -> Plan pipeline without activating generic scheduling."""
    guide = next((source.path for source in config.authoritative_sources if "human" in source.path.lower() or "架构原理" in source.path), None)
    contract = next((source.path for source in config.authoritative_sources if (source.role or "").upper() == "ENGINEERING_CONTRACT" or "contract" in source.path.lower() or "契约" in source.path), None)
    plan = next((source.path for source in config.authoritative_sources if (source.role or "").upper() == "IMPLEMENTATION_PLAN" or "plan" in source.path.lower() or "计划" in source.path), None)
    if not contract or not plan:
        return ()
    return (
        ArtifactSpec("human-guide", "HUMAN_GUIDE", accepted_path=guide),
        ArtifactSpec("engineering-contract", "ENGINEERING_SPEC", dependencies=("human-guide",), accepted_path=contract),
        ArtifactSpec("implementation-plan", "IMPLEMENTATION_PLAN", dependencies=("engineering-contract",), accepted_path=plan),
        ArtifactSpec("plan-graph", "PLAN_GRAPH", dependencies=("implementation-plan",)),
        ArtifactSpec("task-contract", "TASK_CONTRACT", dependencies=("plan-graph",)),
    )


def initialize_artifacts(config: WorkflowConfig) -> dict[str, EngineeringArtifact]:
    result: dict[str, EngineeringArtifact] = {}
    root = Path(config.project_path)
    for spec in artifact_specs(config):
        accepted_hash = None
        status = ArtifactStatus.MISSING.value
        if spec.accepted_path:
            path = Path(spec.accepted_path) if Path(spec.accepted_path).is_absolute() else root / spec.accepted_path
            if path.is_file():
                accepted_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                status = ArtifactStatus.ACCEPTED.value
        if not spec.enabled and spec.optional:
            status = ArtifactStatus.SUPERSEDED.value
        result[spec.id] = EngineeringArtifact(
            id=spec.id, kind=spec.kind, status=status, version_hash=accepted_hash,
            accepted_hash=accepted_hash, derived_from=list(spec.derived_from), skill_role=spec.skill_role,
            review_required=spec.review_required, validator_role=spec.validator_role,
            accepted_path=spec.accepted_path, candidate_path=spec.candidate_path,
        )
    return result


def _satisfied(status: str) -> bool:
    return status in {ArtifactStatus.APPROVED.value, ArtifactStatus.ACCEPTED.value}


def next_artifact(config: WorkflowConfig, state: WorkflowState) -> ArtifactSpec | None:
    specs = artifact_specs(config)
    by_id = {item.id: item for item in specs}
    for spec in specs:
        current = state.artifacts.get(spec.id)
        if not current or current.status in {
            ArtifactStatus.SUPERSEDED.value,
            ArtifactStatus.APPROVED.value,
            ArtifactStatus.ACCEPTED.value,
            ArtifactStatus.BLOCKED.value,
        }:
            continue
        if not spec.enabled and spec.optional:
            continue
        if all(_satisfied(state.artifacts.get(dep, EngineeringArtifact(dep, "", ArtifactStatus.MISSING.value)).status) or (by_id.get(dep) is not None and by_id[dep].optional and not by_id[dep].enabled) for dep in spec.dependencies):
            return spec
    return None


def artifact_descendants(specs: Iterable[ArtifactSpec], roots: Iterable[str]) -> set[str]:
    nodes = tuple(specs)
    result: set[str] = set()
    frontier = set(roots)
    while frontier:
        current = frontier.pop()
        for spec in nodes:
            if current in spec.dependencies and spec.id not in result and spec.id not in roots:
                result.add(spec.id)
                frontier.add(spec.id)
    return result


def artifact_impact_closure(config: WorkflowConfig, direct: Iterable[str]) -> set[str]:
    return set(direct) | artifact_descendants(artifact_specs(config), direct)


def reconcile_artifact_impact(config: WorkflowConfig, state: WorkflowState, direct: Iterable[str]) -> dict[str, Any]:
    direct_set = set(direct)
    affected = artifact_impact_closure(config, direct_set)
    current_ids = set(state.artifacts)
    new_ids = {spec.id for spec in artifact_specs(config)}
    return {
        "directly_affected": sorted(direct_set),
        "dependency_affected": sorted(affected - direct_set),
        "unaffected": sorted(new_ids - affected),
        "new": sorted(new_ids - current_ids),
        "preserved": sorted((current_ids & new_ids) - affected),
    }


def validate_artifact_outcome(config: WorkflowConfig, state: WorkflowState, raw: Any, *, stage: str) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(raw, dict):
        return None, ["artifact must be an object"]
    artifact_id = raw.get("id")
    if not isinstance(artifact_id, str) or artifact_id != state.current_artifact_id:
        return None, ["artifact.id must match current_artifact_id"]
    spec = next((item for item in artifact_specs(config) if item.id == artifact_id), None)
    if spec is None:
        return None, ["artifact.id is not in the configured artifact graph"]
    if raw.get("kind") != spec.kind:
        return None, ["artifact.kind does not match the configured artifact kind"]
    errors: list[str] = []
    if stage in {"ARTIFACT_GENERATION", "ARTIFACT_PATCH"}:
        candidate_hash = raw.get("candidate_hash")
        content = raw.get("candidate_content")
        if content is not None and not isinstance(content, str):
            errors.append("artifact.candidate_content must be a string when present")
        if content is not None:
            calculated = hashlib.sha256(content.encode()).hexdigest()
            if candidate_hash is not None and candidate_hash != calculated:
                errors.append("artifact.candidate_hash does not match candidate_content")
            candidate_hash = calculated
        if not isinstance(candidate_hash, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", candidate_hash):
            errors.append("artifact.candidate_hash must be a SHA-256 string")
        candidate_path = raw.get("candidate_path")
        if candidate_path is not None and not isinstance(candidate_path, str):
            errors.append("artifact.candidate_path must be a string")
        elif isinstance(candidate_path, str) and (Path(candidate_path).is_absolute() or ".." in Path(candidate_path).parts):
            errors.append("artifact.candidate_path is unsafe")
    if stage == "ARTIFACT_REVIEW" and raw.get("review") is not None and not isinstance(raw.get("review"), dict):
        errors.append("artifact.review must be an object")
    normalized = dict(raw)
    normalized["candidate_hash"] = candidate_hash if stage in {"ARTIFACT_GENERATION", "ARTIFACT_PATCH"} else raw.get("candidate_hash")
    return (normalized, errors) if not errors else (None, errors)


def validate_final_conformance(state: WorkflowState, outcome: dict[str, Any]) -> list[str]:
    conformance = [item for item in state.artifacts.values() if item.kind == "CONFORMANCE_SPEC"]
    if not conformance or not any(item.status in {ArtifactStatus.APPROVED.value, ArtifactStatus.ACCEPTED.value} for item in conformance):
        return ["FINAL_VERIFICATION requires an approved CONFORMANCE_SPEC artifact"]
    results = outcome.get("conformance_results")
    if not isinstance(results, list) or not results:
        return ["FINAL_VERIFICATION requires conformance_results"]
    errors: list[str] = []
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            errors.append(f"conformance_results[{index}] must be an object")
            continue
        for key in ("requirement_id", "conformance_id", "status", "evidence"):
            if not isinstance(item.get(key), str) or not item[key]:
                errors.append(f"conformance_results[{index}].{key} is required")
        if item.get("status") not in {"PASS", "FAIL"}:
            errors.append(f"conformance_results[{index}].status must be PASS or FAIL")
    return errors
