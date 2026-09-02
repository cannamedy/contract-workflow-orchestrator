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


# Kept local to avoid making the scheduler depend on the state-machine's transition table.
AGENT_STAGE_NAMES = frozenset({
    Stage.TASK_EXECUTION.value,
    Stage.TASK_INDEPENDENT_REVIEW.value,
    Stage.TASK_PATCH.value,
    Stage.PLAN_DEFECT_RESOLUTION.value,
    Stage.PLAN_REVISION_REVIEW.value,
    Stage.FINAL_VERIFICATION.value,
})
HUMAN_GATE_NAMES = frozenset({
    Stage.HUMAN_PLAN_FREEZE.value,
    Stage.HUMAN_GROUP_APPROVAL.value,
    Stage.HUMAN_FINAL_ACCEPTANCE.value,
})


def _task_map(config: WorkflowConfig) -> dict[str, tuple[str, TaskSpec]]:
    return {task.id: (group_id, task) for group_id, task in config.tasks}


def _descendants(config: WorkflowConfig, roots: Iterable[str]) -> set[str]:
    task_map = _task_map(config)
    result: set[str] = set()
    frontier = set(roots)
    while frontier:
        current = frontier.pop()
        for task_id, (_, task) in task_map.items():
            if current in task.dependencies and task_id not in result and task_id not in roots:
                result.add(task_id)
                frontier.add(task_id)
    return result


def _fresh_work_items(config: WorkflowConfig, state: WorkflowState) -> dict[str, WorkItemState]:
    task_map = _task_map(config)
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
    task_map = _task_map(config)
    pending = [decision for decision in state.decisions.values() if decision.status == DecisionStatus.PENDING.value]
    all_ids = set(task_map)
    decisions: dict[str, HumanDecision] = {}
    direct_by_task: dict[str, list[str]] = {task_id: [] for task_id in all_ids}
    dependency_by_task: dict[str, list[str]] = {task_id: [] for task_id in all_ids}

    for decision in pending:
        direct = set(decision.directly_blocked_items) & all_ids
        dependent = _descendants(config, direct)
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

    items: dict[str, WorkItemState] = {}
    for task_id, item in state.work_items.items():
        if task_id not in task_map:
            continue
        _, task = task_map[task_id]
        direct_ids = sorted(direct_by_task[task_id])
        dependency_ids = sorted(dependency_by_task[task_id])
        status = item.status
        if status == WorkItemStatus.COMPLETED.value:
            direct_ids = []
            dependency_ids = []
        elif task_id == state.current_task and state.current_stage in AGENT_STAGE_NAMES:
            status = WorkItemStatus.REQUIRES_PATCH.value if state.current_stage == Stage.TASK_PATCH.value else WorkItemStatus.RUNNING.value
        elif direct_ids:
            status = WorkItemStatus.BLOCKED_BY_HUMAN_DECISION.value
        elif any(state.work_items.get(dep, WorkItemState(dep, "")).status != WorkItemStatus.COMPLETED.value for dep in task.dependencies):
            status = WorkItemStatus.WAITING_DEPENDENCY.value
        elif status in {
            WorkItemStatus.BLOCKED_BY_HUMAN_DECISION.value,
            WorkItemStatus.WAITING_DEPENDENCY.value,
            WorkItemStatus.READY.value,
        }:
            status = WorkItemStatus.READY.value
        items[task_id] = replace(item, status=status, dependencies=tuple(task.dependencies), blocking_decision_ids=direct_ids, dependency_blocked_by_decision_ids=dependency_ids)
    return replace(state, work_items=items, decisions=decisions)


def ready_work(config: WorkflowConfig, state: WorkflowState) -> list[WorkItemState]:
    state = recompute(config, state)
    result = []
    for _, task in config.tasks:
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
    if state.current_stage in HUMAN_GATE_NAMES:
        return state
    if state.current_stage in AGENT_STAGE_NAMES and state.current_task and state.run_id:
        return state
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
    if state.work_items and all(item.status == WorkItemStatus.COMPLETED.value for item in state.work_items.values()):
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
