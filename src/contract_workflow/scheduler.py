from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from .models import (
    Stage,
    DecisionStatus,
    HumanDecision,
    WorkItemState,
    WorkItemStatus,
    TaskSpec,
    WorkflowConfig,
    WorkflowState,
    WorkflowStatus,
)
from .artifacts import next_artifact, reconcile_artifact_staleness


# Kept local to avoid making the scheduler depend on the state-machine's transition table.
AGENT_STAGE_NAMES = frozenset({
    Stage.TASK_EXECUTION.value,
    Stage.TASK_INDEPENDENT_REVIEW.value,
    Stage.TASK_PATCH.value,
    Stage.PLAN_DEFECT_RESOLUTION.value,
    Stage.PLAN_REVISION_REVIEW.value,
    Stage.FINAL_VERIFICATION.value,
    Stage.AUTHORITY_CHANGE_ANALYSIS.value,
    Stage.CHANGE_PROPAGATION_PLANNING.value,
    Stage.CONTRACT_REVISION.value,
    Stage.CONTRACT_REVISION_REVIEW.value,
    Stage.PLAN_REVISION.value,
    Stage.PLAN_REVISION_REVIEW.value,
    Stage.PLAN_GRAPH_BUILD.value,
    Stage.TASK_REBASE_ANALYSIS.value,
    Stage.ARTIFACT_GENERATION.value, Stage.ARTIFACT_REVIEW.value, Stage.ARTIFACT_PATCH.value,
})
HUMAN_GATE_NAMES = frozenset({
    Stage.HUMAN_PLAN_FREEZE.value,
    Stage.HUMAN_GROUP_APPROVAL.value,
    Stage.HUMAN_FINAL_ACCEPTANCE.value,
})


def _task_map(config: WorkflowConfig, state: WorkflowState | None = None) -> dict[str, tuple[str, TaskSpec]]:
    if state and state.plan_graph and isinstance(state.plan_graph.get("tasks"), list):
        result: dict[str, tuple[str, TaskSpec]] = {}
        for raw in state.plan_graph["tasks"]:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                continue
            task = TaskSpec(
                id=raw["id"], expected_outputs=tuple(raw.get("expected_outputs", ()) or ()),
                allowed_paths=tuple(raw.get("allowed_paths", ()) or ()), dependencies=tuple(raw.get("dependencies", ()) or ()),
                requirement_ids=tuple(raw.get("requirement_ids", ()) or ()), contract_anchors=tuple(raw.get("contract_anchors", ()) or ()),
                skill_role=raw.get("skill_role"), engineering_spec_anchors=tuple(raw.get("engineering_spec_anchors", ()) or ()),
                machine_contract_refs=tuple(raw.get("machine_contract_refs", ()) or ()), conformance_ids=tuple(raw.get("conformance_ids", ()) or ()),
                implementation_design_refs=tuple(raw.get("implementation_design_refs", ()) or ()),
            )
            result[task.id] = (str(raw.get("group", "default")), task)
        if result:
            return result
    return {task.id: (group_id, task) for group_id, task in config.tasks}


def _descendants(config: WorkflowConfig, roots: Iterable[str], state: WorkflowState | None = None) -> set[str]:
    task_map = _task_map(config, state)
    result: set[str] = set()
    frontier = set(roots)
    while frontier:
        current = frontier.pop()
        for task_id, (_, task) in task_map.items():
            if current in task.dependencies and task_id not in result and task_id not in roots:
                result.add(task_id)
                frontier.add(task_id)
    return result


def dependency_closure(config: WorkflowConfig, roots: Iterable[str], state: WorkflowState | None = None) -> set[str]:
    """Return the deterministic downstream task closure for authority/decision blockers."""
    return _descendants(config, roots, state)


def _fresh_work_items(config: WorkflowConfig, state: WorkflowState) -> dict[str, WorkItemState]:
    task_map = _task_map(config, state)
    order = list(task_map)
    current_index = order.index(state.current_task) if state.current_task in order else None
    result: dict[str, WorkItemState] = {}
    for index, (task_id, (group_id, task)) in enumerate(task_map.items()):
        if state.current_stage in {Stage.FINAL_VERIFICATION.value, Stage.HUMAN_FINAL_ACCEPTANCE.value, Stage.COMPLETED.value}:
            status = WorkItemStatus.COMPLETED.value
        elif current_index is not None and index < current_index:
            status = WorkItemStatus.COMPLETED.value
        elif task_id == state.current_task and state.current_stage in AGENT_STAGE_NAMES:
            status = WorkItemStatus.RUNNING.value
        elif task.dependencies:
            status = WorkItemStatus.WAITING_DEPENDENCY.value
        else:
            status = WorkItemStatus.READY.value
        result[task_id] = WorkItemState(task_id, group_id, status, tuple(task.dependencies))
    return result


