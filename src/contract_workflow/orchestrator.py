from __future__ import annotations

import json
import hashlib
import fnmatch
import os
import shutil
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import WorkflowConfigError, load_workflow, workflow_schema_errors
from .git_audit import GitAudit, audit_git, source_integrity, working_tree_paths
from .authority import AuthorityScan, authority_snapshot, dependency_tasks, scan_authority_changes, validate_analysis, bootstrap_ledger, source_id
from .artifacts import artifact_impact_closure, dependency_revisions, effective_artifact_specs, hydrate_external_artifacts, missing_skill_roles, validate_artifact_outcome, validate_artifact_promotion, validate_final_conformance, reconcile_artifact_impact
from .plan_graph import reconcile_plan_graph, validate_plan_graph
from .propagation import PROPAGATION_STAGES, canonical_digest, contract_text, propagation_steps, safe_project_path, source_path_for_role, validate_candidate_artifacts, validate_propagation_plan, validate_rebase
from .logging import EventLogger
from .models import (
    ArtifactStatus,
    DecisionStatus,
    HumanDecision,
    SCOPED_DECISION_VERDICTS,
    Stage,
    StepResult,
    Verdict,
    WorkItemStatus,
    WorkflowConfig,
    WorkflowState,
    WorkflowStatus,
    now_iso,
)
from .outcome import make_outcome, validate_outcome
from .prompt_builder import PromptBuilder
from .project_validator import ProjectValidatorResult, execute_project_validator, requires_project_validation
from .runners import AgentRunner, CodexCliRunner, MockRunner, RunnerResult
from .state_machine import AGENT_STAGES, HUMAN_GATES, approve, initial_state, stop, transition_after_outcome, transition_ready
from .state_store import StateStore, StateStoreError
from .scheduler import AGENT_STAGE_NAMES, HUMAN_GATE_NAMES, dependency_closure, pending_decisions, recompute, ready_work, schedule
from .workspace import RunWorkspace, TargetDriftError, WorkspaceError, apply_validated_diff


class OrchestratorError(RuntimeError):
    pass


READ_ONLY_RECOVERY_STAGES = frozenset({
    Stage.TASK_INDEPENDENT_REVIEW.value,
    Stage.PLAN_REVISION_REVIEW.value,
    Stage.FINAL_VERIFICATION.value,
    Stage.ARTIFACT_GENERATION.value, Stage.ARTIFACT_REVIEW.value, Stage.ARTIFACT_PATCH.value,
})

STRICT_WORKSPACE_STAGES = frozenset({
    Stage.AUTHORITY_CHANGE_ANALYSIS.value,
    Stage.CHANGE_PROPAGATION_PLANNING.value,
    Stage.CONTRACT_REVISION_REVIEW.value,
    Stage.PLAN_REVISION_REVIEW.value,
    Stage.PLAN_GRAPH_BUILD.value,
    Stage.TASK_REBASE_ANALYSIS.value,
    Stage.TASK_INDEPENDENT_REVIEW.value,
    Stage.FINAL_VERIFICATION.value,
    Stage.ARTIFACT_GENERATION.value, Stage.ARTIFACT_REVIEW.value, Stage.ARTIFACT_PATCH.value,
})
CANDIDATE_WORKSPACE_STAGES = frozenset({
    Stage.CONTRACT_REVISION.value,
    Stage.PLAN_REVISION.value,
})
PROJECT_MUTATING_STAGES = frozenset({
    Stage.TASK_EXECUTION.value,
    Stage.TASK_PATCH.value,
})


def state_root(project: Path) -> Path:
    configured = os.environ.get("CWO_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    key = str(project.resolve()).strip("/").replace("/", "_") or "root"
    return Path.home() / ".local" / "share" / "contract-workflow" / key


def _agent_process_running(workspace_path: Path) -> bool:
    """Best-effort liveness check for an interrupted local Agent invocation."""
    target = workspace_path.resolve()
    proc_root = Path("/proc")
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / "cwd").resolve()
            if cwd != target and not cwd.is_relative_to(target):
                continue
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            if "codex" in command.lower():
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


