from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .models import ARTIFACT_KINDS, ArtifactSpec, ArtifactStatus, DecisionStatus, EngineeringArtifact, WorkflowConfig, WorkflowState


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


def _remote_human_guide_revision(config: WorkflowConfig, store: Any | None) -> dict[str, Any] | None:
    if store is None:
        return None
    source = next((item for item in config.authoritative_sources if (item.role or "").upper() == "HUMAN_GUIDE" or item.source_id == "human-guide"), None)
    if source is None:
        return None
    sid = source.source_id or "human-guide"
    ledger = store.load_authority_ledger() or {}
    entry = (ledger.get("sources", {}) or {}).get(sid, {})
    if not isinstance(entry, dict) or entry.get("status") not in {"ACCEPTED", "CHANGE_PENDING", "PROPAGATING", "WAITING_DECISION", "NEWER_REMOTE_REVISION_AVAILABLE"}:
        return None
    accepted_hash = entry.get("accepted_authority_content_sha256") or entry.get("accepted_content_sha256")
    commit = entry.get("accepted_remote_commit")
    blob = entry.get("accepted_remote_blob") or entry.get("accepted_authority_blob")
    remote = store.load_remote_state() or {}
    remote_entry = (remote.get("sources", {}) or {}).get(sid, {})
    if isinstance(remote_entry, dict):
        accepted_hash = accepted_hash or remote_entry.get("content_sha256")
        commit = commit or remote_entry.get("commit_sha")
        blob = blob or remote_entry.get("git_blob_sha")
    snapshot_raw = entry.get("accepted_snapshot_path")
    if not snapshot_raw and entry.get("status") == "ACCEPTED":
        snapshot_raw = entry.get("snapshot_path")
    if not snapshot_raw and entry.get("status") == "ACCEPTED" and isinstance(remote_entry, dict):
        snapshot_raw = remote_entry.get("snapshot_path")
    remote_url = remote_entry.get("remote_url") if isinstance(remote_entry, dict) else None
    branch = remote_entry.get("branch") if isinstance(remote_entry, dict) else None
    if not isinstance(accepted_hash, str) or not isinstance(snapshot_raw, str) or not isinstance(commit, str) or not isinstance(blob, str):
        return None
    snapshot = Path(snapshot_raw).expanduser().resolve()
    if not snapshot.is_file() or hashlib.sha256(snapshot.read_bytes()).hexdigest() != accepted_hash:
        return None
    return {
        "accepted_hash": accepted_hash,
        "snapshot_path": str(snapshot),
        "remote": remote_url or config.authority_remote,
        "branch": branch or config.authority_branch,
        "commit_sha": commit,
        "git_blob_sha": blob,
    }


def initialize_artifacts(config: WorkflowConfig, store: Any | None = None) -> dict[str, EngineeringArtifact]:
    result: dict[str, EngineeringArtifact] = {}
    root = Path(config.project_path)
    for spec in artifact_specs(config):
        accepted_hash = None
        status = ArtifactStatus.MISSING.value
        metadata: dict[str, Any] = {}
        external_revision = _remote_human_guide_revision(config, store) if spec.kind == "HUMAN_GUIDE" and spec.promotion_policy == "EXTERNAL" else None
        if external_revision:
            accepted_hash = external_revision["accepted_hash"]
            status = ArtifactStatus.ACCEPTED.value
            metadata["accepted_source"] = {"kind": "GIT_REMOTE", **external_revision}
        elif spec.accepted_path:
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
            promotion_policy=spec.promotion_policy,
            consume_approved_dependencies=spec.consume_approved_dependencies,
            accepted_path=(external_revision["snapshot_path"] if external_revision else spec.accepted_path), candidate_path=spec.candidate_path,
            metadata=metadata,
        )
    for spec in artifact_specs(config):
        if spec.dependencies and spec.id in result:
            result[spec.id].metadata["dependency_revisions"] = [
                {
                    "artifact_id": dependency_id,
                    "status": result[dependency_id].status,
                    "hash": result[dependency_id].accepted_hash,
                    "accepted_hash": result[dependency_id].accepted_hash,
                    "candidate_hash": result[dependency_id].candidate_hash,
                }
                for dependency_id in spec.dependencies
                if dependency_id in result
            ]
    return result


