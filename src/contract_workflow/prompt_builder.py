from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import Stage, WorkflowConfig, WorkflowState
from .outcome import render_outcome_contract


DEFAULT_TEMPLATES = {
    "TASK_EXECUTION": "task-execution.md",
    "TASK_INDEPENDENT_REVIEW": "task-review.md",
    "TASK_PATCH": "task-patch.md",
    "PLAN_DEFECT_RESOLUTION": "plan-defect-resolution.md",
    "PLAN_REVISION_REVIEW": "plan-revision-review.md",
    "FINAL_VERIFICATION": "task-review.md",
    "AUTHORITY_CHANGE_ANALYSIS": "authority-change-analysis.md",
    "CHANGE_PROPAGATION_PLANNING": "authority-change-analysis.md",
    "CONTRACT_REVISION": "authority-change-analysis.md",
    "CONTRACT_REVISION_REVIEW": "task-review.md",
    "PLAN_REVISION": "authority-change-analysis.md",
    "PLAN_REVISION_REVIEW": "task-review.md",
    "PLAN_GRAPH_BUILD": "authority-change-analysis.md",
    "TASK_REBASE_ANALYSIS": "authority-change-analysis.md",
    "ARTIFACT_GENERATION": "authority-change-analysis.md",
    "ARTIFACT_REVIEW": "task-review.md",
    "ARTIFACT_PATCH": "authority-change-analysis.md",
}
DEFAULT_TEXTS = {
    "TASK_EXECUTION": "# Task Execution\n\nWork only on the current task using the configured Skill.",
    "TASK_INDEPENDENT_REVIEW": "# Independent Review\n\nReview the current task independently using the configured Skill.",
    "TASK_PATCH": "# Task Patch\n\nAddress only the current review issues.",
    "PLAN_DEFECT_RESOLUTION": "# Plan Defect Resolution\n\nResolve only the reported planning defect.",
    "PLAN_REVISION_REVIEW": "# Plan Revision Review\n\nReview the revised plan against authoritative artifacts.",
    "FINAL_VERIFICATION": "# Final Verification\n\nVerify the completed workflow against its authoritative artifacts.",
    "AUTHORITY_CHANGE_ANALYSIS": "# Authority Change Analysis\n\nCompare the accepted authority revision with the registered candidate and produce only the structured impact analysis. Do not edit authority, Contract, Plan, or task artifacts.",
    "CHANGE_PROPAGATION_PLANNING": "# Change Propagation Planning\n\nTurn the accepted Change Record into an ordered machine propagation plan. Do not modify project authority files.",
    "CONTRACT_REVISION": "# Contract Revision\n\nProduce an incremental candidate Engineering Contract in the external propagation store. Do not modify the accepted project Contract.",
    "CONTRACT_REVISION_REVIEW": "# Contract Revision Review\n\nIndependently review the candidate Contract against the candidate Human Guide, requirements, anchors, and unaffected Contract sections.",
    "PLAN_REVISION": "# Plan Revision\n\nProduce an incremental candidate Implementation Plan and structured task graph in the external propagation store. Do not modify the accepted project Plan.",
    "PLAN_REVISION_REVIEW": "# Plan Revision Review\n\nIndependently verify the candidate Plan and its Requirement-to-Contract-to-Task closure.",
    "PLAN_GRAPH_BUILD": "# Plan Graph Build\n\nEmit a complete machine-readable Plan Graph projection. Do not infer it from unvalidated prose in later stages.",
    "TASK_REBASE_ANALYSIS": "# Task Rebase Analysis\n\nCompare affected task definitions and existing implementation/review evidence, preserving the smallest required rebase work.",
    "ARTIFACT_GENERATION": "# Artifact Generation\n\nProduce the configured candidate artifact using its routed Skill. Do not modify accepted project authority.",
    "ARTIFACT_REVIEW": "# Artifact Review\n\nIndependently review the candidate artifact against its declared inputs and traceability.",
    "ARTIFACT_PATCH": "# Artifact Patch\n\nRepair only the reported candidate artifact defects, preserving unaffected content.",
}
STAGE_VERDICT_GUIDANCE = {
    "TASK_EXECUTION": "Use APPROVED when the requested implementation and tests are complete; COMPLETED is not valid for this stage.",
    "TASK_INDEPENDENT_REVIEW": "Use APPROVED when the current artifacts satisfy the Contract, or REQUIRES_PATCH when the implementation has a defect.",
    "TASK_PATCH": "Use APPROVED when the requested patch is complete and verified; report a structured blocking verdict when it cannot safely proceed.",
    "PLAN_DEFECT_RESOLUTION": "Use APPROVED when the planning defect is resolved; report a structured blocking verdict when it cannot safely proceed.",
    "PLAN_REVISION_REVIEW": "Use APPROVED when the revised plan is correct, or REQUIRES_PATCH when it still has a defect.",
    "FINAL_VERIFICATION": "Use APPROVED or COMPLETED only when final verification succeeds.",
    "AUTHORITY_CHANGE_ANALYSIS": "Use APPROVED for a valid machine-resolvable analysis, or ARCHITECTURE_DECISION_REQUIRED only when the authority semantics have no unique safe interpretation.",
    "CHANGE_PROPAGATION_PLANNING": "Use APPROVED when the ordered propagation plan is complete; use a scoped decision verdict only for genuine authority ambiguity.",
    "CONTRACT_REVISION": "Use APPROVED when the candidate Contract and revision report are complete; do not write the accepted Contract.",
    "CONTRACT_REVISION_REVIEW": "Use APPROVED when the candidate Contract is aligned and traceable, or REQUIRES_PATCH for a repairable defect.",
    "PLAN_REVISION": "Use APPROVED when the candidate Plan is complete and incremental; do not write the accepted Plan.",
    "PLAN_REVISION_REVIEW": "Use APPROVED when the candidate Plan is complete and graph-valid, or REQUIRES_PATCH for a repairable defect.",
    "PLAN_GRAPH_BUILD": "Use APPROVED only with a complete valid plan_graph object.",
    "TASK_REBASE_ANALYSIS": "Use APPROVED when affected task rebase evidence is complete; do not modify implementation files in this stage.",
    "ARTIFACT_GENERATION": "Use APPROVED when the candidate artifact is complete; include its id, kind, candidate_hash, and candidate_content or external candidate evidence.",
    "ARTIFACT_REVIEW": "Use APPROVED when the candidate artifact is valid and traceable, or REQUIRES_PATCH for a machine-repairable defect.",
    "ARTIFACT_PATCH": "Use APPROVED when the repaired candidate artifact is complete; report a scoped decision only for genuine authority ambiguity.",
}