def ensure_work_items(config: WorkflowConfig, state: WorkflowState) -> WorkflowState:
    if state.work_items:
        return state
    return replace(state, work_items=_fresh_work_items(config, state))


def recompute(config: WorkflowConfig, state: WorkflowState) -> WorkflowState:
    state = ensure_work_items(config, state)
    task_map = _task_map(config, state)
    pending = [decision for decision in state.decisions.values() if decision.status == DecisionStatus.PENDING.value]
    all_ids = set(task_map)
    decisions: dict[str, HumanDecision] = {}
    direct_by_task: dict[str, list[str]] = {task_id: [] for task_id in all_ids}
    dependency_by_task: dict[str, list[str]] = {task_id: [] for task_id in all_ids}
    authority_direct_by_task: dict[str, list[str]] = {task_id: [] for task_id in all_ids}
    authority_dependency_by_task: dict[str, list[str]] = {task_id: [] for task_id in all_ids}

    for decision in pending:
        direct = set(decision.directly_blocked_items) & all_ids
        dependent = _descendants(config, direct, state)
        for task_id in direct:
            direct_by_task[task_id].append(decision.decision_id)
        for task_id in dependent - direct:
            dependency_by_task[task_id].append(decision.decision_id)
        decisions[decision.decision_id] = replace(
            decision,
            dependency_blocked_items=tuple(sorted(dependent - direct)),
            unaffected_items=tuple(sorted(all_ids - direct - dependent)),
        )
    decisions.update({decision.decision_id: decision for decision in state.decisions.values() if decision.status != DecisionStatus.PENDING.value})

    for change_id, change in state.authority_changes.items():
        if change.get("status") not in {"CHANGE_PENDING", "PROPAGATING"}:
            continue
        direct = set(change.get("directly_affected_tasks", ())) & all_ids
        dependent = _descendants(config, direct, state)
        for task_id in direct:
            authority_direct_by_task[task_id].append(change_id)
        for task_id in dependent - direct:
            authority_dependency_by_task[task_id].append(change_id)

    items: dict[str, WorkItemState] = {}
    source_items = dict(state.work_items)
    for task_id, (group_id, task) in task_map.items():
        source_items.setdefault(task_id, WorkItemState(task_id, group_id, WorkItemStatus.NEW.value, tuple(task.dependencies)))
    for task_id, item in state.work_items.items():
        if task_id not in task_map:
            items[task_id] = replace(item, status=WorkItemStatus.SUPERSEDED.value)
    for task_id, item in source_items.items():
        if task_id not in task_map:
            continue
        _, task = task_map[task_id]
        direct_ids = sorted(direct_by_task[task_id])
        dependency_ids = sorted(dependency_by_task[task_id])
        authority_direct_ids = sorted(authority_direct_by_task[task_id])
        authority_dependency_ids = sorted(authority_dependency_by_task[task_id])
        status = item.status
        if authority_direct_ids:
            requires_rebase = any(
                task_id in change.get("directly_affected_tasks", []) and change.get("propagation_ready")
                for change in state.authority_changes.values()
            )
            status = WorkItemStatus.TASK_REBASE_REQUIRED.value if requires_rebase else WorkItemStatus.BLOCKED_BY_AUTHORITY_CHANGE.value
        elif status == WorkItemStatus.COMPLETED.value:
            direct_ids = []
            dependency_ids = []
        elif task_id == state.current_task and state.current_stage in AGENT_STAGE_NAMES:
            status = WorkItemStatus.REQUIRES_PATCH.value if state.current_stage == Stage.TASK_PATCH.value else WorkItemStatus.RUNNING.value
        elif direct_ids:
            status = WorkItemStatus.BLOCKED_BY_HUMAN_DECISION.value
        elif authority_dependency_ids or dependency_ids or any(state.work_items.get(dep, WorkItemState(dep, "")).status != WorkItemStatus.COMPLETED.value for dep in task.dependencies):
            status = WorkItemStatus.WAITING_DEPENDENCY.value
        elif status in {
            WorkItemStatus.BLOCKED_BY_HUMAN_DECISION.value,
            WorkItemStatus.BLOCKED_BY_AUTHORITY_CHANGE.value,
            WorkItemStatus.TASK_REBASE_REQUIRED.value,
            WorkItemStatus.WAITING_DEPENDENCY.value,
            WorkItemStatus.READY.value,
            WorkItemStatus.NEW.value,
        }:
            status = WorkItemStatus.READY.value
        items[task_id] = replace(item, status=status, dependencies=tuple(task.dependencies), blocking_decision_ids=direct_ids, dependency_blocked_by_decision_ids=dependency_ids, blocking_authority_change_ids=authority_direct_ids, dependency_blocked_by_authority_change_ids=authority_dependency_ids)
    return replace(state, work_items=items, decisions=decisions)