def hydrate_external_artifacts(config: WorkflowConfig, state: WorkflowState, store: Any) -> WorkflowState:
    """Refresh accepted external revisions without reading local draft files."""
    projected = initialize_artifacts(config, store)
    artifacts = dict(state.artifacts)
    changed = False
    for spec in artifact_specs(config):
        if spec.kind != "HUMAN_GUIDE" or spec.promotion_policy != "EXTERNAL":
            continue
        candidate = projected.get(spec.id)
        current = artifacts.get(spec.id)
        if not candidate or candidate.status != ArtifactStatus.ACCEPTED.value:
            continue
        if current is None or current.status in {ArtifactStatus.MISSING.value, ArtifactStatus.ACCEPTED.value}:
            if current is None or current.to_dict() != candidate.to_dict():
                artifacts[spec.id] = candidate
                changed = True
    return state if not changed else replace(state, artifacts=artifacts)


def dependency_revisions(config: WorkflowConfig, state: WorkflowState, spec: ArtifactSpec) -> list[dict[str, Any]]:
    revisions: list[dict[str, Any]] = []
    for dependency_id in spec.dependencies:
        dependency = state.artifacts.get(dependency_id)
        if dependency is None:
            revisions.append({"artifact_id": dependency_id, "status": ArtifactStatus.MISSING.value, "hash": None, "accepted_hash": None, "candidate_hash": None})
            continue
        revision_hash = dependency.accepted_hash
        if dependency.status in {ArtifactStatus.APPROVED.value, ArtifactStatus.PROMOTION_READY.value}:
            revision_hash = dependency.candidate_hash
        revisions.append({"artifact_id": dependency_id, "status": dependency.status, "hash": revision_hash, "accepted_hash": dependency.accepted_hash, "candidate_hash": dependency.candidate_hash})
    return revisions


def _dependencies_satisfied(config: WorkflowConfig, state: WorkflowState, spec: ArtifactSpec) -> bool:
    for dependency_id in spec.dependencies:
        dependency_spec = next((item for item in artifact_specs(config) if item.id == dependency_id), None)
        dependency = state.artifacts.get(dependency_id)
        if dependency_spec and dependency_spec.optional and not dependency_spec.enabled:
            continue
        if dependency is None or dependency.status == ArtifactStatus.ACCEPTED.value:
            if dependency is None:
                return False
            continue
        if dependency.status == ArtifactStatus.APPROVED.value and spec.consume_approved_dependencies:
            continue
        return False
    return True


def dependency_revisions_match(recorded: Any, current: list[dict[str, Any]]) -> bool:
    """Compare derivation identity, not a transient APPROVED/ACCEPTED status."""
    if not isinstance(recorded, list) or len(recorded) != len(current):
        return False
    for old, new in zip(recorded, current):
        if not isinstance(old, dict) or old.get("artifact_id") != new.get("artifact_id") or old.get("hash") != new.get("hash"):
            return False
    return True


def next_artifact(config: WorkflowConfig, state: WorkflowState) -> ArtifactSpec | None:
    specs = artifact_specs(config)
    for spec in specs:
        current = state.artifacts.get(spec.id)
        if not current or current.status in {
            ArtifactStatus.SUPERSEDED.value,
            ArtifactStatus.APPROVED.value,
            ArtifactStatus.PROMOTION_READY.value,
            ArtifactStatus.ACCEPTED.value,
            ArtifactStatus.BLOCKED.value,
        }:
            continue
        if not spec.enabled and spec.optional:
            continue
        if _dependencies_satisfied(config, state, spec):
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


