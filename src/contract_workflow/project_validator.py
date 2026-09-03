from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ArtifactStatus, ArtifactSpec, EngineeringArtifact, ProjectValidatorConfig, WorkflowConfig, WorkflowState, now_iso
from .workspace import RunWorkspace, WorkspaceError, tree_fingerprint


VALIDATOR_STATUSES = frozenset({"PASS", "PASS_WITH_WARNINGS", "FAIL", "ARTIFACT_MISSING"})
PASS_STATUSES = frozenset({"PASS", "PASS_WITH_WARNINGS"})
ARTIFACT_FAILURE_STATUSES = frozenset({"FAIL", "ARTIFACT_MISSING"})
PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]+)\}")
FORBIDDEN_COMMAND_CHARS = frozenset(";&|<>`\n\r")
# This is the only built-in CWO validator role. It is validated by the
# existing deterministic Plan Graph code, not by a project subprocess.
INTERNAL_VALIDATOR_ROLES = frozenset({"plan_graph_validator"})


def requires_project_validation(role: str | None) -> bool:
    return bool(role) and role not in INTERNAL_VALIDATOR_ROLES


@dataclass(frozen=True)
class ProjectValidatorResult:
    """Validated project-validator execution, including infrastructure failures."""

    kind: str
    evidence: dict[str, Any]
    code: str | None = None
    message: str = ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: str) -> Path:
    value = Path(path)
    if value.is_absolute() or ".." in value.parts or ".git" in value.parts:
        raise WorkspaceError(f"unsafe project-validator materialization path: {path}")
    return value


def _logical_path(config: WorkflowConfig, raw: str | None) -> Path | None:
    if not raw:
        return None
    value = Path(raw)
    return value if value.is_absolute() else Path(config.project_path).resolve() / value


def _project_relative(config: WorkflowConfig, raw: str | None) -> Path | None:
    if not raw:
        return None
    value = Path(raw)
    project = Path(config.project_path).resolve()
    if value.is_absolute():
        resolved = value.resolve()
        if not resolved.is_relative_to(project):
            return None
        return resolved.relative_to(project)
    return _safe_relative(raw)


def _authority_destination(config: WorkflowConfig, spec: ArtifactSpec) -> Path | None:
    if spec.accepted_path:
        relative = _project_relative(config, spec.accepted_path)
        if relative is not None:
            return relative
    if spec.kind == "HUMAN_GUIDE":
        for source in config.authoritative_sources:
            if (source.role or "").upper() == "HUMAN_GUIDE" or source.source_id == "human-guide":
                return _project_relative(config, source.path)
    return None


def _materialize_file(workspace: RunWorkspace, source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"materialization source does not exist: {source}")
    target = workspace.path / _safe_relative(destination.as_posix())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())


def _configured_command(config: WorkflowConfig, role: str, workspace: Path) -> tuple[str, ...]:
    validator_config: ProjectValidatorConfig | None = config.project_validators
    if validator_config is None:
        raise ValueError("project validator configuration is absent")
    if validator_config.roles and role not in validator_config.roles:
        raise ValueError(f"project validator role is not configured: {role}")
    template = validator_config.invocation
    placeholders = set(PLACEHOLDER_PATTERN.findall(template))
    unknown = placeholders - {"entrypoint", "validator_role", "project"}
    if unknown:
        raise ValueError(f"project validator invocation has unsupported placeholders: {', '.join(sorted(unknown))}")
    try:
        tokens = shlex.split(template)
    except ValueError as exc:
        raise ValueError(f"project validator invocation cannot be parsed: {exc}") from exc
    values = {
        "entrypoint": validator_config.entrypoint,
        "validator_role": role,
        "project": str(workspace),
    }
    command: list[str] = []
    for token in tokens:
        for character in FORBIDDEN_COMMAND_CHARS:
            if character in token:
                raise ValueError("project validator invocation contains shell control characters")
        command.append(PLACEHOLDER_PATTERN.sub(lambda match: values[match.group(1)], token))
    return tuple(command)