def ready_work(config: WorkflowConfig, state: WorkflowState) -> list[WorkItemState]:
    state = recompute(config, state)
    result = []
    for _, task in _task_map(config, state).values():
        item = state.work_items.get(task.id)
        if item and item.status == WorkItemStatus.READY.value and all(
            state.work_items.get(dep, WorkItemState(dep, "")).status == WorkItemStatus.COMPLETED.value
            for dep in task.dependencies
        ):
            result.append(item)
    return result


def pending_decisions(state: WorkflowState) -> list[HumanDecision]:
    return [decision for decision in state.decisions.values() if decision.status == DecisionStatus.PENDING.value]


def schedule(config: WorkflowConfig, state: WorkflowState) -> WorkflowState:
    state = recompute(config, state)
    state = reconcile_artifact_staleness(config, state)
    if state.current_stage in HUMAN_GATE_NAMES:
        return state
    if state.current_stage in AGENT_STAGE_NAMES and state.current_task and state.run_id:
        return state
    artifact = next_artifact(config, state)
    if artifact:
        artifact_state = state.artifacts.get(artifact.id)
        if artifact_state and artifact_state.status == "REVIEW_REQUIRED":
            stage = Stage.ARTIFACT_REVIEW.value
        elif artifact_state and artifact_state.status == "REQUIRES_PATCH":
            stage = Stage.ARTIFACT_PATCH.value
        else:
            stage = Stage.ARTIFACT_GENERATION.value
        return replace(state, current_stage=stage, current_artifact_id=artifact.id, current_group=None, current_task=None, run_id=None, attempt=0, pending_human_gate=None, status=WorkflowStatus.RUNNING.value)
    ready = ready_work(config, state)
    if ready:
        item = ready[0]
        items = {**state.work_items, item.id: replace(item, status=WorkItemStatus.RUNNING.value)}
        return replace(
            state,
            work_items=items,
            current_stage=Stage.TASK_EXECUTION.value,
            current_group=item.group,
            current_task=item.id,
            run_id=None,
            attempt=0,
            pending_human_gate=None,
            status=WorkflowStatus.RUNNING.value,
        )
    if pending_decisions(state):
        return replace(
            state,
            current_stage=Stage.WAITING_FOR_HUMAN.value,
            current_group=None,
            current_task=None,
            run_id=None,
            attempt=0,
            pending_human_gate=None,
            status=WorkflowStatus.WAITING_HUMAN.value,
        )
    if any(
        artifact.status == "PROMOTION_READY"
        and any(spec.id == artifact.id and spec.promotion_policy == "EXTERNAL" for spec in config.artifact_pipeline)
        for artifact in state.artifacts.values()
    ):
        return replace(
            state,
            current_stage=Stage.WAITING_FOR_AUTHORITY_CHANGE.value,
            current_group=None,
            current_task=None,
            run_id=None,
            attempt=0,
            pending_human_gate=None,
            status=WorkflowStatus.WAITING_AUTHORITY_CHANGE.value,
        )
    propagation = next((item for item in state.propagation.values() if item.get("status") == "RUNNING" and item.get("next_stage")), None)
    if propagation:
        return replace(
            state,
            current_stage=str(propagation["next_stage"]), current_group=None, current_task=None, run_id=None,
            attempt=0, pending_human_gate=None, status=WorkflowStatus.RUNNING.value,
        )
    if any(change.get("status") in {"CHANGE_PENDING", "PROPAGATING"} for change in state.authority_changes.values()):
        return replace(
            state,
            current_stage=Stage.WAITING_FOR_AUTHORITY_CHANGE.value,
            current_group=None,
            current_task=None,
            run_id=None,
            attempt=0,
            pending_human_gate=None,
            status=WorkflowStatus.WAITING_AUTHORITY_CHANGE.value,
        )
    terminal = {WorkItemStatus.COMPLETED.value, WorkItemStatus.SUPERSEDED.value}
    if state.work_items and all(item.status in terminal for item in state.work_items.values()):
        return replace(state, current_stage=Stage.FINAL_VERIFICATION.value, current_group=None, current_task=None, run_id=None, attempt=0, status=WorkflowStatus.RUNNING.value)
    return replace(
        state,
        current_stage=Stage.HARD_STOP.value,
        status=WorkflowStatus.HARD_STOPPED.value,
        stop_reason="no READY work and no resolvable dependency or human decision",
        stop_code="DEPENDENCY_BLOCKED",
        blocked_stage=state.current_stage,
        recoverable=False,
    )