def reconcile_artifact_staleness(config: WorkflowConfig, state: WorkflowState) -> WorkflowState:
    """Reset downstream candidates whose recorded input revisions changed."""
    changed: dict[str, EngineeringArtifact] = dict(state.artifacts)
    decisions = dict(state.decisions)
    for spec in artifact_specs(config):
        artifact = changed.get(spec.id)
        if not artifact or not spec.dependencies:
            continue
        recorded = artifact.metadata.get("dependency_revisions")
        if dependency_revisions_match(recorded, dependency_revisions(config, state, spec)):
            continue
        if artifact.status in {ArtifactStatus.MISSING.value, ArtifactStatus.PENDING.value, ArtifactStatus.SUPERSEDED.value}:
            continue
        changed[spec.id] = EngineeringArtifact(
            **{
                **artifact.to_dict(),
                "status": ArtifactStatus.PENDING.value,
                "candidate_hash": None,
                "candidate_path": None,
                "metadata": {**artifact.metadata, "stale_reason": "DOWNSTREAM_STALE", "previous_dependency_revisions": recorded},
            }
        )
        for decision_id, decision in decisions.items():
            if decision.status == DecisionStatus.PENDING.value and decision.source_artifact_id == spec.id:
                decisions[decision_id] = replace(decision, status=DecisionStatus.SUPERSEDED.value)
    return state if changed == state.artifacts and decisions == state.decisions else replace(state, artifacts=changed, decisions=decisions)


def _resolve_artifact_path(config: WorkflowConfig, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    return Path(config.project_path) / path


def validate_artifact_promotion(config: WorkflowConfig, state: WorkflowState, artifact_id: str, *, allow_external: bool = False) -> list[str]:
    """Validate all deterministic preconditions without changing state."""
    spec = next((item for item in artifact_specs(config) if item.id == artifact_id), None)
    artifact = state.artifacts.get(artifact_id)
    if spec is None or artifact is None:
        return [f"unknown artifact: {artifact_id}"]
    if spec.promotion_policy == "EXTERNAL" and not allow_external:
        return [f"artifact {artifact_id} has EXTERNAL promotion policy"]
    if artifact.status not in {ArtifactStatus.APPROVED.value, ArtifactStatus.PROMOTION_READY.value}:
        return [f"artifact {artifact_id} is not promotion-ready: {artifact.status}"]
    if not isinstance(artifact.candidate_hash, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", artifact.candidate_hash):
        return [f"artifact {artifact_id} has no valid candidate hash"]
    candidate = _resolve_artifact_path(config, artifact.candidate_path)
    if candidate is None or not candidate.is_file():
        return [f"artifact {artifact_id} candidate is missing"]
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != artifact.candidate_hash:
        return [f"artifact {artifact_id} candidate hash mismatch"]
    review = artifact.metadata.get("review")
    if artifact.review_required and (not isinstance(review, dict) or review.get("verdict") != "APPROVED"):
        return [f"artifact {artifact_id} has no approved semantic review evidence"]
    validator = artifact.metadata.get("validator")
    if artifact.validator_role and (not isinstance(validator, dict) or validator.get("status") != "PASS"):
        return [f"artifact {artifact_id} has no passing deterministic validator evidence"]
    if isinstance(validator, dict) and validator.get("status") == "FAIL":
        return [f"artifact {artifact_id} deterministic validator failed"]
    if not _dependencies_satisfied(config, state, spec):
        return [f"artifact {artifact_id} has unaccepted upstream dependencies"]
    expected_revisions = artifact.metadata.get("dependency_revisions")
    if spec.dependencies and not dependency_revisions_match(expected_revisions, dependency_revisions(config, state, spec)):
        return [f"DOWNSTREAM_STALE: artifact {artifact_id} upstream revisions changed"]
    if artifact.change_id:
        change = state.authority_changes.get(artifact.change_id)
        if not isinstance(change, dict) or change.get("status") in {"SUPERSEDED", "REJECTED"}:
            return [f"artifact {artifact_id} Change Record is missing or superseded"]
    if any(item.status == "PENDING" for item in state.decisions.values()):
        return ["unresolved HumanDecision exists"]
    if artifact.metadata.get("superseded_by"):
        return [f"artifact {artifact_id} has a superseding candidate"]
    accepted = _resolve_artifact_path(config, artifact.accepted_path)
    if accepted:
        if artifact.accepted_hash is None and accepted.exists():
            return [f"artifact {artifact_id} accepted target drifted before first promotion"]
        if artifact.accepted_hash is not None and (not accepted.is_file() or hashlib.sha256(accepted.read_bytes()).hexdigest() != artifact.accepted_hash):
            return [f"artifact {artifact_id} accepted target drifted"]
    return []


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
    if raw.get("validator") is not None and not isinstance(raw.get("validator"), dict):
        errors.append("artifact.validator must be an object")
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