class Orchestrator:
    def __init__(self, config: WorkflowConfig, store: StateStore | None = None, runner: AgentRunner | None = None, prompt_builder: PromptBuilder | None = None):
        self.config = config
        self.store = store or StateStore(state_root(Path(config.project_path)))
        self.logger = EventLogger(self.store.events_path)
        self.runner = runner or self._configured_runner()
        self.prompt_builder = prompt_builder or PromptBuilder()

    @classmethod
    def for_project(cls, project: str | Path, workflow_file: str | Path | None = None, **kwargs: Any) -> "Orchestrator":
        project_path = Path(project).expanduser().resolve()
        workflow = Path(workflow_file or project_path / ".contract-workflow" / "workflow.yaml")
        config = load_workflow(workflow, project_override=project_path)
        return cls(config, **kwargs)

    def _configured_runner(self) -> AgentRunner:
        if self.config.runner.type == "mock":
            return MockRunner(self.config.runner.mock_outcomes)
        return CodexCliRunner(self.config.runner.command)

    def _load_or_initialize(self) -> WorkflowState:
        try:
            state = self.store.load()
        except StateStoreError:
            raise
        if state is None:
            state = initial_state(self.config, self.store)
            state = recompute(self.config, state)
            bootstrap_ledger(self.config, self.store)
            self.store.save(state)
            self.logger.emit("workflow_loaded", project=self.config.project_name, workflow_digest=self.config.digest)
        else:
            self.logger.emit("state_recovered", stage=state.current_stage, run_id=state.run_id)
            if state.workflow_digest != self.config.digest:
                state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason="workflow digest changed; hot reload is not supported", stop_code="WORKFLOW_DIGEST_CHANGED", blocked_stage=state.blocked_stage or state.current_stage, recoverable=False, updated_at=now_iso())
                self.store.save(state)
                self.logger.emit("hard_stop_entered", reason=state.stop_reason)
            else:
                state = recompute(self.config, state)
                self.store.save(state)
        return state

    def _audit_gate(self, scan: AuthorityScan | None = None) -> tuple[GitAudit, list[str], AuthorityScan]:
        scan = scan or scan_authority_changes(self.config, self.store, self._load_or_initialize_state_only())
        integrity = source_integrity(Path(self.config.project_path), self.config.authoritative_sources, scan.integrity_overrides or {})
        configured_authority_paths = tuple(str((Path(source.path) if Path(source.path).is_absolute() else Path(self.config.project_path) / source.path).resolve()) for source in self.config.authoritative_sources if not source.mutable_after_start)
        audit_state = self._load_or_initialize_state_only()
        plan_expected = tuple(path for raw in (audit_state.plan_graph or {}).get("tasks", []) if isinstance(raw, dict) for path in tuple(raw.get("expected_outputs", ()) or ()) + tuple(raw.get("allowed_paths", ()) or ()))
        active_invocation = bool(audit_state.run_id and audit_state.current_stage in AGENT_STAGE_NAMES)
        if active_invocation and audit_state.run_id:
            metadata = _read_json(self.store.run_dir(audit_state.run_id) / "metadata.json")
            if metadata.get("status") == "running":
                try:
                    workspace = RunWorkspace.from_metadata(metadata, Path(self.config.project_path))
                    active_invocation = bool(workspace and _agent_process_running(workspace.path))
                except WorkspaceError:
                    active_invocation = True
        # A digest stop before the first step has no Agent mutation to audit;
        # the current dirty tree is the invocation baseline being explicitly
        # recovered.  Once an invocation has started, a hard stop must not
        # reclassify its dirty paths as preserved user work.
        pre_invocation_digest_stop = (
            audit_state.current_stage == Stage.HARD_STOP.value
            and audit_state.stop_code == "WORKFLOW_DIGEST_CHANGED"
            and audit_state.total_steps == 0
            and audit_state.run_id is None
        )
        runner_failure_baseline = False
        if audit_state.stop_code in {"RETRY_EXHAUSTED", "RECOVERY_UNCERTAIN"} and audit_state.run_id:
            failed_run = _read_json(self.store.run_dir(audit_state.run_id) / "metadata.json")
            try:
                failed_workspace = RunWorkspace.from_metadata(failed_run, Path(self.config.project_path))
                runner_failure_baseline = bool(failed_workspace and failed_workspace.real_unchanged())
            except WorkspaceError:
                runner_failure_baseline = False
        pre_invocation_recovery = pre_invocation_digest_stop or runner_failure_baseline
        baseline_paths = () if active_invocation or (audit_state.current_stage == Stage.HARD_STOP.value and not pre_invocation_recovery) else working_tree_paths(Path(self.config.project_path))
        audit = audit_git(Path(self.config.project_path), self.config, tuple(sorted(set(scan.registered_paths) | set(configured_authority_paths))), plan_expected, baseline_paths=baseline_paths)
        self.logger.emit("doctor_check", git_blocking=audit.blocking, integrity_errors=len(integrity), classifications=[item.value for item in audit.classifications])
        return audit, integrity, scan

    def _load_or_initialize_state_only(self) -> WorkflowState:
        return self.store.load() or initial_state(self.config, self.store)

    def _authority_gate(self, state: WorkflowState) -> StepResult | None:
        # A persisted terminal stop is authoritative for this run.  In
        # particular, do not let a newly observed authority candidate reopen
        # a WORKFLOW_DIGEST_CHANGED (or other terminal) stop on every loop.
        # Recovery/approval must explicitly move a recoverable state forward.
        if state.status in {
            WorkflowStatus.HARD_STOPPED.value,
            WorkflowStatus.FAILED.value,
            WorkflowStatus.STOPPED.value,
            WorkflowStatus.COMPLETED.value,
        }:
            return None
        scan = scan_authority_changes(self.config, self.store, state)
        hydrated = hydrate_external_artifacts(self.config, state, self.store)
        if hydrated.artifacts != state.artifacts:
            state = self._save(hydrated)
        if scan.unauthorized:
            reason = "; ".join(scan.unauthorized)
            new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason=reason, stop_code="UNAUTHORIZED_AUTHORITY_MUTATION", blocked_stage=state.current_stage, recoverable=False, updated_at=now_iso())
            self.logger.emit("hard_stop_entered", reason=reason, stop_code="UNAUTHORIZED_AUTHORITY_MUTATION")
            return StepResult(self._save(new_state), "hard_stop")
        nonblocking_remote_errors = tuple(error for error in scan.errors if error.startswith("REMOTE_CHECK_FAILED") or error.startswith("REMOTE_AUTHORITY_SOURCE_MISSING"))
        if scan.errors and len(nonblocking_remote_errors) != len(scan.errors):
            reason = "; ".join(scan.errors)
            new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason=reason, stop_code="FROZEN_SOURCE_MISMATCH", blocked_stage=state.current_stage, recoverable=False, updated_at=now_iso())
            self.logger.emit("hard_stop_entered", reason=reason, stop_code="FROZEN_SOURCE_MISMATCH")
            return StepResult(self._save(new_state), "hard_stop")
        if nonblocking_remote_errors:
            self.logger.emit("remote_authority_check_failed", errors=list(nonblocking_remote_errors))
        unanalyzed = [item for item in scan.changes if str(item.get("change_id")) not in state.authority_changes or state.authority_changes.get(str(item.get("change_id")), {}).get("classification") is None]
        if unanalyzed and state.current_stage != Stage.AUTHORITY_CHANGE_ANALYSIS.value:
            change = sorted(unanalyzed, key=lambda item: str(item.get("change_id", "")))[0]
            reset_items = {
                item_id: replace(item, status=WorkItemStatus.READY.value) if item.status in {WorkItemStatus.RUNNING.value, WorkItemStatus.REQUIRES_PATCH.value} else item
                for item_id, item in state.work_items.items()
            }
            new_state = replace(state, current_stage=Stage.AUTHORITY_CHANGE_ANALYSIS.value, current_group=None, current_task=None, run_id=None, attempt=0, status=WorkflowStatus.RUNNING.value, pending_human_gate=None, stop_reason=None, stop_code=None, blocked_stage=None, recoverable=False, current_authority_change_id=str(change["change_id"]), authority_changes={**state.authority_changes, **{str(item["change_id"]): item for item in scan.changes}}, work_items=reset_items, updated_at=now_iso())
            self.logger.emit("authority_change_detected", change_id=change["change_id"], source_path=change.get("source_path"), base_sha256=change.get("base_sha256"), candidate_sha256=change.get("candidate_sha256"))
            return StepResult(self._save(new_state), "authority_change_analysis")
        if unanalyzed and state.current_stage == Stage.AUTHORITY_CHANGE_ANALYSIS.value:
            merged = {**state.authority_changes, **{str(item["change_id"]): item for item in scan.changes}}
            if merged != state.authority_changes:
                return StepResult(self._save(replace(state, authority_changes=merged, updated_at=now_iso())), "authority_change_registered")
        return None

    def _save(self, state: WorkflowState) -> WorkflowState:
        self.store.save(state)
        return state

    def step(self) -> StepResult:
        state = self._load_or_initialize()
        if state.total_steps >= self.config.policy.max_total_steps and state.status == WorkflowStatus.RUNNING.value:
            state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, stop_reason="max_total_steps exceeded", stop_code="MAX_TOTAL_STEPS", blocked_stage=state.current_stage, recoverable=False, updated_at=now_iso())
            return StepResult(self._save(state), "hard_stop")
        authority_result = self._authority_gate(state)
        if authority_result:
            return authority_result
        if state.status in {WorkflowStatus.COMPLETED.value, WorkflowStatus.FAILED.value, WorkflowStatus.STOPPED.value} or state.current_stage in HUMAN_GATES:
            return StepResult(state, "waiting")

        if state.current_stage == Stage.WAITING_FOR_HUMAN.value:
            scheduled = schedule(self.config, state)
            return StepResult(self._save(scheduled), "rescheduled" if scheduled.current_stage != state.current_stage else "waiting")

        audit, integrity, scan = self._audit_gate()
        if integrity or audit.blocking:
            reason = "; ".join(integrity) or "; ".join(item.classification.value for item in audit.changes if item.classification.value in {"FROZEN_AUTHORITY_CHANGE", "MERGE_CONFLICT", "UNEXPECTED_UNRELATED_CHANGE"})
            stop_code = "FROZEN_SOURCE_MISMATCH" if integrity else ("UNEXPECTED_UNRELATED_CHANGE" if any(item.classification.value == "UNEXPECTED_UNRELATED_CHANGE" for item in audit.changes) else ("MERGE_CONFLICT" if any(item.classification.value == "MERGE_CONFLICT" for item in audit.changes) else "GIT_AUDIT_BLOCKED"))
            new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason=reason or audit.error or "Git audit blocked", stop_code=stop_code, blocked_stage=state.current_stage, recoverable=stop_code == "UNEXPECTED_UNRELATED_CHANGE", updated_at=now_iso())
            self.logger.emit("hard_stop_entered", reason=new_state.stop_reason)
            return StepResult(self._save(new_state), "hard_stop")

        if state.current_stage == Stage.WAITING_FOR_AUTHORITY_CHANGE.value:
            scheduled = schedule(self.config, state)
            return StepResult(self._save(scheduled), "rescheduled" if scheduled.current_stage != state.current_stage else "waiting")
        if state.current_stage == Stage.ARTIFACT_VALIDATION.value:
            return self._artifact_validation_step(state)
        if state.current_stage in {Stage.INITIALIZING.value, Stage.READY.value}:
            scheduled = schedule(self.config, state)
            self.logger.emit("transition", from_stage=state.current_stage, to_stage=scheduled.current_stage)
            return StepResult(self._save(scheduled), "schedule")
        if state.current_stage in AGENT_STAGES:
            return self._agent_step(state)
        return StepResult(state, "waiting")

    def _artifact_validation_step(self, state: WorkflowState) -> StepResult:
        artifact_id = state.current_artifact_id
        spec = next((item for item in self.config.artifact_pipeline if item.id == artifact_id), None)
        artifact = state.artifacts.get(artifact_id) if artifact_id else None
        if spec is None or artifact is None:
            stopped = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, stop_reason="ARTIFACT_VALIDATION has no configured current artifact", stop_code="PROJECT_VALIDATOR_EXECUTION_FAILED", blocked_stage=Stage.ARTIFACT_VALIDATION.value, recoverable=False, updated_at=now_iso())
            return StepResult(self._save(stopped), "hard_stop")
        result: ProjectValidatorResult = execute_project_validator(
            self.config,
            state,
            artifact,
            spec,
            state_root=self.store.root,
            upstream_hashes=artifact.metadata.get("dependency_revisions", []),
            timeout_seconds=self.config.runner.timeout_seconds,
        )
        evidence = result.evidence
        metadata = {**artifact.metadata, "validator": evidence}
        if result.kind == "INFRA_FAIL":
            attempts = int(artifact.metadata.get("validator_attempts", 0)) + 1
            metadata["validator_attempts"] = attempts
            metadata["validator_error"] = {"code": result.code, "message": result.message}
            updated_artifact = replace(artifact, metadata=metadata)
            updated = replace(state, artifacts={**state.artifacts, artifact_id: updated_artifact}, last_outcome={"validator": evidence, "error_code": result.code, "summary": result.message}, updated_at=now_iso())
            self.store.save_artifact(updated_artifact.to_dict())
            if result.code == "REAL_PROJECT_CHANGED_DURING_RUN" or attempts >= self.config.policy.max_attempts_per_stage:
                stopped = replace(updated, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, stop_reason=f"{result.code}: {result.message}", stop_code=result.code or "PROJECT_VALIDATOR_EXECUTION_FAILED", blocked_stage=Stage.ARTIFACT_VALIDATION.value, recoverable=False, updated_at=now_iso())
                self.logger.emit("project_validator_execution_failed", artifact_id=artifact_id, validator_role=artifact.validator_role, code=result.code, attempts=attempts)
                return StepResult(self._save(stopped), "hard_stop")
            self.logger.emit("project_validator_retry", artifact_id=artifact_id, validator_role=artifact.validator_role, code=result.code, attempt=attempts)
            return StepResult(self._save(updated), "validator_retry", self.config.policy.retry_backoff_seconds)
        if result.kind == "ARTIFACT_FAIL":
            updated_artifact = replace(artifact, status=ArtifactStatus.REQUIRES_PATCH.value, metadata=metadata)
            updated = replace(state, artifacts={**state.artifacts, artifact_id: updated_artifact}, current_stage=Stage.ARTIFACT_PATCH.value, current_artifact_id=artifact_id, current_group=None, current_task=None, run_id=None, attempt=0, last_outcome={"verdict": Verdict.REQUIRES_PATCH.value, "summary": result.message, "validator": evidence}, status=WorkflowStatus.RUNNING.value, updated_at=now_iso())
            self.store.save_artifact(updated_artifact.to_dict())
            self.logger.emit("project_validator_failed", artifact_id=artifact_id, validator_role=artifact.validator_role, status=evidence.get("status"))
            return StepResult(self._save(updated), "artifact_validation_failed")

        status = ArtifactStatus.REVIEW_REQUIRED.value if artifact.review_required else ArtifactStatus.APPROVED.value
        updated_artifact = replace(artifact, status=status, metadata={**metadata, "validator_attempts": 0})
        artifacts = {**state.artifacts, artifact_id: updated_artifact}
        updated = replace(state, artifacts=artifacts, current_artifact_id=artifact_id if updated_artifact.status == ArtifactStatus.REVIEW_REQUIRED.value else None, current_stage=Stage.ARTIFACT_REVIEW.value if updated_artifact.status == ArtifactStatus.REVIEW_REQUIRED.value else Stage.READY.value, current_group=None, current_task=None, run_id=None, attempt=0, last_outcome={"validator": evidence, "summary": "project validator passed"}, status=WorkflowStatus.RUNNING.value, updated_at=now_iso())
        self.store.save_artifact(updated_artifact.to_dict())
        self.logger.emit("project_validator_passed", artifact_id=artifact_id, validator_role=artifact.validator_role, status=evidence.get("status"))
        if updated_artifact.status == ArtifactStatus.APPROVED.value:
            return self._prepare_artifact_promotion(updated, artifact_id)
        return StepResult(self._save(updated), "artifact_validation_passed")

    def _agent_step(self, state: WorkflowState) -> StepResult:
        stage = state.current_stage
        if state.run_id:
            run_dir = self.store.run_dir(state.run_id)
            outcome_path = run_dir / "outcome.json"
            valid, outcome, errors = validate_outcome(outcome_path, state.run_id, stage)
            metadata = _read_json(run_dir / "metadata.json")
            if valid and outcome:
                if metadata.get("status") == "running":
                    workspace = RunWorkspace.from_metadata(metadata, Path(self.config.project_path))
                    return self._workspace_stop(state, workspace, "RECOVERY_UNCERTAIN: Agent outcome exists while invocation is still marked running", "RECOVERY_UNCERTAIN")
                self.logger.emit("state_recovered", run_id=state.run_id, stage=stage, reason="valid outcome reconciled")
                return self._finalize_agent_outcome(state, outcome, run_dir, metadata)
            if metadata.get("status") == "running" and not outcome_path.exists():
                new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason="RECOVERY_UNCERTAIN: prior Agent invocation has no completed artifact", stop_code="RECOVERY_UNCERTAIN", blocked_stage=stage, recoverable=False, updated_at=now_iso())
                self.logger.emit("hard_stop_entered", reason=new_state.stop_reason)
                return StepResult(self._save(new_state), "hard_stop")
            return self._invalid_or_failed(state, errors or ["invalid outcome"])

        run_id = uuid.uuid4().hex
        run_dir = self.store.run_dir(run_id)
        state = replace(state, run_id=run_id, attempt=state.attempt + 1, total_steps=state.total_steps + 1, updated_at=now_iso())
        if state.current_task in state.work_items:
            item = state.work_items[state.current_task]
            state = replace(state, work_items={**state.work_items, state.current_task: replace(item, status=WorkItemStatus.REQUIRES_PATCH.value if stage == Stage.TASK_PATCH.value else WorkItemStatus.RUNNING.value, attempt=state.attempt)})
        self._save(state)
        outcome_path = run_dir / "outcome.json"
        authority_before = self._agent_authority_snapshot()
        try:
            workspace = RunWorkspace.create(Path(self.config.project_path), self.store.root, run_id)
        except Exception as exc:
            _write_json(run_dir / "metadata.json", {"run_id": run_id, "stage": stage, "status": "failed", "error": str(exc), "started_at": now_iso()})
            return self._workspace_stop(state, None, f"could not create isolated Agent workspace: {exc}", "WORKSPACE_SETUP_FAILED")
        workspace_metadata = {
            "authoritative_origin": str(Path(self.config.project_path).resolve()),
            "workspace_path": str(workspace.path),
            "effective_cwd": str(workspace.path.resolve()),
            "workspace_baseline": workspace.baseline,
            "real_baseline": workspace.real_baseline,
            "excluded_roots": [str(item) for item in workspace.excluded_roots],
        }
        prompt = self.prompt_builder.build(self.config, state, outcome_path, execution_workspace=workspace.path)
        (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        _write_json(run_dir / "metadata.json", {"run_id": run_id, "stage": stage, "status": "running", "started_at": now_iso(), "authority_snapshot": authority_before, **workspace_metadata})
        self.logger.emit("stage_entered", stage=stage, group=state.current_group, task=state.current_task)
        self.logger.emit("agent_run_started", run_id=run_id, stage=stage, attempt=state.attempt, workspace_path=str(workspace.path))
        try:
            result = self.runner.run(workspace.path, prompt, run_dir, self.config.runner.timeout_seconds, env={"CWO_RUN_ID": run_id, "CWO_OUTCOME_PATH": str(outcome_path), "CWO_AUTHORITATIVE_ORIGIN": str(Path(self.config.project_path).resolve())})
        except Exception as exc:
            workspace.discard()
            _write_json(run_dir / "metadata.json", {"run_id": run_id, "stage": stage, "status": "failed", "error": str(exc), "started_at": now_iso(), **workspace_metadata})
            return self._workspace_stop(state, None, f"Agent runner failed before completion: {exc}", "RUNNER_FAILURE")
        changes = workspace.diff()
        workspace.record_diff(run_dir, changes)
        runner_metadata = dict(result.runner_metadata)
        runner_metadata.setdefault("effective_cwd", str(workspace.path.resolve()))
        runner_metadata.setdefault("authoritative_origin", str(Path(self.config.project_path).resolve()))
        _write_json(run_dir / "metadata.json", {"run_id": run_id, "stage": stage, "status": "completed", "started_at": result.started_at, "finished_at": result.finished_at, "exit_code": result.exit_code, "timed_out": result.timed_out, "runner_metadata": runner_metadata, "runner_argv": runner_metadata.get("argv", runner_metadata.get("command", "")), "sandbox_mode": runner_metadata.get("sandbox_mode", "unknown"), "workspace_diff_count": len(changes), **workspace_metadata})
        self.logger.emit("agent_run_finished", run_id=run_id, stage=stage, exit_code=result.exit_code, timed_out=result.timed_out, workspace_diff_count=len(changes))
        drift = self._classify_real_drift(workspace, state, stage)
        _write_json(run_dir / "real-drift.json", drift)
        _write_json(run_dir / "metadata.json", {**_read_json(run_dir / "metadata.json"), "real_drift": drift})
        blocking_drift = next((item for item in drift if item["classification"] in {"AUTHORITY_DRIFT", "ACCEPTED_UPSTREAM_DRIFT", "CURRENT_TARGET_DRIFT"}), None)
        if blocking_drift:
            # Keep the historical stop code for this invocation-time case;
            # the persisted real_drift evidence carries the more precise
            # CURRENT_TARGET_DRIFT classification.
            stop_code = {"AUTHORITY_DRIFT": "UNAUTHORIZED_AUTHORITY_MUTATION", "ACCEPTED_UPSTREAM_DRIFT": "ACCEPTED_UPSTREAM_DRIFT", "CURRENT_TARGET_DRIFT": "REAL_PROJECT_CHANGED_DURING_RUN"}[blocking_drift["classification"]]
            return self._workspace_stop(state, workspace, f"{stop_code}: {blocking_drift['path']}", stop_code)
        if authority_before != self._agent_authority_snapshot():
            return self._workspace_stop(state, workspace, "UNAUTHORIZED_AUTHORITY_MUTATION: authority changed during Agent invocation", "UNAUTHORIZED_AUTHORITY_MUTATION")
        valid, outcome, errors = validate_outcome(outcome_path, run_id, stage)
        if result.exit_code != 0 or result.timed_out:
            workspace.discard()
            errors = ["runner timed out" if result.timed_out else f"runner process failed with exit code {result.exit_code}"]
            return self._invalid_or_failed(state, errors)
        if not valid or not outcome:
            workspace.discard()
            return self._invalid_or_failed(state, errors)
        self.logger.emit("outcome_validated", run_id=run_id, stage=stage, verdict=outcome["verdict"])
        return self._finalize_agent_outcome(state, outcome, run_dir, _read_json(run_dir / "metadata.json"))

    def _workspace_stop(self, state: WorkflowState, workspace: RunWorkspace | None, reason: str, stop_code: str) -> StepResult:
        if workspace:
            workspace.discard()
        stopped = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason=reason, stop_code=stop_code, blocked_stage=state.current_stage, recoverable=False, updated_at=now_iso())
        self.logger.emit("hard_stop_entered", reason=reason, stop_code=stop_code)
        return StepResult(self._save(stopped), "hard_stop")

    def _finalize_agent_outcome(self, state: WorkflowState, outcome: dict[str, Any], run_dir: Path, metadata: dict[str, Any]) -> StepResult:
        stage = state.current_stage
        try:
            workspace = RunWorkspace.from_metadata(metadata, Path(self.config.project_path))
        except WorkspaceError as exc:
            return self._workspace_stop(state, None, str(exc), "RECOVERY_UNCERTAIN")
        changes = workspace.diff() if workspace and workspace.path.exists() else []
        if workspace:
            workspace.record_diff(run_dir, changes)
            drift = self._classify_real_drift(workspace, state, stage)
            _write_json(run_dir / "real-drift.json", drift)
            _write_json(run_dir / "metadata.json", {**metadata, "real_drift": drift})
            blocking_drift = next((item for item in drift if item["classification"] in {"AUTHORITY_DRIFT", "ACCEPTED_UPSTREAM_DRIFT", "CURRENT_TARGET_DRIFT"}), None)
            if blocking_drift:
                stop_code = {"AUTHORITY_DRIFT": "UNAUTHORIZED_AUTHORITY_MUTATION", "ACCEPTED_UPSTREAM_DRIFT": "ACCEPTED_UPSTREAM_DRIFT", "CURRENT_TARGET_DRIFT": "TARGET_DRIFT"}[blocking_drift["classification"]]
                return self._workspace_stop(state, workspace, f"{stop_code}: {blocking_drift['path']}", stop_code)
        if stage in STRICT_WORKSPACE_STAGES and changes:
            return self._workspace_stop(state, workspace, f"workspace mutation is forbidden during {stage}", "WORKSPACE_MUTATION_VIOLATION")
        if stage in PROJECT_MUTATING_STAGES and outcome.get("verdict") == Verdict.APPROVED.value and changes:
            task = self.config.task_at(state.current_group, state.current_task)
            if not task:
                return self._workspace_stop(state, workspace, "project-mutating stage has no bounded task", "UNAUTHORIZED_WORKSPACE_MUTATION")
            allowed = tuple(task.allowed_paths) + tuple(task.expected_outputs)
            protected = self._authority_relative_paths()
            if any(str(change.get("path", "")) in protected for change in changes):
                return self._workspace_stop(state, workspace, "Agent attempted to mutate an authority artifact", "UNAUTHORIZED_AUTHORITY_MUTATION")
            try:
                if workspace is None:
                    raise WorkspaceError("project-mutating outcome has no isolated workspace")
                apply_validated_diff(workspace, changes, allowed)
            except TargetDriftError as exc:
                return self._workspace_stop(state, workspace, str(exc), "TARGET_DRIFT")
            except WorkspaceError as exc:
                return self._workspace_stop(state, workspace, str(exc), "UNAUTHORIZED_WORKSPACE_MUTATION")
        if workspace:
            workspace.discard()
        return self._apply_outcome(state, outcome)

    def _authority_relative_paths(self) -> set[str]:
        project = Path(self.config.project_path).resolve()
        paths: set[str] = set()
        for source in self.config.authoritative_sources:
            path = Path(source.path)
            path = path if path.is_absolute() else project / path
            if path.resolve().is_relative_to(project):
                paths.add(path.resolve().relative_to(project).as_posix())
        ledger = self.store.load_authority_ledger() or {}
        for entry in (ledger.get("sources", {}) or {}).values():
            if not isinstance(entry, dict):
                continue
            for key in ("path", "configured_path", "candidate_path"):
                raw = entry.get(key)
                if not isinstance(raw, str):
                    continue
                path = Path(raw)
                path = path if path.is_absolute() else project / path
                if path.resolve().is_relative_to(project):
                    paths.add(path.resolve().relative_to(project).as_posix())
        return paths

    def _remote_human_guide_paths(self) -> set[str]:
        """Return local Human Guide paths that are only draft counterparts of remote authority."""
        project = Path(self.config.project_path).resolve()
        remote_state = self.store.load_remote_state() or {}
        remote_sources = remote_state.get("sources", {}) if isinstance(remote_state, dict) else {}
        remote_ids: set[str] = set()
        for sid, entry in (remote_sources or {}).items():
            if isinstance(entry, dict) and isinstance(entry.get("snapshot_path"), str):
                remote_ids.add(str(sid))
        state = self.store.load()
        if state:
            for artifact in state.artifacts.values():
                accepted_source = artifact.metadata.get("accepted_source") if isinstance(artifact.metadata, dict) else None
                if artifact.kind == "HUMAN_GUIDE" and isinstance(accepted_source, dict) and accepted_source.get("kind") == "GIT_REMOTE":
                    remote_ids.add(source_id(next((item for item in self.config.authoritative_sources if source_id(item) == "human-guide"), self.config.authoritative_sources[0])))
        paths: set[str] = set()
        for source in self.config.authoritative_sources:
            sid = source_id(source)
            role = (source.role or "").upper()
            if sid not in remote_ids or not (role == "HUMAN_GUIDE" or sid == "human-guide" or "human" in source.path.lower() or "架构原理" in source.path):
                continue
            path = Path(source.path)
            path = path if path.is_absolute() else project / path
            try:
                if path.resolve().is_relative_to(project):
                    paths.add(path.resolve().relative_to(project).as_posix())
            except (OSError, RuntimeError):
                continue
        return paths

    def _accepted_upstream_paths(self, state: WorkflowState) -> set[str]:
        """Return accepted local artifact paths that are pinned inputs, not drafts."""
        project = Path(self.config.project_path).resolve()
        _, paths = self._authority_paths_by_role()
        paths -= self._remote_human_guide_paths()
        for artifact in state.artifacts.values():
            if artifact.status != ArtifactStatus.ACCEPTED.value or not artifact.accepted_path:
                continue
            path = Path(artifact.accepted_path)
            path = path if path.is_absolute() else project / path
            try:
                if path.resolve().is_relative_to(project):
                    paths.add(path.resolve().relative_to(project).as_posix())
            except (OSError, RuntimeError):
                continue
        return paths

    def _authority_paths_by_role(self) -> tuple[set[str], set[str]]:
        """Separate Human Guide authority paths from accepted downstream inputs."""
        project = Path(self.config.project_path).resolve()
        authority: set[str] = set()
        upstream: set[str] = set()

        def add(raw: Any, role: str | None) -> None:
            if not isinstance(raw, str):
                return
            path = Path(raw)
            path = path if path.is_absolute() else project / path
            try:
                if not path.resolve().is_relative_to(project):
                    return
                relative = path.resolve().relative_to(project).as_posix()
            except (OSError, RuntimeError):
                return
            if (role or "").upper() == "HUMAN_GUIDE":
                authority.add(relative)
            else:
                upstream.add(relative)

        for source in self.config.authoritative_sources:
            role = source.role or ("HUMAN_GUIDE" if source_id(source) == "human-guide" else "ACCEPTED_UPSTREAM")
            add(source.path, role)
        ledger = self.store.load_authority_ledger() or {}
        for entry in (ledger.get("sources", {}) or {}).values():
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role") or "ACCEPTED_UPSTREAM")
            for key in ("path", "configured_path", "candidate_path"):
                add(entry.get(key), role)
        return authority, upstream

    def _current_target_paths(self, state: WorkflowState, stage: str) -> tuple[str, ...]:
        if stage not in PROJECT_MUTATING_STAGES:
            return ()
        task = self.config.task_at(state.current_group, state.current_task)
        if not task:
            return ()
        return tuple(task.allowed_paths) + tuple(task.expected_outputs)

    def _classify_real_drift(self, workspace: RunWorkspace, state: WorkflowState, stage: str) -> list[dict[str, Any]]:
        """Classify only changes made to the real project after the fixed snapshot."""
        local_draft = self._remote_human_guide_paths()
        authority, _ = self._authority_paths_by_role()
        upstream = self._accepted_upstream_paths(state)
        targets = self._current_target_paths(state, stage)
        result: list[dict[str, Any]] = []
        for change in workspace.real_tree_diff():
            path = str(change["path"])
            if path in local_draft:
                classification = "LOCAL_DRAFT_DRIFT"
            elif path in authority:
                classification = "AUTHORITY_DRIFT"
            elif path in upstream:
                classification = "ACCEPTED_UPSTREAM_DRIFT"
            elif any(fnmatch.fnmatch(path, pattern) for pattern in targets):
                classification = "CURRENT_TARGET_DRIFT"
            else:
                classification = "UNRELATED_CONCURRENT_DRIFT"
            baseline = change.get("baseline") or {}
            observed = change.get("observed") or {}
            result.append({
                "path": path,
                "classification": classification,
                "status": change.get("status"),
                "baseline_sha256": baseline.get("sha256") if isinstance(baseline, dict) else None,
                "observed_sha256": observed.get("sha256") if isinstance(observed, dict) else None,
            })
        return result

    def _agent_authority_snapshot(self) -> dict[str, str]:
        """Snapshot local authority files except remote Human Guide draft counterparts."""
        snapshot = authority_snapshot(self.config, self.store)
        remote_paths = self._remote_human_guide_paths()
        for source in self.config.authoritative_sources:
            path = Path(source.path)
            path = path if path.is_absolute() else Path(self.config.project_path).resolve() / path
            if ((source.role or "").upper() == "HUMAN_GUIDE" or source_id(source) == "human-guide") and path.resolve().is_relative_to(Path(self.config.project_path).resolve()) and path.resolve().relative_to(Path(self.config.project_path).resolve()).as_posix() in remote_paths:
                snapshot.pop(source_id(source), None)
        return snapshot

    def _apply_outcome(self, state: WorkflowState, outcome: dict[str, Any]) -> StepResult:
        stage = state.current_stage
        if stage == Stage.AUTHORITY_CHANGE_ANALYSIS.value:
            return self._apply_authority_analysis(state, outcome)
        if stage in {Stage.ARTIFACT_GENERATION.value, Stage.ARTIFACT_REVIEW.value, Stage.ARTIFACT_PATCH.value}:
            return self._apply_artifact_outcome(state, outcome)
        # PLAN_REVISION_REVIEW is also an existing ordinary plan-defect stage.
        # Route it to propagation only when an active propagation record owns
        # the current stage; otherwise preserve the v0.3-A transition table.
        active_propagation = (
            state.current_authority_change_id
            and state.current_authority_change_id in state.propagation
            and state.propagation[state.current_authority_change_id].get("next_stage") == stage
        )
        if stage in PROPAGATION_STAGES and (stage != Stage.PLAN_REVISION_REVIEW.value or active_propagation):
            return self._apply_propagation_outcome(state, outcome)
        if stage == Stage.FINAL_VERIFICATION.value and self.config.artifact_pipeline_explicit:
            conformance_errors = validate_final_conformance(state, outcome)
            if conformance_errors:
                return self._invalid_or_failed(state, conformance_errors)
        verdict = Verdict(outcome["verdict"])
        if verdict in SCOPED_DECISION_VERDICTS:
            state, unresolved = self._record_decisions(state, outcome)
            if unresolved:
                blocked = replace(state, current_stage=Stage.READY.value, current_group=None, current_task=None, run_id=None, attempt=0, status=WorkflowStatus.RUNNING.value, last_outcome=outcome)
                blocked = schedule(self.config, blocked)
                self.logger.emit("scoped_human_decision", decision_ids=unresolved, ready_work=[item.id for item in ready_work(self.config, blocked)])
                return StepResult(self._save(blocked), "scoped_human_decision")
            outcome = {**outcome, "verdict": Verdict.APPROVED.value, "summary": "resolved by an exact existing ADR"}
        result = transition_after_outcome(self.config, state, outcome)
        new_state = result.state
        if stage == Stage.TASK_INDEPENDENT_REVIEW.value and verdict == Verdict.APPROVED and state.current_task:
            item = new_state.work_items.get(state.current_task)
            if item:
                new_items = {**new_state.work_items, state.current_task: replace(item, status=WorkItemStatus.COMPLETED.value, last_outcome=outcome, blocking_decision_ids=[], dependency_blocked_by_decision_ids=[])}
                new_state = replace(new_state, work_items=new_items)
            if self.config.mode == "autonomous":
                new_state = schedule(self.config, replace(new_state, current_stage=Stage.READY.value, current_group=None, current_task=None, pending_human_gate=None, status=WorkflowStatus.RUNNING.value))
        new_state = recompute(self.config, new_state)
        self.logger.emit("transition", from_stage=state.current_stage, to_stage=new_state.current_stage, verdict=outcome["verdict"])
        return StepResult(self._save(new_state), result.action, result.retry_delay)

    def _apply_artifact_outcome(self, state: WorkflowState, outcome: dict[str, Any]) -> StepResult:
        stage = state.current_stage
        raw, errors = validate_artifact_outcome(self.config, state, outcome.get("artifact"), stage=stage)
        if errors or raw is None:
            return self._invalid_or_failed(state, errors or ["invalid artifact outcome"])
        artifact_id = str(raw["id"])
        current = state.artifacts.get(artifact_id)
        if current is None:
            return self._invalid_or_failed(state, ["current artifact state is missing"])
        if stage in {Stage.ARTIFACT_GENERATION.value, Stage.ARTIFACT_PATCH.value}:
            candidate_hash = raw.get("candidate_hash")
            candidate_path = raw.get("candidate_path") or current.candidate_path
            if raw.get("candidate_content") is not None:
                candidate_path = str(self.store.save_artifact_candidate(artifact_id, str(raw["candidate_content"])))
            status = ArtifactStatus.REVIEW_REQUIRED.value if current.review_required else ArtifactStatus.APPROVED.value
            metadata = {
                **{key: value for key, value in current.metadata.items() if key not in {"validator", "review", "validator_error"}},
                "last_outcome": outcome.get("summary", ""),
                "dependency_revisions": dependency_revisions(self.config, state, next(item for item in self.config.artifact_pipeline if item.id == artifact_id)),
            }
            updated_artifact = replace(current, status=status if not current.validator_role else ArtifactStatus.CANDIDATE.value, version_hash=candidate_hash, candidate_hash=candidate_hash, candidate_path=candidate_path, change_id=state.current_authority_change_id, metadata=metadata)
        else:
            verdict = Verdict(outcome["verdict"])
            if verdict == Verdict.REQUIRES_PATCH:
                updated_artifact = replace(current, status=ArtifactStatus.REQUIRES_PATCH.value, metadata={**current.metadata, "review": raw.get("review", {})})
            elif verdict == Verdict.APPROVED:
                review = raw.get("review") or {}
                metadata = {**current.metadata, "review": {"verdict": verdict.value, **review}}
                updated_artifact = replace(current, status=ArtifactStatus.APPROVED.value, version_hash=current.candidate_hash, metadata=metadata)
            elif verdict in SCOPED_DECISION_VERDICTS:
                updated_artifact = replace(current, status=ArtifactStatus.BLOCKED.value, metadata={**current.metadata, "review": {"verdict": verdict.value, **(raw.get("review") or {})}})
            else:
                return self._invalid_or_failed(state, [f"unsupported artifact review verdict {verdict.value}"])
        artifacts = {**state.artifacts, artifact_id: updated_artifact}
        self.store.save_artifact(updated_artifact.to_dict())
        if stage in {Stage.ARTIFACT_GENERATION.value, Stage.ARTIFACT_PATCH.value} and requires_project_validation(updated_artifact.validator_role):
            validation_state = replace(state, artifacts=artifacts, current_artifact_id=artifact_id, current_stage=Stage.ARTIFACT_VALIDATION.value, current_group=None, current_task=None, run_id=None, attempt=0, last_outcome=outcome, status=WorkflowStatus.RUNNING.value)
            return StepResult(self._save(validation_state), "artifact_validation")
        updated = replace(state, artifacts=artifacts, current_artifact_id=None, current_stage=Stage.READY.value, current_group=None, current_task=None, run_id=None, attempt=0, last_outcome=outcome, status=WorkflowStatus.RUNNING.value)
        if stage == Stage.ARTIFACT_REVIEW.value and Verdict(outcome["verdict"]) in SCOPED_DECISION_VERDICTS:
            decision_outcome = {
                **outcome,
                "decision_requests": [
                    {
                        **request,
                        "source_artifact_id": request.get("source_artifact_id", artifact_id),
                    }
                    for request in (outcome.get("decision_requests") or [outcome])
                    if isinstance(request, dict)
                ],
            }
            updated, _ = self._record_decisions(updated, decision_outcome)
            return StepResult(self._save(schedule(self.config, updated)), "artifact_outcome")
        if updated_artifact.status == ArtifactStatus.APPROVED.value:
            return self._prepare_artifact_promotion(updated, artifact_id)
        return StepResult(self._save(schedule(self.config, updated)), "artifact_outcome")

    def _prepare_artifact_promotion(self, state: WorkflowState, artifact_id: str) -> StepResult:
        spec = next(item for item in self.config.artifact_pipeline if item.id == artifact_id)
        artifact = state.artifacts[artifact_id]
        if artifact.status == ArtifactStatus.APPROVED.value:
            artifact = replace(artifact, status=ArtifactStatus.PROMOTION_READY.value)
            state = replace(state, artifacts={**state.artifacts, artifact_id: artifact})
            self.store.save_artifact(artifact.to_dict())
        validation_errors = validate_artifact_promotion(self.config, state, artifact_id, allow_external=spec.promotion_policy == "EXTERNAL")
        if validation_errors:
            return self._invalid_or_failed(state, validation_errors)
        if spec.promotion_policy == "AUTO":
            promoted = self._promote_artifact(state, artifact_id)
            return StepResult(self._save(schedule(self.config, promoted)), "artifact_auto_promoted")
        if spec.promotion_policy == "EXTERNAL":
            promoted_artifact = replace(artifact, status=ArtifactStatus.PROMOTION_READY.value, metadata={**artifact.metadata, "external_acceptance_required": True})
            updated = replace(state, artifacts={**state.artifacts, artifact_id: promoted_artifact})
            self.store.save_artifact(promoted_artifact.to_dict())
            self.logger.emit("artifact_promotion_external", artifact_id=artifact_id, kind=spec.kind)
            return StepResult(self._save(schedule(self.config, updated)), "artifact_promotion_external")
        decision_id = artifact.metadata.get("promotion_decision_id") or f"ADR-ARTIFACT-PROMOTION-{artifact_id}"
        promoted_artifact = replace(artifact, status=ArtifactStatus.PROMOTION_READY.value, metadata={**artifact.metadata, "promotion_decision_id": decision_id})
        request = {
            "decision_id": decision_id,
            "category": "ARTIFACT_PROMOTION",
            "question": f"Accept artifact {artifact_id} ({spec.kind}) as the current artifact revision?",
            "context": "The candidate passed semantic review and deterministic validation.",
            "why_human_required": "This artifact explicitly declares promotion_policy=HUMAN_GATE.",
            "options": ["promote"],
            "recommended_option": "promote",
            "allow_freeform": False,
            "source_change": state.current_authority_change_id or "",
            "source_stage": Stage.ARTIFACT_REVIEW.value,
            "source_artifact_id": artifact_id,
            "affected_requirements": artifact.affected_requirements,
            "affected_contract_anchors": artifact.affected_contract_anchors,
            "directly_blocked_items": [],
        }
        updated = replace(state, artifacts={**state.artifacts, artifact_id: promoted_artifact})
        self.store.save_artifact(promoted_artifact.to_dict())
        updated, _ = self._record_decisions(updated, {"decision_requests": [request]})
        self.logger.emit("artifact_promotion_request_created", artifact_id=artifact_id, decision_id=decision_id)
        return StepResult(self._save(schedule(self.config, updated)), "artifact_promotion_request")

    @staticmethod
    def _atomic_write_bytes(destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _artifact_acceptance_path(self, artifact_id: str) -> Path:
        spec = next(item for item in self.config.artifact_pipeline if item.id == artifact_id)
        if spec.accepted_path:
            path = Path(spec.accepted_path)
            return path if path.is_absolute() else Path(self.config.project_path) / path
        return self.store.artifact_accepted_path(artifact_id)

    def _promote_artifact(self, state: WorkflowState, artifact_id: str) -> WorkflowState:
        artifact = state.artifacts[artifact_id]
        spec = next(item for item in self.config.artifact_pipeline if item.id == artifact_id)
        existing = self.store.load_artifact_promotion(artifact_id)
        destination = self._artifact_acceptance_path(artifact_id)
        if artifact.status == ArtifactStatus.ACCEPTED.value and artifact.accepted_hash == artifact.candidate_hash:
            return state
        if isinstance(existing, dict) and existing.get("status") == "COMMITTED" and existing.get("new_accepted_hash") == artifact.candidate_hash:
            if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == artifact.candidate_hash:
                return self._finalize_artifact_acceptance(state, artifact_id, destination, existing)
        if isinstance(existing, dict) and existing.get("status") == "PREPARED":
            target_hash = hashlib.sha256(destination.read_bytes()).hexdigest() if destination.is_file() else None
            if target_hash == existing.get("new_accepted_hash") == artifact.candidate_hash:
                existing["status"] = "COMMITTED"
                existing["after_accepted_hash"] = target_hash
                self.store.save_artifact_promotion(artifact_id, existing)
                return self._finalize_artifact_acceptance(state, artifact_id, destination, existing)
            if target_hash not in {None, existing.get("before_accepted_hash"), existing.get("previous_accepted_hash")}:
                raise OrchestratorError("artifact promotion target drifted during recovery")
        spec_uses_external_store = not spec.accepted_path
        if spec_uses_external_store and destination.exists() and not isinstance(existing, dict):
            raise OrchestratorError("artifact accepted target drifted before first promotion")
        errors = validate_artifact_promotion(self.config, state, artifact_id)
        if errors:
            raise OrchestratorError("; ".join(errors))
        candidate = Path(str(artifact.candidate_path))
        content = candidate.read_bytes()
        before_hash = hashlib.sha256(destination.read_bytes()).hexdigest() if destination.is_file() else None
        if isinstance(existing, dict) and existing.get("status") == "PREPARED":
            if existing.get("candidate_hash") != artifact.candidate_hash or existing.get("before_accepted_hash") != before_hash:
                raise OrchestratorError("artifact promotion precondition changed during recovery")
        review = artifact.metadata.get("review", {})
        validator = artifact.metadata.get("validator", {})
        record = {
            "artifact_id": artifact_id,
            "artifact_kind": spec.kind,
            "status": "PREPARED",
            "previous_accepted_hash": artifact.accepted_hash,
            "before_accepted_hash": before_hash,
            "new_accepted_hash": artifact.candidate_hash,
            "candidate_hash": artifact.candidate_hash,
            "change_id": artifact.change_id,
            "derived_from": dependency_revisions(self.config, state, spec),
            "review_evidence": review,
            "validator_evidence": validator,
            "promotion_policy": spec.promotion_policy,
            "promotion_time": now_iso(),
            "accepted_path": str(destination),
        }
        self.store.save_artifact_promotion(artifact_id, record)
        self._atomic_write_bytes(destination, content)
        record["status"] = "COMMITTED"
        record["after_accepted_hash"] = artifact.candidate_hash
        self.store.save_artifact_promotion(artifact_id, record)
        return self._finalize_artifact_acceptance(state, artifact_id, destination, record)

    def _finalize_artifact_acceptance(self, state: WorkflowState, artifact_id: str, destination: Path, record: dict[str, Any]) -> WorkflowState:
        artifact = state.artifacts[artifact_id]
        accepted = replace(
            artifact,
            status=ArtifactStatus.ACCEPTED.value,
            version_hash=artifact.candidate_hash,
            accepted_hash=artifact.candidate_hash,
            accepted_path=str(destination),
            metadata={
                **artifact.metadata,
                "promotion": record,
                "promotion_history": [*(artifact.metadata.get("promotion_history", []) or []), record],
            },
        )
        self.store.save_artifact(accepted.to_dict())
        self.logger.emit("artifact_promoted", artifact_id=artifact_id, kind=accepted.kind, accepted_hash=accepted.accepted_hash)
        return replace(state, artifacts={**state.artifacts, artifact_id: accepted})

    def _apply_authority_analysis(self, state: WorkflowState, outcome: dict[str, Any]) -> StepResult:
        analysis, errors = validate_analysis(self.config, self.store, state, outcome)
        if errors or analysis is None:
            return self._invalid_or_failed(state, errors or ["invalid authority change analysis"])
        change_id = str(analysis["change_id"])
        change = dict(state.authority_changes.get(change_id, {}))
        direct = list(analysis["directly_affected_tasks"])
        computed = sorted(dependency_tasks(self.config, direct, state))
        direct_artifacts = list(analysis.get("directly_affected_artifacts", analysis.get("affected_artifacts", [])))
        artifact_closure = sorted(artifact_impact_closure(self.config, direct_artifacts) - set(direct_artifacts))
        all_tasks = {str(item.get("id")) for item in (state.plan_graph or {}).get("tasks", []) if isinstance(item, dict) and isinstance(item.get("id"), str)} or {task.id for _, task in self.config.tasks}
        record = {**change, **analysis, "directly_affected_artifacts": direct_artifacts, "dependency_affected_artifacts": artifact_closure, "affected_artifacts": sorted(set(direct_artifacts) | set(artifact_closure)), "dependency_affected_tasks": computed, "unaffected_tasks": sorted(all_tasks - set(direct) - set(computed)), "status": "CHANGE_PENDING" if analysis["human_decision_required"] else "PROPAGATING", "analyzed_at": now_iso()}
        if direct_artifacts:
            record["artifact_impact"] = reconcile_artifact_impact(self.config, state, direct_artifacts)
        self.store.save_authority_change(record)
        ledger = bootstrap_ledger(self.config, self.store)
        entry = ledger.setdefault("sources", {}).setdefault(record["source_id"], {})
        if analysis["classification"] in {"C0", "C1"} and not analysis["semantic_change"] and not direct:
            entry.update({"accepted_sha256": record["candidate_sha256"], "candidate_sha256": record["candidate_sha256"], "path": record["source_path"], "candidate_path": record["source_path"], "status": "ACCEPTED", "change_id": change_id})
            record["status"] = "ACCEPTED"
            self.logger.emit("authority_change_auto_accepted", change_id=change_id, classification=analysis["classification"])
        else:
            entry.update({"candidate_sha256": record["candidate_sha256"], "status": record["status"], "change_id": change_id})
            self.logger.emit("authority_change_analyzed", change_id=change_id, classification=analysis["classification"], human_decision_required=analysis["human_decision_required"], required_propagation=analysis["required_propagation"])
        self.store.save_authority_change(record)
        self.store.save_authority_ledger(ledger)
        if record["status"] != "ACCEPTED":
            propagation = None if analysis["human_decision_required"] else self._new_propagation(record)
        else:
            propagation = None
        updated = replace(state, authority_changes={**state.authority_changes, change_id: record}, current_authority_change_id=change_id if propagation else None, current_stage=Stage.READY.value, current_group=None, current_task=None, run_id=None, attempt=0, status=WorkflowStatus.RUNNING.value, last_outcome=outcome, stop_reason=None, stop_code=None, blocked_stage=None, recoverable=False)
        if direct_artifacts:
            from .models import ArtifactStatus
            blocked_artifacts = dict(updated.artifacts)
            for artifact_id in set(direct_artifacts) | set(artifact_closure):
                artifact = blocked_artifacts.get(artifact_id)
                if artifact:
                    blocked_artifacts[artifact_id] = replace(artifact, status=ArtifactStatus.BLOCKED.value, change_id=change_id)
            updated = replace(updated, artifacts=blocked_artifacts)
        if propagation:
            updated = replace(updated, propagation={**updated.propagation, change_id: propagation})
        if analysis["human_decision_required"]:
            requests = outcome.get("decision_requests") or analysis.get("human_decision_requests", [])
            normalized_requests = [{**request, "source_change": change_id, "source_stage": Stage.AUTHORITY_CHANGE_ANALYSIS.value, "directly_blocked_items": request.get("directly_blocked_items", direct)} for request in requests if isinstance(request, dict)]
            updated, _ = self._record_decisions(updated, {**outcome, "decision_requests": normalized_requests, "directly_affected_work": direct, "blocking_scope": {"directly_blocked_items": direct}})
        updated = schedule(self.config, recompute(self.config, updated))
        return StepResult(self._save(updated), "authority_change_analyzed")

    def _new_propagation(self, change: dict[str, Any]) -> dict[str, Any]:
        steps = propagation_steps(change)
        value = {
            "schema_version": "1.0", "change_id": change["change_id"], "status": "RUNNING", "stages": steps,
            "stage_index": 0, "next_stage": steps[0] if steps else None, "candidate_artifacts": [],
            "reviews": {}, "created_at": now_iso(),
        }
        self.store.save_propagation_json(str(change["change_id"]), "propagation-plan.json", value)
        return value

    def _apply_propagation_outcome(self, state: WorkflowState, outcome: dict[str, Any]) -> StepResult:
        change_id = state.current_authority_change_id
        propagation = dict(state.propagation.get(change_id or "", {}))
        if not change_id or not propagation or propagation.get("status") != "RUNNING":
            return self._invalid_or_failed(state, ["active propagation record is missing"])
        stage = state.current_stage
        if Verdict(outcome["verdict"]) in SCOPED_DECISION_VERDICTS:
            requests = {**outcome, "directly_affected_work": state.authority_changes.get(change_id, {}).get("directly_affected_tasks", [])}
            propagation.update({"status": "WAITING_DECISION", "next_stage": stage})
            updated, unresolved = self._record_decisions(state, requests)
            updated = replace(updated, propagation={**updated.propagation, change_id: propagation}, current_stage=Stage.READY.value, current_group=None, current_task=None, run_id=None, status=WorkflowStatus.RUNNING.value, last_outcome=outcome)
            self.store.save_propagation_json(change_id, "propagation-plan.json", propagation)
            updated = schedule(self.config, updated)
            return StepResult(self._save(updated), "scoped_human_decision")
        if Verdict(outcome["verdict"]) == Verdict.REQUIRES_PATCH:
            if stage == Stage.CONTRACT_REVISION_REVIEW.value:
                target = Stage.CONTRACT_REVISION.value
                review = outcome.get("contract_review", outcome.get("review", {"verdict": outcome["verdict"], "summary": outcome.get("summary", "")}))
                if isinstance(review, dict):
                    propagation.setdefault("review_history", []).append({"kind": "contract", **review, "verdict": outcome["verdict"]})
            elif stage == Stage.PLAN_REVISION_REVIEW.value:
                target = Stage.PLAN_REVISION.value
                review = outcome.get("plan_review", outcome.get("review", {"verdict": outcome["verdict"], "summary": outcome.get("summary", "")}))
                if isinstance(review, dict):
                    propagation.setdefault("review_history", []).append({"kind": "plan", **review, "verdict": outcome["verdict"]})
            else:
                return self._invalid_or_failed(state, ["REQUIRES_PATCH is not supported for this propagation stage"])
            propagation.update({"next_stage": target, "stage_index": propagation["stages"].index(target)})
            updated = replace(state, propagation={**state.propagation, change_id: propagation}, current_stage=Stage.READY.value, current_group=None, current_task=None, run_id=None, attempt=0, status=WorkflowStatus.RUNNING.value, last_outcome=outcome)
            self.store.save_propagation_json(change_id, "propagation-plan.json", propagation)
            return StepResult(self._save(schedule(self.config, updated)), "propagation_patch")
        if Verdict(outcome["verdict"]) != Verdict.APPROVED:
            return self._invalid_or_failed(state, [f"unsupported propagation verdict {outcome['verdict']}"])

        change = dict(state.authority_changes[change_id])
        if stage == Stage.CHANGE_PROPAGATION_PLANNING.value:
            plan, errors = validate_propagation_plan(outcome.get("propagation_plan"), propagation["stages"])
            if errors:
                return self._invalid_or_failed(state, errors)
            propagation["plan"] = plan
        elif stage == Stage.CONTRACT_REVISION.value:
            required = source_path_for_role(self.config, "ENGINEERING_CONTRACT")
            artifacts, errors = validate_candidate_artifacts(self.config, outcome.get("candidate_artifacts"), required_path=required)
            if errors:
                return self._invalid_or_failed(state, errors)
            stored = self._persist_candidate_artifacts(change_id, artifacts or [])
            propagation["candidate_artifacts"] = self._merge_artifacts(propagation.get("candidate_artifacts", []), stored)
            propagation["contract_revision_report"] = outcome.get("contract_revision_report", {})
        elif stage == Stage.CONTRACT_REVISION_REVIEW.value:
            review = outcome.get("contract_review", outcome.get("review", {"verdict": outcome["verdict"], "summary": outcome.get("summary", "")}))
            if not isinstance(review, dict):
                return self._invalid_or_failed(state, ["contract_review must be an object"])
            propagation.setdefault("reviews", {})["contract"] = {**review, "verdict": outcome["verdict"]}
        elif stage == Stage.PLAN_REVISION.value:
            required = source_path_for_role(self.config, "IMPLEMENTATION_PLAN")
            artifacts, errors = validate_candidate_artifacts(self.config, outcome.get("candidate_artifacts"), required_path=required)
            if errors:
                return self._invalid_or_failed(state, errors)
            stored = self._persist_candidate_artifacts(change_id, artifacts or [])
            propagation["candidate_artifacts"] = self._merge_artifacts(propagation.get("candidate_artifacts", []), stored)
            propagation["plan_revision_report"] = outcome.get("plan_revision_report", {})
        elif stage == Stage.PLAN_REVISION_REVIEW.value:
            review = outcome.get("plan_review", outcome.get("review", {"verdict": outcome["verdict"], "summary": outcome.get("summary", "")}))
            if not isinstance(review, dict):
                return self._invalid_or_failed(state, ["plan_review must be an object"])
            propagation.setdefault("reviews", {})["plan"] = {**review, "verdict": outcome["verdict"]}
        elif stage == Stage.PLAN_GRAPH_BUILD.value:
            graph, errors = validate_plan_graph(self.config, outcome.get("plan_graph"), contract_text=contract_text(self.config, state))
            if errors:
                return self._invalid_or_failed(state, errors)
            expected_plan_sha = self._candidate_artifact_sha(propagation, source_path_for_role(self.config, "IMPLEMENTATION_PLAN")) or next((source.sha256 for source in self.config.authoritative_sources if (source.role or "").upper() == "IMPLEMENTATION_PLAN" or "plan" in source.path.lower() or "计划" in source.path), None)
            if expected_plan_sha and graph["plan_sha256"].lower() != expected_plan_sha.lower():
                return self._invalid_or_failed(state, ["plan_graph.plan_sha256 does not match the candidate Plan"])
            graph_affected = set(change.get("directly_affected_tasks", ())) | set(dependency_closure(self.config, change.get("directly_affected_tasks", ()), replace(state, plan_graph=graph)))
            reconciliation = reconcile_plan_graph(self.config, state, graph, graph_affected)
            propagation["plan_graph"] = graph
            propagation["graph_reconciliation"] = reconciliation
            change["propagation_ready"] = True
            state = replace(state, plan_graph=graph, plan_graph_reconciliation=reconciliation)
        elif stage == Stage.TASK_REBASE_ANALYSIS.value:
            affected = set(change.get("directly_affected_tasks", ()))
            rebase, errors = validate_rebase(outcome.get("task_rebase"), affected)
            if errors:
                return self._invalid_or_failed(state, errors)
            propagation["task_rebase"] = rebase
            propagation["rebase_ready"] = True
        else:
            return self._invalid_or_failed(state, [f"unsupported propagation stage {stage}"])

        self.store.save_authority_change(change)

        index = propagation["stages"].index(stage) + 1
        if index < len(propagation["stages"]):
            propagation.update({"stage_index": index, "next_stage": propagation["stages"][index]})
            self.store.save_propagation_json(change_id, "propagation-plan.json", propagation)
            updated = replace(state, authority_changes={**state.authority_changes, change_id: change}, propagation={**state.propagation, change_id: propagation}, current_stage=Stage.READY.value, current_group=None, current_task=None, run_id=None, attempt=0, status=WorkflowStatus.RUNNING.value, last_outcome=outcome)
            return StepResult(self._save(schedule(self.config, updated)), "propagation_stage_complete")

        request = {
            "decision_id": f"ADR-AUTHORITY-PROMOTION-{change_id}", "category": "AUTHORITY_PROMOTION",
            "question": f"Promote authority change {change_id} to the accepted Architecture/Contract/Plan baseline?",
            "context": f"Candidate Human Guide, Contract, Plan and Plan Graph are prepared. Change summary: {change.get('analysis_summary', '')}",
            "why_human_required": "The authoritative rule requires Human Review and immutable Revision 2 freeze before promotion.",
            "options": ["promote", "defer"], "recommended_option": "promote", "allow_freeform": False,
            "source_change": change_id, "source_stage": Stage.TASK_REBASE_ANALYSIS.value,
            "affected_requirements": change.get("affected_requirements", []), "affected_contract_anchors": change.get("affected_contract_anchors", []),
            "affected_tasks": change.get("directly_affected_tasks", []), "affected_work_items": change.get("directly_affected_tasks", []),
            "directly_blocked_items": change.get("directly_affected_tasks", []),
        }
        propagation.update({"status": "WAITING_PROMOTION", "next_stage": None, "promotion_decision_id": request["decision_id"], "machine_complete": True})
        change["propagation_ready"] = True
        self.store.save_authority_change(change)
        self.store.save_propagation_json(change_id, "propagation-plan.json", propagation)
        updated, _ = self._record_decisions(replace(state, authority_changes={**state.authority_changes, change_id: change}, propagation={**state.propagation, change_id: propagation}, current_stage=Stage.READY.value, current_group=None, current_task=None, run_id=None, status=WorkflowStatus.RUNNING.value, last_outcome=outcome), {"decision_requests": [request], "directly_affected_work": change.get("directly_affected_tasks", [])})
        updated = replace(updated, authority_changes={**updated.authority_changes, change_id: change}, propagation={**updated.propagation, change_id: propagation})
        updated = schedule(self.config, updated)
        self.logger.emit("authority_promotion_request_created", change_id=change_id, decision_id=request["decision_id"])
        return StepResult(self._save(updated), "authority_promotion_request")

    def _persist_candidate_artifacts(self, change_id: str, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stored: list[dict[str, Any]] = []
        for artifact in artifacts:
            path = self.store.save_candidate_artifact(change_id, artifact["path"], artifact["content"])
            stored.append({"path": artifact["path"], "sha256": artifact["sha256"], "stored_path": str(path), "content": artifact["content"]})
        return stored

    @staticmethod
    def _merge_artifacts(existing: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = {str(item.get("path")): item for item in existing if isinstance(item, dict) and item.get("path")}
        result.update({str(item["path"]): item for item in additions})
        return list(result.values())

    @staticmethod
    def _candidate_artifact_sha(propagation: dict[str, Any], path: str | None) -> str | None:
        if not path:
            return None
        for item in propagation.get("candidate_artifacts", []):
            if item.get("path") == path:
                return item.get("sha256")
        return None

    def _invalid_or_failed(self, state: WorkflowState, errors: list[str]) -> StepResult:
        self.logger.emit("outcome_invalid", run_id=state.run_id, stage=state.current_stage, errors=errors)
        runner_failure = any(error.startswith("runner process failed") or error == "runner timed out" for error in errors)
        if state.attempt >= self.config.policy.max_attempts_per_stage:
            new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason="; ".join(errors), stop_code="RETRY_EXHAUSTED", blocked_stage=state.current_stage, recoverable=runner_failure, updated_at=now_iso())
            self.logger.emit("hard_stop_entered", reason=new_state.stop_reason)
            return StepResult(self._save(new_state), "hard_stop")
        delay = min(self.config.policy.retry_max_delay_seconds, self.config.policy.retry_backoff_seconds * (2 ** max(0, state.attempt - 1)))
        invalid = make_outcome(state.run_id or "", state.current_stage, self.config.project_name, Verdict.INVALID_OUTCOME.value, summary="; ".join(errors), next_action="retry")
        new_state = replace(state, run_id=None, last_outcome=invalid, status=WorkflowStatus.RUNNING.value, updated_at=now_iso())
        self.logger.emit("retry_scheduled", stage=state.current_stage, attempt=state.attempt, delay_seconds=delay)
        return StepResult(self._save(new_state), "retry", delay)

    def run(self, dry_run: bool = False) -> WorkflowState | dict[str, Any]:
        if dry_run:
            return self.dry_run()
        for _ in range(self.config.policy.max_total_steps + 1):
            result = self.step()
            if result.retry_delay:
                import time
                time.sleep(result.retry_delay)
            if result.state.status != WorkflowStatus.RUNNING.value:
                return result.state
            if result.state.current_stage in HUMAN_GATES:
                return result.state
        return self._load_or_initialize()

    def approve(self, gate: str | None = None) -> WorkflowState:
        state = self._load_or_initialize()
        if state.current_stage == Stage.HARD_STOP.value:
            raise OrchestratorError("hard stops cannot be approved")
        result = approve(self.config, state, gate)
        self.logger.emit("transition", from_stage=state.current_stage, to_stage=result.state.current_stage, action="human_approved")
        next_state = result.state
        if state.current_stage == Stage.HUMAN_GROUP_APPROVAL.value:
            next_state = schedule(self.config, replace(next_state, current_stage=Stage.READY.value, current_group=None, current_task=None, pending_human_gate=None, status=WorkflowStatus.RUNNING.value))
        else:
            next_state = recompute(self.config, next_state)
        return self._save(next_state)

    def stop(self, reason: str = "stopped by operator") -> WorkflowState:
        state = self._load_or_initialize()
        return self._save(stop(state, reason))

    def _record_decisions(self, state: WorkflowState, outcome: dict[str, Any]) -> tuple[WorkflowState, list[str]]:
        raw_requests = outcome.get("decision_requests")
        if isinstance(raw_requests, dict):
            raw_requests = [raw_requests]
        if not isinstance(raw_requests, list) or not raw_requests:
            raw_requests = [outcome]
        known_tasks = {str(item.get("id")) for item in (state.plan_graph or {}).get("tasks", []) if isinstance(item, dict) and isinstance(item.get("id"), str)} or {task.id for _, task in self.config.tasks}
        decisions = dict(state.decisions)
        unresolved: list[str] = []
        for raw in raw_requests:
            if not isinstance(raw, dict):
                continue
            decision_id = raw.get("decision_id") or outcome.get("decision_id") or f"ADR-PENDING-{uuid.uuid4().hex[:12]}"
            if not isinstance(decision_id, str) or not decision_id:
                continue
            scope = raw.get("blocking_scope") or outcome.get("blocking_scope") or {}
            if not isinstance(scope, dict):
                scope = {}
            direct = raw.get("directly_blocked_items") or raw.get("affected_work_items") or raw.get("affected_tasks") or scope.get("directly_blocked_items")
            direct = direct or outcome.get("directly_affected_work") or [state.current_task]
            direct = tuple(item for item in direct if isinstance(item, str) and item in known_tasks)
            if not direct:
                direct = tuple(task.id for _, task in self.config.tasks if state.work_items.get(task.id, None) and state.work_items[task.id].status != WorkItemStatus.COMPLETED.value)
            payload = {
                "category": raw.get("category", outcome.get("category", "ARCHITECTURE")),
                "question": raw.get("question", outcome.get("question", outcome.get("summary", "Human authority decision required"))),
                "context": raw.get("context", outcome.get("context", "")),
                "why_human_required": raw.get("why_human_required", outcome.get("why_human_required", "The authority chain has no unique machine-resolvable answer.")),
                "options": tuple(item for item in (raw.get("options", outcome.get("options", ())) or ()) if isinstance(item, str)),
                "recommended_option": raw.get("recommended_option", outcome.get("recommended_option")),
                "allow_freeform": bool(raw.get("allow_freeform", outcome.get("allow_freeform", True))),
                "source_change": raw.get("source_change", outcome.get("source_change", "")),
                "source_stage": raw.get("source_stage", outcome.get("source_stage", state.current_stage)),
                "source_artifact_id": raw.get("source_artifact_id", outcome.get("source_artifact_id", "")),
                "affected_requirements": tuple(raw.get("affected_requirements", outcome.get("affected_requirements", ())) or ()),
                "affected_contract_anchors": tuple(raw.get("affected_contract_anchors", outcome.get("affected_contract_anchors", ())) or ()),
                "affected_tasks": tuple(raw.get("affected_tasks", outcome.get("affected_tasks", direct)) or ()),
                "affected_work_items": tuple(raw.get("affected_work_items", direct) or direct),
                "directly_blocked_items": direct,
            }
            candidate = HumanDecision(decision_id=decision_id, **payload)
            match = self._matching_resolved_adr(candidate, decisions)
            if match:
                self.logger.emit("architecture_decision_reused", decision_id=decision_id, adr_id=match.get("adr_id"))
                continue
            existing = decisions.get(decision_id)
            if existing and existing.status == DecisionStatus.PENDING.value:
                unresolved.append(decision_id)
                continue
            decisions[decision_id] = candidate
            self.store.save_decision(candidate)
            unresolved.append(decision_id)
            self.logger.emit("decision_request_created", decision_id=decision_id, directly_blocked_items=list(direct))
        return replace(state, decisions=decisions), unresolved

    @staticmethod
    def _matching_resolved_adr(candidate: HumanDecision, decisions: dict[str, HumanDecision]) -> dict[str, Any] | None:
        signature = (
            candidate.category, candidate.question, candidate.source_change, candidate.source_stage,
            candidate.source_artifact_id, candidate.affected_requirements, candidate.affected_contract_anchors,
            candidate.affected_tasks, candidate.directly_blocked_items,
        )
        for decision in decisions.values():
            if decision.status != DecisionStatus.RESOLVED.value:
                continue
            other = (
                decision.category, decision.question, decision.source_change, decision.source_stage,
                decision.source_artifact_id, decision.affected_requirements, decision.affected_contract_anchors,
                decision.affected_tasks, decision.directly_blocked_items,
            )
            if signature == other:
                return {"adr_id": decision.adr_id or f"ADR-{decision.decision_id}"}
        return None

    def list_decisions(self, pending_only: bool = True) -> list[HumanDecision]:
        state = self._load_or_initialize()
        values = list(state.decisions.values())
        return [item for item in values if item.status == DecisionStatus.PENDING.value] if pending_only else values

    def show_decision(self, decision_id: str) -> HumanDecision:
        state = self._load_or_initialize()
        try:
            return state.decisions[decision_id]
        except KeyError as exc:
            raise OrchestratorError(f"decision not found: {decision_id}") from exc

    def decide(self, decision_id: str, *, option: str | None = None, answer: str | None = None, rationale: str | None = None) -> WorkflowState:
        state = self._load_or_initialize()
        decision = self.show_decision(decision_id)
        if decision.status != DecisionStatus.PENDING.value:
            raise OrchestratorError(f"decision is not pending: {decision_id}")
        if option is None and answer is None:
            raise OrchestratorError("provide --option or --answer")
        if option is not None and decision.options and option not in decision.options:
            raise OrchestratorError(f"option is not declared by decision: {option}")
        if option is None and not decision.allow_freeform:
            raise OrchestratorError("this decision requires a declared --option")
        value = option if option is not None else answer
        resolved_at = now_iso()
        resolved = replace(decision, status=DecisionStatus.RESOLVED.value, resolved_at=resolved_at, decision=value, decision_rationale=rationale, adr_id=f"ADR-{decision_id}")
        decisions = {**state.decisions, decision_id: resolved}
        adr = {
            "adr_id": resolved.adr_id,
            "decision_id": decision_id,
            "question": resolved.question,
            "decision": value,
            "rationale": rationale or "",
            "scope": {
                "affected_requirements": list(resolved.affected_requirements),
                "affected_contract_anchors": list(resolved.affected_contract_anchors),
                "affected_tasks": list(resolved.affected_tasks),
                "directly_blocked_items": list(resolved.directly_blocked_items),
            },
            "created_at": resolved_at,
        }
        next_state = replace(state, decisions=decisions, adrs={**state.adrs, str(resolved.adr_id): adr}, current_stage=Stage.READY.value, current_group=None, current_task=None, run_id=None, attempt=0, status=WorkflowStatus.RUNNING.value)
        if resolved.source_artifact_id and resolved.source_artifact_id in next_state.artifacts:
            artifact = next_state.artifacts[resolved.source_artifact_id]
            if artifact.status == "BLOCKED":
                resumed_status = "REVIEW_REQUIRED" if artifact.candidate_hash else "PENDING"
                next_state = replace(
                    next_state,
                    artifacts={
                        **next_state.artifacts,
                        resolved.source_artifact_id: replace(artifact, status=resumed_status),
                    },
                )
        promoted = resolved.category == "AUTHORITY_PROMOTION" and value == "promote"
        artifact_promoted = resolved.category == "ARTIFACT_PROMOTION" and value == "promote"
        if promoted:
            next_state = self._promote_authority_change(next_state, resolved)
        elif artifact_promoted:
            if not resolved.source_artifact_id:
                raise OrchestratorError("artifact promotion decision has no source artifact")
            next_state = self._promote_artifact(next_state, resolved.source_artifact_id)
        elif resolved.source_stage == Stage.AUTHORITY_CHANGE_ANALYSIS.value:
            change = next((item for item in next_state.authority_changes.values() if item.get("change_id") == resolved.source_change or item.get("change_id") == resolved.source_change), None)
            if change and resolved.source_change not in next_state.propagation:
                propagation = self._new_propagation(change)
                next_state = replace(next_state, propagation={**next_state.propagation, resolved.source_change: propagation}, current_authority_change_id=resolved.source_change)
            elif change and resolved.source_change in next_state.propagation:
                propagation = dict(next_state.propagation[resolved.source_change])
                propagation.update({"status": "RUNNING", "next_stage": propagation.get("next_stage") or propagation.get("stages", [None])[0]})
                next_state = replace(next_state, propagation={**next_state.propagation, resolved.source_change: propagation}, current_authority_change_id=resolved.source_change)
        # Promotion performs external project writes only after all checks pass.
        # Persist the decision/ADR after that preflight so a rejected promotion
        # does not leave a false RESOLVED record behind.
        self.store.save_decision(resolved)
        self.store.save_adr(adr)
        # Promotion deliberately hands the first affected implementation back
        # at TASK_PATCH.  Generic scheduling would classify that active item as
        # non-ready and turn the valid rebase handoff into DEPENDENCY_BLOCKED.
        if not (promoted and next_state.current_stage in AGENT_STAGE_NAMES and next_state.current_task):
            next_state = schedule(self.config, next_state)
        self.logger.emit("decision_resolved", decision_id=decision_id, adr_id=resolved.adr_id, next_stage=next_state.current_stage)
        return self._save(next_state)

    def _promote_authority_change(self, state: WorkflowState, decision: HumanDecision) -> WorkflowState:
        change_id = decision.source_change
        change = state.authority_changes.get(change_id)
        propagation = state.propagation.get(change_id)
        if not change or not propagation or propagation.get("status") != "WAITING_PROMOTION":
            raise OrchestratorError("authority promotion record is incomplete")
        current_scan = scan_authority_changes(self.config, self.store, state)
        current = next((item for item in current_scan.changes if item.get("change_id") == change_id), None)
        if not current or current.get("candidate_sha256") != change.get("candidate_sha256"):
            raise OrchestratorError("authority candidate changed since propagation analysis")
        if any(review.get("verdict") != Verdict.APPROVED.value for review in propagation.get("reviews", {}).values() if isinstance(review, dict)):
            raise OrchestratorError("all propagation reviews must be approved before promotion")
        project = Path(self.config.project_path)
        before_after: list[dict[str, Any]] = []
        for artifact in propagation.get("candidate_artifacts", []):
            destination = safe_project_path(project, str(artifact.get("path", "")))
            stored = Path(str(artifact.get("stored_path", "")))
            if destination is None or not stored.is_file():
                raise OrchestratorError("candidate artifact is missing or unsafe")
            content = stored.read_text(encoding="utf-8")
            if hashlib.sha256(content.encode()).hexdigest() != artifact.get("sha256"):
                raise OrchestratorError("candidate artifact hash mismatch")
            old_digest = hashlib.sha256(destination.read_bytes()).hexdigest() if destination.is_file() else None
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            before_after.append({"path": artifact["path"], "before_sha256": old_digest, "after_sha256": artifact["sha256"]})
        candidate_source = Path(str(change.get("candidate_snapshot_path", ""))) if change.get("authority_origin") == "git-remote" else safe_project_path(project, str(change.get("source_path", "")))
        configured_source = safe_project_path(project, str(change.get("configured_source_path", "")))
        if candidate_source is None or configured_source is None or not candidate_source.is_file():
            raise OrchestratorError("Human Guide candidate is missing or unsafe")
        if hashlib.sha256(candidate_source.read_bytes()).hexdigest() != change.get("candidate_sha256"):
            raise OrchestratorError("Human Guide candidate hash mismatch")
        if candidate_source.resolve() != configured_source.resolve():
            old_digest = hashlib.sha256(configured_source.read_bytes()).hexdigest() if configured_source.is_file() else None
            configured_source.parent.mkdir(parents=True, exist_ok=True)
            configured_source.write_bytes(candidate_source.read_bytes())
            before_after.append({"path": str(change.get("configured_source_path")), "before_sha256": old_digest, "after_sha256": change.get("candidate_sha256")})
        ledger = bootstrap_ledger(self.config, self.store)
        # Downstream candidate authorities promoted with this change become
        # accepted revisions together with the Human Guide.  Otherwise the
        # next bounded run would rediscover the just-promoted Contract/Plan as
        # unrelated frozen mismatches.
        artifact_by_path = {str(item.get("path")): item for item in propagation.get("candidate_artifacts", []) if isinstance(item, dict)}
        for source in self.config.authoritative_sources:
            configured_path = str(source.path)
            artifact = artifact_by_path.get(configured_path)
            if not artifact:
                continue
            entry = ledger.setdefault("sources", {}).setdefault(source_id(source), {})
            entry.update({
                "accepted_sha256": artifact.get("sha256"),
                "candidate_sha256": artifact.get("sha256"),
                "status": "ACCEPTED",
                "change_id": change_id,
                "path": configured_path,
            })
        entry = ledger.setdefault("sources", {}).setdefault(str(change.get("source_id")), {})
        entry.update({"accepted_sha256": change.get("candidate_sha256"), "candidate_sha256": change.get("candidate_sha256"), "status": "ACCEPTED", "change_id": change_id, "path": change.get("configured_source_path")})
        if change.get("authority_origin") == "git-remote":
            entry.update({"accepted_remote_commit": change.get("candidate_commit"), "accepted_remote_blob": change.get("candidate_blob_sha"), "accepted_authority_blob": change.get("candidate_blob_sha"), "accepted_content_sha256": change.get("candidate_sha256"), "accepted_authority_content_sha256": change.get("candidate_sha256"), "candidate_remote_commit": change.get("candidate_commit"), "candidate_remote_blob": change.get("candidate_blob_sha"), "candidate_authority_blob": change.get("candidate_blob_sha"), "candidate_content_sha256": change.get("candidate_sha256")})
        self.store.save_authority_ledger(ledger)
        change = {**change, "status": "PROMOTED", "promoted_at": now_iso(), "promotion": {"decision_id": decision.decision_id, "before_after": before_after}}
        propagation = {**propagation, "status": "PROMOTED", "promoted_at": now_iso(), "promotion": {"decision_id": decision.decision_id, "before_after": before_after}}
        self.store.save_authority_change(change)
        self.store.save_propagation_json(change_id, "promotion.json", propagation["promotion"])
        direct = list(change.get("directly_affected_tasks", ()))
        graph = state.plan_graph
        current_task = direct[0] if direct and graph else None
        group = None
        if current_task and graph:
            group = next((item.get("group") for item in graph.get("tasks", []) if item.get("id") == current_task), None)
        return replace(state, authority_changes={**state.authority_changes, change_id: change}, propagation={**state.propagation, change_id: propagation}, current_authority_change_id=None, current_stage=Stage.TASK_PATCH.value if current_task else Stage.READY.value, current_group=group, current_task=current_task, run_id=None, attempt=0, status=WorkflowStatus.RUNNING.value)

    def status_report(self) -> dict[str, Any]:
        state = recompute(self.config, self._load_or_initialize())
        counts = {status.value: 0 for status in WorkItemStatus}
        for item in state.work_items.values():
            counts[item.status] = counts.get(item.status, 0) + 1
        pending = pending_decisions(state)
        return {
            "workflow": state.status,
            "stage": state.current_stage,
            "work": counts,
            "pending_decisions": [decision.to_dict() for decision in pending],
            "authority_changes": list(state.authority_changes.values()),
            "propagation": state.propagation,
            "plan_graph": state.plan_graph,
            "plan_graph_reconciliation": state.plan_graph_reconciliation,
            "artifact_pipeline": [spec.__dict__ for spec in effective_artifact_specs(self.config)],
            "artifact_pipeline_mode": "explicit" if self.config.artifact_pipeline_explicit else "legacy-adapter",
            "missing_skill_roles": missing_skill_roles(self.config),
            "unaffected_work_continues": state.status == WorkflowStatus.RUNNING.value and bool(ready_work(self.config, state)),
            "state": state.to_dict(),
        }

    def recover(self) -> WorkflowState:
        state = self._load_or_initialize()
        self.logger.emit("recovery_requested", stop_code=state.stop_code, blocked_stage=state.blocked_stage)
        legacy_recovery = (
            state.current_stage == Stage.HARD_STOP.value
            and state.stop_code == "UNEXPECTED_UNRELATED_CHANGE"
            and state.run_id is not None
        )
        schema_recovery = state.stop_code == "RETRY_EXHAUSTED"
        workflow_digest_recovery = (
            state.stop_code == "WORKFLOW_DIGEST_CHANGED"
            and state.current_stage == Stage.HARD_STOP.value
            and state.run_id is None
        )
        runner_recovery = (
            state.stop_code == "RETRY_EXHAUSTED"
            and isinstance(state.last_outcome, dict)
            and str(state.last_outcome.get("summary", "")).startswith(("runner process failed", "runner timed out"))
        )
        interrupted_recovery = (
            state.stop_code == "RECOVERY_UNCERTAIN"
            and state.current_stage == Stage.HARD_STOP.value
            and state.blocked_stage in AGENT_STAGE_NAMES
            and state.run_id is not None
        )
        schema_recovery = state.stop_code == "RETRY_EXHAUSTED" and not runner_recovery
        if state.current_stage != Stage.HARD_STOP.value or not (legacy_recovery or runner_recovery or interrupted_recovery or schema_recovery or workflow_digest_recovery):
            self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason="stop is not recoverable")
            raise OrchestratorError("hard stop is not recoverable")

        if workflow_digest_recovery:
            audit, integrity, _ = self._audit_gate()
            if integrity or audit.blocking or not audit.is_repository or audit.error:
                reason = "; ".join(integrity) or "; ".join(item.classification.value for item in audit.changes if item.classification.value in {"FROZEN_AUTHORITY_CHANGE", "MERGE_CONFLICT", "UNEXPECTED_UNRELATED_CHANGE"})
                reason = reason or audit.error or "Git audit blocked"
                self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason=reason)
                raise OrchestratorError(reason)
            if any(_read_json(path).get("status") == "running" for path in self.store.runs_path.glob("*/metadata.json")):
                self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason="RECOVERY_UNCERTAIN: an Agent invocation is still running")
                raise OrchestratorError("RECOVERY_UNCERTAIN: an Agent invocation is still running")
            restored_stage = state.blocked_stage or Stage.AUTHORITY_CHANGE_ANALYSIS.value
            self.logger.emit("workflow_digest_recovery_validated", old_digest=state.workflow_digest, new_digest=self.config.digest, restored_stage=restored_stage)
            recovered = replace(
                state,
                workflow_digest=self.config.digest,
                current_stage=restored_stage,
                status=WorkflowStatus.RUNNING.value,
                pending_human_gate=None,
                run_id=None,
                attempt=0,
                stop_reason=None,
                stop_code=None,
                blocked_stage=None,
                recoverable=False,
                updated_at=now_iso(),
            )
            self.logger.emit("hard_stop_recovered", stop_code="WORKFLOW_DIGEST_CHANGED", restored_stage=restored_stage)
            return self._save(recovered)

        if runner_recovery:
            records = [_read_json(path) for path in self.store.runs_path.glob("*/metadata.json")]
            if any(record.get("status") == "running" for record in records):
                self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason="RECOVERY_UNCERTAIN: an Agent invocation is still running")
                raise OrchestratorError("RECOVERY_UNCERTAIN: an Agent invocation is still running")
            latest = max(records, key=lambda record: (str(record.get("finished_at") or record.get("started_at") or ""), str(record.get("run_id") or "")), default={})
            if latest.get("stage") != state.blocked_stage or latest.get("status") != "completed" or (latest.get("exit_code", 0) == 0 and latest.get("timed_out") is not True) or (state.run_id is not None and latest.get("run_id") != state.run_id):
                self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason="runner failure recovery does not match the last completed failed invocation")
                raise OrchestratorError("runner failure recovery does not match the last completed failed invocation")

        legacy_restored_stage = None
        if legacy_recovery:
            metadata = _read_json(self.store.run_dir(state.run_id) / "metadata.json")
            stage = str(metadata.get("stage") or Stage.AUTHORITY_CHANGE_ANALYSIS.value)
            if metadata.get("status") == "failed" and metadata.get("error") == "legacy unrelated drift stop explicitly recovered" and isinstance(metadata.get("real_drift"), list):
                drift = list(metadata["real_drift"])
                workspace = None
            else:
                workspace = RunWorkspace.from_metadata(metadata, Path(self.config.project_path))
                if metadata.get("status") != "running" or workspace is None:
                    raise OrchestratorError("RECOVERY_UNCERTAIN: historical Agent metadata is incomplete")
                if _agent_process_running(workspace.path):
                    raise OrchestratorError("RECOVERY_UNCERTAIN: Agent process is still running")
                drift = self._classify_real_drift(workspace, state, stage)
                _write_json(self.store.run_dir(state.run_id) / "real-drift.json", drift)
            blocking_drift = next((item for item in drift if item["classification"] in {"AUTHORITY_DRIFT", "ACCEPTED_UPSTREAM_DRIFT", "CURRENT_TARGET_DRIFT"}), None)
            if blocking_drift:
                raise OrchestratorError(f"{blocking_drift['classification']}: {blocking_drift['path']}")
            if workspace:
                workspace.discard()
            metadata.update({"status": "failed", "finished_at": metadata.get("finished_at") or now_iso(), "error": "legacy unrelated drift stop explicitly recovered", "real_drift": drift})
            _write_json(self.store.run_dir(state.run_id) / "metadata.json", metadata)
            legacy_restored_stage = stage

        if interrupted_recovery:
            metadata = _read_json(self.store.run_dir(state.run_id) / "metadata.json")
            workspace = RunWorkspace.from_metadata(metadata, Path(self.config.project_path))
            if metadata.get("status") != "running" or workspace is None:
                raise OrchestratorError("RECOVERY_UNCERTAIN: interrupted Agent metadata is incomplete")
            if _agent_process_running(workspace.path):
                raise OrchestratorError("RECOVERY_UNCERTAIN: Agent process is still running")
            drift = self._classify_real_drift(workspace, state, str(metadata.get("stage") or state.blocked_stage or ""))
            blocking_drift = next((item for item in drift if item["classification"] in {"AUTHORITY_DRIFT", "ACCEPTED_UPSTREAM_DRIFT", "CURRENT_TARGET_DRIFT"}), None)
            if blocking_drift:
                raise OrchestratorError(f"{blocking_drift['classification']}: {blocking_drift['path']}")
            workspace.discard()
            metadata.update({"status": "failed", "finished_at": now_iso(), "exit_code": -1, "timed_out": False, "error": "interrupted Agent invocation explicitly recovered", "real_drift": drift})
            _write_json(self.store.run_dir(state.run_id) / "metadata.json", metadata)

        if schema_recovery:
            late_outcome, late_errors, late_detected = self._late_outcome_check(state)
            if late_detected:
                self.logger.emit(
                    "late_outcome_detected",
                    run_id=state.run_id,
                    blocked_stage=state.blocked_stage,
                    verdict=late_outcome.get("verdict") if late_outcome else None,
                    resulting_stage=None,
                )
                if late_errors:
                    reason = "; ".join(late_errors)
                    self.logger.emit("late_outcome_validated", run_id=state.run_id, blocked_stage=state.blocked_stage, verdict=late_outcome.get("verdict") if late_outcome else None, resulting_stage=None, valid=False, reason=reason)
                    self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason=reason)
                    raise OrchestratorError(reason)
                audit, integrity, _ = self._audit_gate()
                if integrity or audit.blocking or not audit.is_repository or audit.error:
                    reason = "; ".join(integrity) or "; ".join(item.classification.value for item in audit.changes if item.classification.value in {"FROZEN_AUTHORITY_CHANGE", "MERGE_CONFLICT", "UNEXPECTED_UNRELATED_CHANGE"})
                    reason = reason or audit.error or "Git audit blocked"
                    self.logger.emit("late_outcome_validated", run_id=state.run_id, blocked_stage=state.blocked_stage, verdict=late_outcome["verdict"], resulting_stage=None, valid=False, reason=reason)
                    self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason=reason)
                    raise OrchestratorError(reason)
                reconciled = replace(
                    state,
                    current_stage=state.blocked_stage,
                    status=WorkflowStatus.RUNNING.value,
                    pending_human_gate=None,
                    stop_reason=None,
                    stop_code=None,
                    blocked_stage=None,
                    recoverable=False,
                )
                if Verdict(late_outcome["verdict"]) in SCOPED_DECISION_VERDICTS:
                    result = self._apply_outcome(reconciled, late_outcome)
                    saved = result.state
                else:
                    result = transition_after_outcome(self.config, reconciled, late_outcome)
                    saved = self._save(result.state)
                self.logger.emit("late_outcome_validated", run_id=state.run_id, blocked_stage=reconciled.current_stage, verdict=late_outcome["verdict"], resulting_stage=saved.current_stage, valid=True)
                self.logger.emit("late_outcome_reconciled", run_id=state.run_id, blocked_stage=reconciled.current_stage, verdict=late_outcome["verdict"], resulting_stage=saved.current_stage)
                return saved
            safety_errors = self._schema_recovery_errors(state)
            if safety_errors:
                reason = "; ".join(safety_errors)
                self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason=reason)
                raise OrchestratorError(reason)
        elif (not state.blocked_stage or state.run_id is not None) and not (runner_recovery or interrupted_recovery or legacy_recovery):
            self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason="recovery uncertainty")
            raise OrchestratorError("recovery uncertainty prevents resume")

        audit, integrity, _ = self._audit_gate()
        if integrity or audit.blocking or not audit.is_repository or audit.error:
            reason = "; ".join(integrity) or "; ".join(item.classification.value for item in audit.changes if item.classification.value in {"FROZEN_AUTHORITY_CHANGE", "MERGE_CONFLICT", "UNEXPECTED_UNRELATED_CHANGE"})
            reason = reason or audit.error or "Git audit blocked"
            self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason=reason)
            if schema_recovery:
                raise OrchestratorError(reason)
            return state
        restored_stage = legacy_restored_stage or state.blocked_stage
        if schema_recovery and restored_stage is None:
            restored_stage = self._latest_completed_stage()
        self.logger.emit("recovery_validation_passed", stop_code=state.stop_code, blocked_stage=restored_stage)
        new_state = replace(state, current_stage=restored_stage, status=WorkflowStatus.RUNNING.value, pending_human_gate=None, run_id=None, attempt=0, stop_reason=None, stop_code=None, blocked_stage=None, recoverable=False, updated_at=now_iso())
        self.logger.emit("hard_stop_recovered", stop_code=state.stop_code, restored_stage=restored_stage)
        return self._save(new_state)

    def _late_outcome_check(self, state: WorkflowState) -> tuple[dict[str, Any] | None, list[str], bool]:
        if not state.run_id or not state.blocked_stage:
            return None, [], False
        runs_root = self.store.runs_path.resolve()
        run_dir = (runs_root / state.run_id).resolve()
        if not run_dir.is_relative_to(runs_root):
            return None, ["RECOVERY_UNCERTAIN: late outcome run_id is outside the state store"], True
        outcome_path = run_dir / "outcome.json"
        if not outcome_path.is_file():
            return None, [], False
        valid, outcome, validation_errors = validate_outcome(outcome_path, state.run_id, state.blocked_stage)
        if not valid or outcome is None:
            identity_errors = {"run_id mismatch", "stage mismatch"}
            if identity_errors.intersection(validation_errors):
                return None, validation_errors, True
            return None, [], False

        errors: list[str] = []
        if state.blocked_stage not in READ_ONLY_RECOVERY_STAGES:
            errors.append("late outcome reconciliation requires a read-only verification stage")
        if state.workflow_digest != self.config.digest:
            errors.append("workflow digest changed; hot reload is not supported")
        if state.stop_code == "RECOVERY_UNCERTAIN" or "RECOVERY_UNCERTAIN" in (state.stop_reason or ""):
            errors.append("RECOVERY_UNCERTAIN prevents late outcome reconciliation")

        metadata = _read_json(run_dir / "metadata.json")
        if not metadata:
            errors.append("RECOVERY_UNCERTAIN: late outcome run metadata is missing")
        elif metadata.get("run_id") != state.run_id:
            errors.append("late outcome metadata run_id mismatch")
        elif metadata.get("stage") != state.blocked_stage:
            errors.append("late outcome metadata stage mismatch")
        if metadata.get("status") == "running":
            errors.append("RECOVERY_UNCERTAIN: the late outcome Agent invocation is still running")
        elif metadata and (metadata.get("status") != "completed" or metadata.get("exit_code") != 0 or metadata.get("timed_out") is not False):
            errors.append("late outcome run did not complete successfully")

        for metadata_path in self.store.runs_path.glob("*/metadata.json"):
            record = _read_json(metadata_path)
            if record.get("status") == "running":
                errors.append("RECOVERY_UNCERTAIN: an Agent invocation is still running")
                break
        return outcome, errors, True

    def _latest_completed_stage(self) -> str | None:
        records = []
        for metadata_path in self.store.runs_path.glob("*/metadata.json"):
            metadata = _read_json(metadata_path)
            if metadata:
                records.append(metadata)
        if not records:
            return None
        latest = max(
            records,
            key=lambda metadata: (
                str(metadata.get("finished_at") or metadata.get("started_at") or ""),
                str(metadata.get("run_id") or ""),
            ),
        )
        stage = latest.get("stage")
        return stage if isinstance(stage, str) else None

    def _schema_recovery_errors(self, state: WorkflowState) -> list[str]:
        errors: list[str] = []
        blocked_stage = state.blocked_stage or self._latest_completed_stage()
        if blocked_stage not in READ_ONLY_RECOVERY_STAGES:
            errors.append("RETRY_EXHAUSTED recovery requires a read-only verification stage")
        if blocked_stage == state.last_successful_stage or blocked_stage == Stage.TASK_EXECUTION.value:
            errors.append("recovery cannot rerun the last successful mutating stage")
        if state.stop_code == "RECOVERY_UNCERTAIN" or "RECOVERY_UNCERTAIN" in (state.stop_reason or ""):
            errors.append("RECOVERY_UNCERTAIN prevents resume")
        if state.workflow_digest != self.config.digest:
            errors.append("workflow digest changed; hot reload is not supported")
        if state.last_outcome is None or state.last_outcome.get("verdict") != Verdict.INVALID_OUTCOME.value:
            errors.append("RETRY_EXHAUSTED recovery requires an INVALID_OUTCOME failure verdict")

        records = []
        for metadata_path in self.store.runs_path.glob("*/metadata.json"):
            metadata = _read_json(metadata_path)
            if metadata:
                records.append((metadata_path, metadata))
        if any(metadata.get("status") == "running" for _, metadata in records):
            errors.append("RECOVERY_UNCERTAIN: an Agent invocation is still running")
        if not records:
            errors.append("RECOVERY_UNCERTAIN: no run metadata found")
            return errors
        latest_path, latest = max(
            records,
            key=lambda item: (
                str(item[1].get("finished_at") or item[1].get("started_at") or ""),
                str(item[1].get("run_id") or ""),
            ),
        )
        latest_run_id = latest.get("run_id")
        if latest.get("status") != "completed":
            errors.append("RECOVERY_UNCERTAIN: latest run metadata is not completed")
        if latest.get("stage") != blocked_stage:
            errors.append("latest completed run does not match the blocked read-only stage")
        if state.run_id is not None and state.run_id != latest_run_id:
            errors.append("latest completed run does not match the state run_id")
        if not isinstance(latest_run_id, str) or not latest_run_id:
            errors.append("RECOVERY_UNCERTAIN: latest run metadata has no run_id")
            return errors
        if latest.get("exit_code") != 0 or latest.get("timed_out") is not False:
            errors.append("latest Agent run did not complete successfully")
        outcome_path = latest_path.parent / "outcome.json"
        valid, outcome, validation_errors = validate_outcome(outcome_path, latest_run_id, blocked_stage)
        if not outcome_path.is_file() or outcome is None:
            errors.append("RECOVERY_UNCERTAIN: latest completed run has no outcome artifact")
        elif valid:
            errors.append("latest run outcome is valid; schema recovery is not applicable")
        elif not validation_errors:
            errors.append("latest run outcome failed validation without a deterministic schema error")
        return errors

    def status(self) -> WorkflowState:
        return self._load_or_initialize()

    def dry_run(self) -> dict[str, Any]:
        state = self.store.load() or initial_state(self.config, self.store)
        audit, integrity, scan = self._audit_gate()
        state = hydrate_external_artifacts(self.config, state, self.store)
        run_id = state.run_id or "dry-run"
        dry_changes = {str(change["change_id"]): change for change in scan.changes}
        dry_state = replace(state, run_id=run_id, current_stage=Stage.AUTHORITY_CHANGE_ANALYSIS.value if scan.changes else state.current_stage, current_authority_change_id=str(scan.changes[0]["change_id"]) if scan.changes else state.current_authority_change_id, authority_changes={**state.authority_changes, **dry_changes})
        prompt = self.prompt_builder.build(self.config, dry_state, self.store.root / "runs" / run_id / "outcome.json")
        authority_action = "authority_change_analysis" if scan.new_changes or any(str(item.get("change_id")) not in state.authority_changes or state.authority_changes.get(str(item.get("change_id")), {}).get("classification") is None for item in scan.changes) else None
        return {"project": self.config.project_name, "workflow_digest": self.config.digest, "stage": Stage.AUTHORITY_CHANGE_ANALYSIS.value if authority_action else state.current_stage, "possible_action": authority_action or ("blocked" if integrity or audit.blocking else ("agent_run" if state.current_stage in AGENT_STAGES else "transition")), "authority_changes": list(scan.changes), "authority_change_errors": list(scan.errors) + list(scan.unauthorized), "git_classifications": [item.value for item in audit.classifications], "integrity_errors": integrity, "prompt": prompt}


