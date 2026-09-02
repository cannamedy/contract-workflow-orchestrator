from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import WorkflowConfigError, load_workflow, workflow_schema_errors
from .git_audit import GitAudit, audit_git, source_integrity
from .logging import EventLogger
from .models import Stage, StepResult, Verdict, WorkflowConfig, WorkflowState, WorkflowStatus, now_iso
from .outcome import make_outcome, validate_outcome
from .prompt_builder import PromptBuilder
from .runners import AgentRunner, CodexCliRunner, MockRunner, RunnerResult
from .state_machine import AGENT_STAGES, HUMAN_GATES, approve, initial_state, stop, transition_after_outcome, transition_ready
from .state_store import StateStore, StateStoreError


class OrchestratorError(RuntimeError):
    pass


READ_ONLY_RECOVERY_STAGES = frozenset({
    Stage.TASK_INDEPENDENT_REVIEW.value,
    Stage.PLAN_REVISION_REVIEW.value,
    Stage.FINAL_VERIFICATION.value,
})


def state_root(project: Path) -> Path:
    configured = os.environ.get("CWO_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    key = str(project.resolve()).strip("/").replace("/", "_") or "root"
    return Path.home() / ".local" / "share" / "contract-workflow" / key


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
            state = initial_state(self.config)
            self.store.save(state)
            self.logger.emit("workflow_loaded", project=self.config.project_name, workflow_digest=self.config.digest)
        else:
            self.logger.emit("state_recovered", stage=state.current_stage, run_id=state.run_id)
            if state.workflow_digest != self.config.digest:
                state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason="workflow digest changed; hot reload is not supported", stop_code="WORKFLOW_DIGEST_CHANGED", blocked_stage=state.current_stage, recoverable=False, updated_at=now_iso())
                self.store.save(state)
                self.logger.emit("hard_stop_entered", reason=state.stop_reason)
        return state

    def _audit_gate(self) -> tuple[GitAudit, list[str]]:
        integrity = source_integrity(Path(self.config.project_path), self.config.authoritative_sources)
        audit = audit_git(Path(self.config.project_path), self.config)
        self.logger.emit("doctor_check", git_blocking=audit.blocking, integrity_errors=len(integrity), classifications=[item.value for item in audit.classifications])
        return audit, integrity

    def _save(self, state: WorkflowState) -> WorkflowState:
        self.store.save(state)
        return state

    def step(self) -> StepResult:
        state = self._load_or_initialize()
        if state.total_steps >= self.config.policy.max_total_steps and state.status == WorkflowStatus.RUNNING.value:
            state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, stop_reason="max_total_steps exceeded", stop_code="MAX_TOTAL_STEPS", blocked_stage=state.current_stage, recoverable=False, updated_at=now_iso())
            return StepResult(self._save(state), "hard_stop")
        if state.status in {WorkflowStatus.COMPLETED.value, WorkflowStatus.HARD_STOPPED.value, WorkflowStatus.FAILED.value, WorkflowStatus.STOPPED.value} or state.current_stage in HUMAN_GATES:
            return StepResult(state, "waiting")

        audit, integrity = self._audit_gate()
        if integrity or audit.blocking:
            reason = "; ".join(integrity) or "; ".join(item.classification.value for item in audit.changes if item.classification.value in {"FROZEN_AUTHORITY_CHANGE", "MERGE_CONFLICT", "UNEXPECTED_UNRELATED_CHANGE"})
            stop_code = "FROZEN_SOURCE_MISMATCH" if integrity else ("UNEXPECTED_UNRELATED_CHANGE" if any(item.classification.value == "UNEXPECTED_UNRELATED_CHANGE" for item in audit.changes) else ("MERGE_CONFLICT" if any(item.classification.value == "MERGE_CONFLICT" for item in audit.changes) else "GIT_AUDIT_BLOCKED"))
            new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason=reason or audit.error or "Git audit blocked", stop_code=stop_code, blocked_stage=state.current_stage, recoverable=stop_code == "UNEXPECTED_UNRELATED_CHANGE", updated_at=now_iso())
            self.logger.emit("hard_stop_entered", reason=new_state.stop_reason)
            return StepResult(self._save(new_state), "hard_stop")

        if state.current_stage in {Stage.INITIALIZING.value, Stage.READY.value}:
            result = transition_ready(self.config, state)
            self.logger.emit("transition", from_stage=state.current_stage, to_stage=result.state.current_stage)
            return StepResult(self._save(result.state), result.action)
        if state.current_stage in AGENT_STAGES:
            return self._agent_step(state)
        return StepResult(state, "waiting")

    def _agent_step(self, state: WorkflowState) -> StepResult:
        stage = state.current_stage
        if state.run_id:
            run_dir = self.store.run_dir(state.run_id)
            outcome_path = run_dir / "outcome.json"
            valid, outcome, errors = validate_outcome(outcome_path, state.run_id, stage)
            metadata = _read_json(run_dir / "metadata.json")
            if valid and outcome:
                self.logger.emit("state_recovered", run_id=state.run_id, stage=stage, reason="valid outcome reconciled")
                return self._apply_outcome(state, outcome)
            if metadata.get("status") == "running" and not outcome_path.exists():
                new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason="RECOVERY_UNCERTAIN: prior Agent invocation has no completed artifact", stop_code="RECOVERY_UNCERTAIN", blocked_stage=stage, recoverable=False, updated_at=now_iso())
                self.logger.emit("hard_stop_entered", reason=new_state.stop_reason)
                return StepResult(self._save(new_state), "hard_stop")
            return self._invalid_or_failed(state, errors or ["invalid outcome"])

        run_id = uuid.uuid4().hex
        run_dir = self.store.run_dir(run_id)
        state = replace(state, run_id=run_id, attempt=state.attempt + 1, total_steps=state.total_steps + 1, updated_at=now_iso())
        self._save(state)
        outcome_path = run_dir / "outcome.json"
        prompt = self.prompt_builder.build(self.config, state, outcome_path)
        (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        _write_json(run_dir / "metadata.json", {"run_id": run_id, "stage": stage, "status": "running", "started_at": now_iso()})
        self.logger.emit("stage_entered", stage=stage, group=state.current_group, task=state.current_task)
        self.logger.emit("agent_run_started", run_id=run_id, stage=stage, attempt=state.attempt)
        result = self.runner.run(Path(self.config.project_path), prompt, run_dir, self.config.runner.timeout_seconds, env={"CWO_RUN_ID": run_id, "CWO_OUTCOME_PATH": str(outcome_path)})
        _write_json(run_dir / "metadata.json", {"run_id": run_id, "stage": stage, "status": "completed", "started_at": result.started_at, "finished_at": result.finished_at, "exit_code": result.exit_code, "timed_out": result.timed_out, "runner_metadata": result.runner_metadata})
        self.logger.emit("agent_run_finished", run_id=run_id, stage=stage, exit_code=result.exit_code, timed_out=result.timed_out)
        valid, outcome, errors = validate_outcome(outcome_path, run_id, stage)
        if result.exit_code != 0 or result.timed_out:
            errors = ["runner timed out" if result.timed_out else f"runner process failed with exit code {result.exit_code}"]
            return self._invalid_or_failed(state, errors)
        if not valid or not outcome:
            return self._invalid_or_failed(state, errors)
        self.logger.emit("outcome_validated", run_id=run_id, stage=stage, verdict=outcome["verdict"])
        return self._apply_outcome(state, outcome)

    def _apply_outcome(self, state: WorkflowState, outcome: dict[str, Any]) -> StepResult:
        result = transition_after_outcome(self.config, state, outcome)
        self.logger.emit("transition", from_stage=state.current_stage, to_stage=result.state.current_stage, verdict=outcome["verdict"])
        return StepResult(self._save(result.state), result.action, result.retry_delay)

    def _invalid_or_failed(self, state: WorkflowState, errors: list[str]) -> StepResult:
        self.logger.emit("outcome_invalid", run_id=state.run_id, stage=state.current_stage, errors=errors)
        if state.attempt >= self.config.policy.max_attempts_per_stage:
            new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason="; ".join(errors), stop_code="RETRY_EXHAUSTED", blocked_stage=state.current_stage, recoverable=False, updated_at=now_iso())
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
        return self._save(result.state)

    def stop(self, reason: str = "stopped by operator") -> WorkflowState:
        state = self._load_or_initialize()
        return self._save(stop(state, reason))

    def recover(self) -> WorkflowState:
        state = self._load_or_initialize()
        self.logger.emit("recovery_requested", stop_code=state.stop_code, blocked_stage=state.blocked_stage)
        legacy_recovery = (
            state.recoverable
            and state.stop_code == "UNEXPECTED_UNRELATED_CHANGE"
        )
        schema_recovery = state.stop_code == "RETRY_EXHAUSTED"
        if state.current_stage != Stage.HARD_STOP.value or not (legacy_recovery or schema_recovery):
            self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason="stop is not recoverable")
            raise OrchestratorError("hard stop is not recoverable")

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
                audit, integrity = self._audit_gate()
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
                result = transition_after_outcome(self.config, reconciled, late_outcome)
                self.logger.emit("late_outcome_validated", run_id=state.run_id, blocked_stage=reconciled.current_stage, verdict=late_outcome["verdict"], resulting_stage=result.state.current_stage, valid=True)
                self.logger.emit("transition", from_stage=reconciled.current_stage, to_stage=result.state.current_stage, verdict=late_outcome["verdict"])
                saved = self._save(result.state)
                self.logger.emit("late_outcome_reconciled", run_id=state.run_id, blocked_stage=reconciled.current_stage, verdict=late_outcome["verdict"], resulting_stage=saved.current_stage)
                return saved
            safety_errors = self._schema_recovery_errors(state)
            if safety_errors:
                reason = "; ".join(safety_errors)
                self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason=reason)
                raise OrchestratorError(reason)
        elif not state.blocked_stage or state.run_id is not None:
            self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason="recovery uncertainty")
            raise OrchestratorError("recovery uncertainty prevents resume")

        audit, integrity = self._audit_gate()
        if integrity or audit.blocking or not audit.is_repository or audit.error:
            reason = "; ".join(integrity) or "; ".join(item.classification.value for item in audit.changes if item.classification.value in {"FROZEN_AUTHORITY_CHANGE", "MERGE_CONFLICT", "UNEXPECTED_UNRELATED_CHANGE"})
            reason = reason or audit.error or "Git audit blocked"
            self.logger.emit("recovery_validation_failed", stop_code=state.stop_code, blocked_stage=state.blocked_stage, reason=reason)
            if schema_recovery:
                raise OrchestratorError(reason)
            return state
        restored_stage = state.blocked_stage
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
        state = self.store.load() or initial_state(self.config)
        audit, integrity = self._audit_gate()
        run_id = state.run_id or "dry-run"
        dry_state = replace(state, run_id=run_id)
        prompt = self.prompt_builder.build(self.config, dry_state, self.store.root / "runs" / run_id / "outcome.json")
        return {"project": self.config.project_name, "workflow_digest": self.config.digest, "stage": state.current_stage, "possible_action": "blocked" if integrity or audit.blocking else ("agent_run" if state.current_stage in AGENT_STAGES else "transition"), "git_classifications": [item.value for item in audit.classifications], "integrity_errors": integrity, "prompt": prompt}


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