def _write_text(path: Path, value: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", errors="replace")
    return str(path)


def _base_evidence(
    *,
    artifact: EngineeringArtifact,
    spec: ArtifactSpec,
    role: str,
    candidate_hash: str,
    upstream_hashes: list[dict[str, Any]],
    validation_id: str,
) -> dict[str, Any]:
    return {
        "validator_role": role,
        "artifact_id": artifact.id,
        "artifact_kind": artifact.kind,
        "candidate_hash": candidate_hash,
        "upstream_hashes": upstream_hashes,
        "validator": role,
        "validation_id": validation_id,
        "executed_at": now_iso(),
        "promotion_policy": spec.promotion_policy,
    }


def execute_project_validator(
    config: WorkflowConfig,
    state: WorkflowState,
    artifact: EngineeringArtifact,
    spec: ArtifactSpec,
    *,
    state_root: Path,
    upstream_hashes: list[dict[str, Any]],
    timeout_seconds: int,
) -> ProjectValidatorResult:
    """Run a configured project validator against an exact candidate projection."""
    role = artifact.validator_role or spec.validator_role
    candidate_hash = artifact.candidate_hash
    validation_id = uuid.uuid4().hex
    evidence = _base_evidence(
        artifact=artifact,
        spec=spec,
        role=role or "",
        candidate_hash=candidate_hash or "",
        upstream_hashes=upstream_hashes,
        validation_id=validation_id,
    )
    validation_dir = state_root / "validator-runs" / validation_id
    validation_dir.mkdir(parents=True, exist_ok=True)
    evidence["evidence_dir"] = str(validation_dir)
    if not role:
        return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", "validator_role is required for external validation")
    if not isinstance(candidate_hash, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", candidate_hash):
        return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", "candidate hash is invalid")
    candidate = _logical_path(config, artifact.candidate_path)
    if candidate is None or not candidate.is_file():
        return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", "candidate artifact is missing")
    if _sha256(candidate) != candidate_hash:
        return ProjectValidatorResult("INFRA_FAIL", evidence, "VALIDATOR_CANDIDATE_HASH_MISMATCH", "candidate file hash does not match artifact candidate_hash")

    workspace: RunWorkspace | None = None
    try:
        workspace = RunWorkspace.create(Path(config.project_path), state_root, f"validator-{validation_id}")
        destination = _authority_destination(config, spec)
        if destination is None:
            raise WorkspaceError(f"artifact {artifact.id} has no project-relative accepted_path for validator materialization")
        _materialize_file(workspace, candidate, destination)

        # External accepted artifacts (notably remote Human Guide snapshots)
        # are projected at their configured project path. Accepted artifacts
        # already present in the project remain part of the workspace snapshot.
        for dependency_id in spec.dependencies:
            dependency_spec = next((item for item in config.artifact_pipeline if item.id == dependency_id), None)
            dependency = state.artifacts.get(dependency_id)
            if dependency_spec is None or dependency is None:
                continue
            source_raw = dependency.accepted_path
            if dependency.status in {ArtifactStatus.APPROVED.value, ArtifactStatus.PROMOTION_READY.value} and dependency.candidate_path:
                source_raw = dependency.candidate_path
            source_path = _logical_path(config, source_raw)
            dependency_destination = _authority_destination(config, dependency_spec)
            if source_path and dependency_destination and not source_path.resolve().is_relative_to(Path(config.project_path).resolve()):
                _materialize_file(workspace, source_path, dependency_destination)

        # Materialization is CWO-owned setup, not validator mutation.
        workspace.baseline = tree_fingerprint(workspace.path)
        command = _configured_command(config, role, workspace.path)
        evidence["command"] = list(command)
        entrypoint = workspace.path / _safe_relative(config.project_validators.entrypoint if config.project_validators else "")
        if entrypoint.is_file():
            evidence["validator_entrypoint_sha256"] = _sha256(entrypoint)
        completed = subprocess.run(
            list(command),
            cwd=workspace.path,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        stdout_path = validation_dir / "stdout.log"
        stderr_path = validation_dir / "stderr.log"
        evidence["stdout_path"] = _write_text(stdout_path, completed.stdout)
        evidence["stderr_path"] = _write_text(stderr_path, completed.stderr)
        evidence["exit_code"] = completed.returncode
        changes = workspace.diff()
        evidence["workspace_diff"] = changes
        if changes:
            return ProjectValidatorResult("INFRA_FAIL", evidence, "VALIDATOR_MUTATED_WORKSPACE", "project validator mutated its read-only workspace")
        if not workspace.real_unchanged():
            return ProjectValidatorResult("INFRA_FAIL", evidence, "REAL_PROJECT_CHANGED_DURING_RUN", "real project changed while validator was running")
        try:
            result = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", f"validator output is not structured JSON: {exc}")
        if not isinstance(result, dict):
            return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", "validator JSON result must be an object")
        status = result.get("status")
        source_hash = result.get("source_sha256")
        if result.get("validator") not in {None, role}:
            return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", "validator result role does not match configured role")
        if status not in VALIDATOR_STATUSES:
            return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", "validator result has an unsupported status")
        evidence.update({key: result[key] for key in ("status", "artifact", "findings", "coverage") if key in result})
        evidence["source_sha256"] = source_hash
        if not isinstance(source_hash, str) or source_hash.lower() != candidate_hash.lower():
            return ProjectValidatorResult("INFRA_FAIL", evidence, "VALIDATOR_CANDIDATE_HASH_MISMATCH", "validator source_sha256 does not match candidate_hash")
        if status in PASS_STATUSES and completed.returncode != 0:
            return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", "validator returned PASS with non-zero exit code")
        if status in ARTIFACT_FAILURE_STATUSES and completed.returncode == 0:
            return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", "validator returned failure status with zero exit code")
        if status in PASS_STATUSES:
            return ProjectValidatorResult("PASS", evidence)
        return ProjectValidatorResult("ARTIFACT_FAIL", evidence, status, f"project validator returned {status}")
    except subprocess.TimeoutExpired as exc:
        evidence["timeout_seconds"] = timeout_seconds
        evidence["stdout_path"] = _write_text(validation_dir / "stdout.log", (exc.stdout or "") if isinstance(exc.stdout, str) else "")
        evidence["stderr_path"] = _write_text(validation_dir / "stderr.log", (exc.stderr or "") if isinstance(exc.stderr, str) else "")
        return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", "project validator timed out")
    except (OSError, ValueError, WorkspaceError) as exc:
        return ProjectValidatorResult("INFRA_FAIL", evidence, "PROJECT_VALIDATOR_EXECUTION_FAILED", str(exc))
    finally:
        if workspace is not None:
            workspace.discard()
