from __future__ import annotations

from dataclasses import replace
from typing import Any

from .models import BLOCKING_VERDICTS, SCOPED_DECISION_VERDICTS, Stage, StepResult, Verdict, WorkflowConfig, WorkflowState, WorkflowStatus, now_iso


AGENT_STAGES = {
    Stage.TASK_EXECUTION.value, Stage.TASK_INDEPENDENT_REVIEW.value,
    Stage.TASK_PATCH.value, Stage.PLAN_DEFECT_RESOLUTION.value,
    Stage.PLAN_REVISION_REVIEW.value, Stage.FINAL_VERIFICATION.value,
    Stage.AUTHORITY_CHANGE_ANALYSIS.value,
}
HUMAN_GATES = {Stage.HUMAN_GROUP_APPROVAL.value, Stage.HUMAN_PLAN_FREEZE.value, Stage.HUMAN_FINAL_ACCEPTANCE.value}


def initial_state(config: WorkflowConfig) -> WorkflowState:
    return WorkflowState(project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file, workflow_digest=config.digest)


def _update(state: WorkflowState, **changes: Any) -> WorkflowState:
    return replace(state, **changes, updated_at=now_iso(), total_steps=state.total_steps + 1)


def _next_task(config: WorkflowConfig, group: str | None, task: str | None) -> tuple[str, str] | None:
    tasks = [(gid, spec.id) for gid, spec in config.tasks]
    if not tasks:
        return None
    if group is None or task is None:
        return tasks[0]
    try:
        index = tasks.index((group, task)) + 1
    except ValueError:
        return None
    return tasks[index] if index < len(tasks) else None


def _enter_hard_stop(state: WorkflowState, reason: str, stop_code: str = "HARD_STOP", recoverable: bool = False) -> StepResult:
    new_state = _update(state, current_stage=Stage.HARD_STOP.value, status=WorkflowStatus.HARD_STOPPED.value, pending_human_gate=None, stop_reason=reason, stop_code=stop_code, blocked_stage=state.current_stage, recoverable=recoverable)
    return StepResult(new_state, "hard_stop")


