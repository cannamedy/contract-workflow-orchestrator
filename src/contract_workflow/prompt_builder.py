from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import Stage, WorkflowConfig, WorkflowState


DEFAULT_TEMPLATES = {
    "TASK_EXECUTION": "task-execution.md",
    "TASK_INDEPENDENT_REVIEW": "task-review.md",
    "TASK_PATCH": "task-patch.md",
    "PLAN_DEFECT_RESOLUTION": "plan-defect-resolution.md",
    "PLAN_REVISION_REVIEW": "plan-revision-review.md",
    "FINAL_VERIFICATION": "task-review.md",
}
DEFAULT_TEXTS = {
    "TASK_EXECUTION": "# Task Execution\n\nWork only on the current task using the configured Skill.",
    "TASK_INDEPENDENT_REVIEW": "# Independent Review\n\nReview the current task independently using the configured Skill.",
    "TASK_PATCH": "# Task Patch\n\nAddress only the current review issues.",
    "PLAN_DEFECT_RESOLUTION": "# Plan Defect Resolution\n\nResolve only the reported planning defect.",
    "PLAN_REVISION_REVIEW": "# Plan Revision Review\n\nReview the revised plan against authoritative artifacts.",
    "FINAL_VERIFICATION": "# Final Verification\n\nVerify the completed workflow against its authoritative artifacts.",
}
STAGE_VERDICT_GUIDANCE = {
    "TASK_EXECUTION": "Use APPROVED when the requested implementation and tests are complete; COMPLETED is not valid for this stage.",
    "TASK_INDEPENDENT_REVIEW": "Use APPROVED when the current artifacts satisfy the Contract, or REQUIRES_PATCH when the implementation has a defect.",
    "TASK_PATCH": "Use APPROVED when the requested patch is complete and verified; report a structured blocking verdict when it cannot safely proceed.",
    "PLAN_DEFECT_RESOLUTION": "Use APPROVED when the planning defect is resolved; report a structured blocking verdict when it cannot safely proceed.",
    "PLAN_REVISION_REVIEW": "Use APPROVED when the revised plan is correct, or REQUIRES_PATCH when it still has a defect.",
    "FINAL_VERIFICATION": "Use APPROVED or COMPLETED only when final verification succeeds.",
}


class PromptBuilder:
    def __init__(self, template_dir: Path | None = None):
        self.template_dir = template_dir or Path(__file__).resolve().parents[2] / "templates"

    def build(self, config: WorkflowConfig, state: WorkflowState, outcome_path: Path) -> str:
        stage = state.current_stage
        template_name = DEFAULT_TEMPLATES.get(stage)
        template = DEFAULT_TEXTS.get(stage, "")
        if template_name:
            path = self.template_dir / template_name
            if path.is_file():
                template = path.read_text(encoding="utf-8")
        skill = config.skills.get("planner" if "PLAN" in stage else "coding")
        frozen = "\n".join(f"- {item.path} sha256={item.sha256} commit={item.git_commit or '-'} tag={item.git_tag or '-'}" for item in config.authoritative_sources) or "- none declared"
        task = config.task_at(state.current_group, state.current_task)
        task_paths = []
        if task:
            task_paths.extend(task.expected_outputs)
            task_paths.extend(path for path in task.allowed_paths if path not in task_paths)
        allowed = "- .contract-workflow runtime tracker (orchestrator-owned)"
        if task_paths:
            allowed += "\n" + "\n".join(f"- {path}" for path in task_paths)
        else:
            allowed += "\n- no task-specific mutable paths declared"
        previous_issues = _previous_issues(state)
        if stage == "TASK_INDEPENDENT_REVIEW":
            previous_issues = "not provided to preserve review independence; inspect the current repository and frozen authority directly"
        values = {
            "project_path": config.project_path, "project": config.project_name, "stage": stage,
            "group": state.current_group or "", "task": state.current_task or "",
            "skill_path": skill.path if skill else "(no Skill configured)", "frozen_authority": frozen,
            "outcome_path": str(outcome_path), "allowed_scope": allowed,
            "hard_stops": ", ".join(config.hard_stops) or "OPEN_CONTRACT_ISSUE, ARCHITECTURE_DECISION_REQUIRED, SECURITY_SENSITIVE_ACTION, DESTRUCTIVE_ACTION_REQUIRED, FROZEN_SOURCE_MISMATCH",
            "attempt": str(state.attempt), "previous_issue_summary": previous_issues,
            "verdict_guidance": STAGE_VERDICT_GUIDANCE.get(stage, "Use only a verdict valid for the current stage."),
        }
        try:
            body = template.format(**values)
        except (KeyError, ValueError):
            body = template
        context = f"PROJECT: {config.project_name}\nPROJECT PATH: {config.project_path}\nCURRENT STAGE: {stage}\nCURRENT GROUP: {state.current_group or ''}\nCURRENT TASK: {state.current_task or ''}\nRUN ID: {state.run_id or outcome_path.parent.name}"
        return context + "\n\n" + body.rstrip() + "\n\n" + self._contract(values)

    @staticmethod
    def _contract(values: dict[str, str]) -> str:
        return f'''## ORCHESTRATOR OUTPUT CONTRACT

Re-read the relevant `SKILL.md` at `{values['skill_path']}` before acting. Execute only the current stage `{values['stage']}` for project `{values['project']}` at `{values['project_path']}`. Respect the frozen authority metadata below and the allowed scope. Write a human-readable report, then write valid machine-readable JSON exactly to `{values['outcome_path']}`. Do not modify orchestrator workflow state or infer transitions from prose.

Frozen authority:
{values['frozen_authority']}

Attempt: {values['attempt']}
Allowed scope:
{values['allowed_scope']}

Hard stops: {values['hard_stops']}
Stage verdict guidance: {values['verdict_guidance']}
Previous issue summary: {values['previous_issue_summary']}

The outcome must contain `schema_version`, `run_id`, `stage`, `verdict`, `blocking`, `project`, `group`, `task`, `issues`, `changed_files`, `tests`, `next_action`, and `summary`. Do not include private chain-of-thought.'''


def _previous_issues(state: WorkflowState) -> str:
    if not state.last_outcome:
        return "none"
    issues = state.last_outcome.get("issues", [])
    return "; ".join(str(item.get("message", "")) for item in issues if isinstance(item, dict)) or str(state.last_outcome.get("summary", "none"))
