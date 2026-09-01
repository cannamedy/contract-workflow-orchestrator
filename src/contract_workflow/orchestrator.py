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
                state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason="workflow digest changed; hot reload is not supported", updated_at=now_iso())
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
            state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, stop_reason="max_total_steps exceeded", updated_at=now_iso())
            return StepResult(self._save(state), "hard_stop")
        if state.status in {WorkflowStatus.COMPLETED.value, WorkflowStatus.HARD_STOPPED.value, WorkflowStatus.FAILED.value, WorkflowStatus.STOPPED.value} or state.current_stage in HUMAN_GATES:
            return StepResult(state, "waiting")

        audit, integrity = self._audit_gate()
        if integrity or audit.blocking:
            reason = "; ".join(integrity) or "; ".join(item.classification.value for item in audit.changes if item.classification.value in {"FROZEN_AUTHORITY_CHANGE", "MERGE_CONFLICT", "UNEXPECTED_UNRELATED_CHANGE"})
            new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason=reason or audit.error or "Git audit blocked", updated_at=now_iso())
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
                new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason="RECOVERY_UNCERTAIN: prior Agent invocation has no completed artifact", updated_at=now_iso())
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
            new_state = replace(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason="; ".join(errors), updated_at=now_iso())
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