def transition_after_outcome(config: WorkflowConfig, state: WorkflowState, outcome: dict[str, Any]) -> StepResult:
    """Pure deterministic transition; prose is never consulted."""
    verdict = Verdict(outcome["verdict"])
    stage = Stage(state.current_stage)
    if stage == Stage.AUTHORITY_CHANGE_ANALYSIS:
        if verdict == Verdict.APPROVED:
            return StepResult(_update(state, current_stage=Stage.READY.value, current_group=None, current_task=None, run_id=None, attempt=0, last_outcome=outcome), "authority_change_analyzed")
    if verdict in SCOPED_DECISION_VERDICTS:
        return StepResult(_update(state, current_stage=Stage.WAITING_FOR_HUMAN.value, status=WorkflowStatus.WAITING_HUMAN.value, pending_human_gate=None, run_id=None), "scoped_human_decision")
    if verdict in BLOCKING_VERDICTS:
        return _enter_hard_stop(state, f"{verdict.value}: {outcome.get('summary', '')}".strip(), stop_code=verdict.value)
    if stage == Stage.TASK_EXECUTION:
        if verdict == Verdict.APPROVED:
            return StepResult(_update(state, current_stage=Stage.TASK_INDEPENDENT_REVIEW.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "transition")
    elif stage == Stage.TASK_INDEPENDENT_REVIEW:
        if verdict == Verdict.REQUIRES_PATCH:
            if not config.policy.auto_patch:
                return _enter_hard_stop(state, "REQUIRES_PATCH but auto_patch is disabled", stop_code="AUTO_PATCH_DISABLED")
            return StepResult(_update(state, current_stage=Stage.TASK_PATCH.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "transition")
        if verdict == Verdict.PLAN_TASK_DEFECT:
            if not config.policy.auto_plan_defect_resolution:
                return _enter_hard_stop(state, "PLAN_TASK_DEFECT requires plan defect resolution", stop_code="PLAN_DEFECT_RESOLUTION_DISABLED")
            return StepResult(_update(state, current_stage=Stage.PLAN_DEFECT_RESOLUTION.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "transition")
        if verdict == Verdict.APPROVED:
            if config.mode == "gated":
                return StepResult(_update(state, current_stage=Stage.HUMAN_GROUP_APPROVAL.value, pending_human_gate=Stage.HUMAN_GROUP_APPROVAL.value, status=WorkflowStatus.WAITING_HUMAN.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "human_gate")
            next_task = _next_task(config, state.current_group, state.current_task)
            if next_task:
                return StepResult(_update(state, current_group=next_task[0], current_task=next_task[1], current_stage=Stage.TASK_EXECUTION.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "transition")
            return StepResult(_update(state, current_stage=Stage.FINAL_VERIFICATION.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "transition")
    elif stage == Stage.TASK_PATCH:
        if verdict == Verdict.APPROVED:
            return StepResult(_update(state, current_stage=Stage.TASK_INDEPENDENT_REVIEW.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "transition")
    elif stage == Stage.PLAN_DEFECT_RESOLUTION:
        if verdict == Verdict.APPROVED:
            return StepResult(_update(state, current_stage=Stage.PLAN_REVISION_REVIEW.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "transition")
    elif stage == Stage.PLAN_REVISION_REVIEW:
        if verdict == Verdict.REQUIRES_PATCH:
            if not config.policy.auto_plan_revision_review:
                return _enter_hard_stop(state, "plan revision review patch is disabled", stop_code="PLAN_REVIEW_PATCH_DISABLED")
            return StepResult(_update(state, current_stage=Stage.PLAN_DEFECT_RESOLUTION.value, run_id=None, attempt=0, last_outcome=outcome), "transition")
        if verdict == Verdict.APPROVED:
            return StepResult(_update(state, current_stage=Stage.HUMAN_PLAN_FREEZE.value, pending_human_gate=Stage.HUMAN_PLAN_FREEZE.value, status=WorkflowStatus.WAITING_HUMAN.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "human_gate")
    elif stage == Stage.FINAL_VERIFICATION:
        if verdict in {Verdict.APPROVED, Verdict.COMPLETED}:
            if config.mode == "gated":
                return StepResult(_update(state, current_stage=Stage.HUMAN_FINAL_ACCEPTANCE.value, pending_human_gate=Stage.HUMAN_FINAL_ACCEPTANCE.value, status=WorkflowStatus.WAITING_HUMAN.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "human_gate")
            return StepResult(_update(state, current_stage=Stage.COMPLETED.value, status=WorkflowStatus.COMPLETED.value, last_successful_stage=stage.value, run_id=None, attempt=0, last_outcome=outcome), "completed")
    return _enter_hard_stop(state, f"unsupported verdict {verdict.value} for stage {stage.value}", stop_code="UNSUPPORTED_VERDICT")


def transition_ready(config: WorkflowConfig, state: WorkflowState) -> StepResult:
    if state.current_stage == Stage.INITIALIZING.value:
        return StepResult(_update(state, current_stage=Stage.READY.value), "transition")
    if state.current_stage == Stage.READY.value:
        first = _next_task(config, None, None)
        if not first:
            return StepResult(_update(state, current_stage=Stage.FINAL_VERIFICATION.value), "transition")
        return StepResult(_update(state, current_stage=Stage.TASK_EXECUTION.value, current_group=first[0], current_task=first[1]), "transition")
    raise ValueError(f"stage {state.current_stage} is not ready for a setup transition")


def approve(config: WorkflowConfig, state: WorkflowState, gate: str | None = None) -> StepResult:
    if state.current_stage not in HUMAN_GATES or state.pending_human_gate != state.current_stage:
        raise ValueError("no approvable human gate is pending")
    if gate and gate != state.current_stage:
        raise ValueError(f"requested gate {gate} does not match {state.current_stage}")
    current = Stage(state.current_stage)
    if current == Stage.HUMAN_GROUP_APPROVAL:
        next_task = _next_task(config, state.current_group, state.current_task)
        if next_task:
            return StepResult(_update(state, current_group=next_task[0], current_task=next_task[1], current_stage=Stage.TASK_EXECUTION.value, pending_human_gate=None, status=WorkflowStatus.RUNNING.value, run_id=None, attempt=0), "approved")
        return StepResult(_update(state, current_stage=Stage.FINAL_VERIFICATION.value, pending_human_gate=None, status=WorkflowStatus.RUNNING.value, run_id=None, attempt=0), "approved")
    if current == Stage.HUMAN_PLAN_FREEZE:
        return StepResult(_update(state, current_stage=Stage.TASK_EXECUTION.value, pending_human_gate=None, status=WorkflowStatus.RUNNING.value, run_id=None, attempt=0), "approved")
    return StepResult(_update(state, current_stage=Stage.COMPLETED.value, pending_human_gate=None, status=WorkflowStatus.COMPLETED.value, run_id=None, attempt=0), "completed")


def stop(state: WorkflowState, reason: str = "stopped by operator") -> WorkflowState:
    return _update(state, status=WorkflowStatus.STOPPED.value, stop_reason=reason, pending_human_gate=None)