def doctor(project: str | Path, workflow_file: str | Path | None = None) -> dict[str, Any]:
    project_path = Path(project).expanduser().resolve()
    workflow_path = Path(workflow_file or project_path / ".contract-workflow" / "workflow.yaml").expanduser().resolve()
    report: dict[str, Any] = {"python": True, "project_path": str(project_path), "project_exists": project_path.is_dir(), "workflow_file": str(workflow_path), "workflow_exists": workflow_path.is_file(), "checks": []}
    if not project_path.is_dir() or not workflow_path.is_file():
        report["ok"] = False
        return report
    try:
        config = load_workflow(workflow_path, project_override=project_path)
        report["workflow_parse"] = True
    except WorkflowConfigError as exc:
        report["workflow_parse"] = False
        report["workflow_error"] = str(exc)
        report["ok"] = False
        return report
    schema_errors = workflow_schema_errors(workflow_path)
    report["workflow_schema"] = not schema_errors
    if schema_errors:
        report["workflow_schema_errors"] = schema_errors
    report["git_repository"] = (_git_repo(project_path))
    report["skills"] = {role: _skill_check(spec.path, spec.expected_version) for role, spec in config.skills.items()}
    report["authoritative_sources"] = source_integrity(project_path, config.authoritative_sources)
    audit = audit_git(project_path, config)
    report["git_audit"] = {"blocking": audit.blocking, "classifications": [item.value for item in audit.classifications], "changes": [{"path": item.path, "status": item.status, "classification": item.classification.value} for item in audit.changes]}
    root = state_root(project_path)
    report["state_dir"] = {"path": str(root), "writable": _writable_parent(root)}
    report["codex_runtime"] = "available" if shutil.which("codex") else "CODEX_RUNTIME_UNAVAILABLE"
    report["ok"] = bool(report["git_repository"] and report["workflow_parse"] and report["workflow_schema"] and report["project_exists"] and not report["authoritative_sources"] and not audit.blocking and report["state_dir"]["writable"] and all(report["skills"].values() or [True]))
    return report


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _git_repo(project: Path) -> bool:
    import subprocess
    return subprocess.run(["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, check=False).returncode == 0


def _writable_parent(path: Path) -> bool:
    probe = path if path.exists() else path.parent
    return os.access(probe, os.W_OK)


def _skill_check(path: str, expected: str | None) -> bool:
    file = Path(path).expanduser()
    if file.is_dir():
        file = file / "SKILL.md"
    if not file.is_file():
        return False
    if not expected:
        return True
    text = file.read_text(encoding="utf-8", errors="replace")
    return f'version: "{expected}"' in text or f"version: '{expected}'" in text