class PromptBuilder:
    def __init__(self, template_dir: Path | None = None):
        self.template_dir = template_dir or Path(__file__).resolve().parents[2] / "templates"

    def build(self, config: WorkflowConfig, state: WorkflowState, outcome_path: Path, execution_workspace: Path | None = None) -> str:
        stage = state.current_stage
        template_name = DEFAULT_TEMPLATES.get(stage)
        template = DEFAULT_TEXTS.get(stage, "")
        if template_name:
            path = self.template_dir / template_name
            if path.is_file():
                template = path.read_text(encoding="utf-8")
        skill = _skill_for_stage(config, stage, state)
        frozen = "\n".join(f"- {item.path} sha256={item.sha256} commit={item.git_commit or '-'} tag={item.git_tag or '-'}" for item in config.authoritative_sources) or "- none declared"
        task = config.task_at(state.current_group, state.current_task)
        task_paths = []
        task_requirements = "none declared"
        task_anchors = "none declared"
        artifact_context = "none"
        artifact_spec = next((item for item in config.artifact_pipeline if item.id == state.current_artifact_id), None)
        if artifact_spec:
            artifact_context = f"id={artifact_spec.id}, kind={artifact_spec.kind}, dependencies={list(artifact_spec.dependencies)}, validator_role={artifact_spec.validator_role or 'none'}, review_required={artifact_spec.review_required}"
        if stage == Stage.FINAL_VERIFICATION.value and config.artifact_pipeline_explicit:
            artifact_context = "approved CONFORMANCE_SPEC is required; emit conformance_results mapping requirement_id to conformance_id, evidence, and PASS/FAIL"
        if task:
            task_paths.extend(task.expected_outputs)
            task_paths.extend(path for path in task.allowed_paths if path not in task_paths)
            task_requirements = ", ".join(task.requirement_ids) or task_requirements
            task_anchors = ", ".join(task.contract_anchors) or task_anchors
        allowed = "- .contract-workflow runtime tracker (orchestrator-owned)"
        if task_paths:
            allowed += "\n" + "\n".join(f"- {path}" for path in task_paths)
        else:
            allowed += "\n- no task-specific mutable paths declared"
        previous_issues = _previous_issues(state)
        if stage == "TASK_INDEPENDENT_REVIEW":
            previous_issues = "not provided to preserve review independence; inspect the current repository and frozen authority directly"
        pending = [change for change in state.authority_changes.values() if change.get("status") in {"CHANGE_PENDING", "PROPAGATING"}]
        authority_context = "none"
        if stage == Stage.AUTHORITY_CHANGE_ANALYSIS.value and state.current_authority_change_id:
            change = state.authority_changes.get(state.current_authority_change_id, {})
            authority_context = str(change)
            snapshot = change.get("candidate_snapshot_path") if isinstance(change, dict) else None
            if snapshot:
                authority_context += f" Read the immutable remote candidate snapshot at `{snapshot}`; do not use the local Human Draft Workspace copy as the candidate."
        elif pending and state.current_task and any(state.current_task in change.get("unaffected_tasks", []) for change in pending):
            authority_context = "There is a registered pending authority change " + ", ".join(str(change.get("change_id")) for change in pending) + ". This task has been deterministically classified as unaffected. Do not modify affected authority artifacts. Operate only within this task's allowed scope."
        if stage in {Stage.CHANGE_PROPAGATION_PLANNING.value, Stage.CONTRACT_REVISION.value, Stage.CONTRACT_REVISION_REVIEW.value, Stage.PLAN_REVISION.value, Stage.PLAN_REVISION_REVIEW.value, Stage.PLAN_GRAPH_BUILD.value, Stage.TASK_REBASE_ANALYSIS.value}:
            active = state.propagation.get(state.current_authority_change_id or "", {})
            authority_context = "Registered propagation context: " + str(active or "unavailable") + ". Candidate artifacts are external runtime evidence and must not be written over accepted project authorities."
        execution_path = str(execution_workspace or config.project_path)
        values = {
            "project_path": execution_path, "authoritative_origin": config.project_path, "project": config.project_name, "stage": stage,
            "group": state.current_group or "", "task": state.current_task or "",
            "run_id": state.run_id or outcome_path.parent.name,
            "skill_path": skill.path if skill else "(no Skill configured)", "frozen_authority": frozen,
            "outcome_path": str(outcome_path), "allowed_scope": allowed,
            "hard_stops": ", ".join(config.hard_stops) or "OPEN_CONTRACT_ISSUE, ARCHITECTURE_DECISION_REQUIRED, SECURITY_SENSITIVE_ACTION, DESTRUCTIVE_ACTION_REQUIRED, FROZEN_SOURCE_MISMATCH",
            "task_requirements": task_requirements, "task_anchors": task_anchors,
            "attempt": str(state.attempt), "previous_issue_summary": previous_issues,
            "verdict_guidance": STAGE_VERDICT_GUIDANCE.get(stage, "Use only a verdict valid for the current stage."),
            "authority_context": authority_context,
            "artifact_context": artifact_context,
        }
        try:
            body = template.format(**values)
        except (KeyError, ValueError):
            body = template
        context = f"PROJECT ID: {config.project_name}\nEXECUTION WORKSPACE: {execution_path}\nAUTHORITATIVE ORIGIN: {config.project_path}\nCURRENT STAGE: {stage}\nCURRENT ARTIFACT: {state.current_artifact_id or ''}\nARTIFACT CONTEXT: {artifact_context}\nCURRENT GROUP: {state.current_group or ''}\nCURRENT TASK: {state.current_task or ''}\nRUN ID: {values['run_id']}"
        return context + "\n\n" + body.rstrip() + "\n\n" + self._contract(values)

    @staticmethod
    def _contract(values: dict[str, str]) -> str:
        return f'''## ORCHESTRATOR OUTPUT CONTRACT

Re-read the relevant `SKILL.md` at `{values['skill_path']}` before acting. Execute only the current stage `{values['stage']}` for project `{values['project']}` in execution workspace `{values['project_path']}`. The authoritative origin repository is `{values['authoritative_origin']}`. Do not access or modify the authoritative origin repository directly. Respect the frozen authority metadata below and the allowed scope. Write a human-readable report, then write valid machine-readable JSON exactly to `{values['outcome_path']}`. Do not modify orchestrator workflow state or infer transitions from prose.

Frozen authority:
{values['frozen_authority']}

Attempt: {values['attempt']}
Allowed scope:
{values['allowed_scope']}

Hard stops: {values['hard_stops']}
Task Requirement IDs: {values['task_requirements']}
Task Contract anchors: {values['task_anchors']}
Authority change context: {values['authority_context']}
Artifact context: {values['artifact_context']}
Stage verdict guidance: {values['verdict_guidance']}
Previous issue summary: {values['previous_issue_summary']}

For `ARCHITECTURE_DECISION_REQUIRED` or an unresolved `OPEN_CONTRACT_ISSUE`, include `decision_id` or `decision_requests`, `directly_affected_work`, and `blocking_scope` in the machine-readable outcome.

For `AUTHORITY_CHANGE_ANALYSIS`, include an `authority_change` object with change_id, base_sha256, candidate_sha256, classification (C0-C4), semantic_change, affected_requirements, affected_contract_anchors, directly_affected_tasks, dependency_affected_tasks, unaffected_tasks, directly_affected_artifacts (when an artifact pipeline is configured), dependency_affected_artifacts, machine_resolvable, human_decision_required, human_decision_requests, required_propagation, and a concise analysis_summary. Do not include chain-of-thought.

For propagation stages, emit only structured fields appropriate to the stage: `propagation_plan`, `candidate_artifacts`, `contract_revision_report`, `plan_revision_report`, `plan_graph`, or `task_rebase` as requested by the prompt. Candidate artifact content is evidence in CWO external state and must never be written to accepted project authority files by the Agent.

For generic artifact stages, emit an `artifact` object with the current artifact `id` and `kind`. Generation and patch stages must include `candidate_hash` and may include `candidate_content`; review stages must include a concise `review` object. CWO validates lifecycle and dependency state deterministically.

{render_outcome_contract(values['run_id'], values['stage'], values['project'], values['group'], values['task'])}
Do not include private chain-of-thought.'''


def _skill_for_stage(config: WorkflowConfig, stage: str, state: WorkflowState):
    if stage == Stage.AUTHORITY_CHANGE_ANALYSIS.value:
        for key, spec in config.skills.items():
            if "technical-specification-guide" in spec.path or "engineering-contract-spec" in spec.path:
                return spec
        for spec in config.skills.values():
            sibling = Path(spec.path).expanduser().resolve().parent / "engineering-contract-spec" / "SKILL.md"
            if sibling.is_file():
                from .models import SkillSpec
                return SkillSpec(str(sibling))
    if stage in {Stage.CONTRACT_REVISION.value, Stage.CONTRACT_REVISION_REVIEW.value}:
        for key, spec in config.skills.items():
            if "contract" in key.lower() and "implementation" not in key.lower():
                return spec
            if "engineering-contract-spec" in spec.path:
                return spec
        for spec in config.skills.values():
            sibling = Path(spec.path).expanduser().resolve().parent / "engineering-contract-spec" / "SKILL.md"
            if sibling.is_file():
                from .models import SkillSpec
                return SkillSpec(str(sibling))
        return None
    if stage in {Stage.PLAN_REVISION.value, Stage.PLAN_REVISION_REVIEW.value, Stage.PLAN_GRAPH_BUILD.value, Stage.CHANGE_PROPAGATION_PLANNING.value}:
        for key, spec in config.skills.items():
            if "planner" in key.lower() or "implementation-planner" in spec.path:
                return spec
        return None
    if stage in {Stage.ARTIFACT_GENERATION.value, Stage.ARTIFACT_REVIEW.value, Stage.ARTIFACT_PATCH.value}:
        artifact = next((item for item in config.artifact_pipeline if item.id == state.current_artifact_id), None)
        if artifact and artifact.skill_role and artifact.skill_role in config.skills:
            return config.skills[artifact.skill_role]
        return config.skills.get("artifact") or config.skills.get("engineering-spec")
    task = config.task_at(state.current_group, state.current_task)
    if task and task.skill_role and task.skill_role in config.skills:
        return config.skills[task.skill_role]
    return config.skills.get("coding")


def _previous_issues(state: WorkflowState) -> str:
    if not state.last_outcome:
        return "none"
    issues = state.last_outcome.get("issues", [])
    return "; ".join(str(item.get("message", "")) for item in issues if isinstance(item, dict)) or str(state.last_outcome.get("summary", "none"))
