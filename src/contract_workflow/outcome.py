from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Verdict


OUTCOME_SCHEMA_VERSION = "1.0"
OUTCOME_REQUIRED_FIELDS = (
    "schema_version", "run_id", "stage", "verdict", "blocking", "project", "group", "task",
    "issues", "changed_files", "tests", "next_action", "summary",
)
ISSUE_REQUIRED_FIELDS = (
    "type", "severity", "requirement_ids", "message", "blocking", "recommended_stage",
)

# This is the single machine contract used by both validation and prompt
# generation. Additional issue fields remain allowed for useful evidence.
OUTCOME_SCHEMA = {
    "type": "object",
    "required": OUTCOME_REQUIRED_FIELDS,
    "properties": {
        "schema_version": {"const": OUTCOME_SCHEMA_VERSION},
        "run_id": {"type": "string"},
        "stage": {"type": "string"},
        "verdict": {"type": "string"},
        "blocking": {"type": "boolean"},
        "project": {"type": "string"},
        "group": {},
        "task": {},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ISSUE_REQUIRED_FIELDS,
                "properties": {
                    "type": {"type": "string"},
                    "severity": {"type": "string"},
                    "requirement_ids": {"type": "array", "items": {"type": "string"}},
                    "message": {"type": "string"},
                    "blocking": {"type": "boolean"},
                    "recommended_stage": {"type": "string"},
                },
            },
        },
        "changed_files": {"type": "array"},
        "tests": {"type": "array"},
        "next_action": {"type": "string"},
        "summary": {"type": "string"},
    },
}


def validate_outcome(path: Path, run_id: str, stage: str) -> tuple[bool, dict[str, Any] | None, list[str]]:
    if not path.is_file():
        return False, None, ["outcome.json is missing"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, None, [f"malformed outcome JSON: {exc}"]
    if not isinstance(value, dict):
        return False, None, ["outcome root must be an object"]
    errors = [f"missing field: {name}" for name in sorted(set(OUTCOME_REQUIRED_FIELDS) - set(value))]
    if value.get("schema_version") != OUTCOME_SCHEMA_VERSION:
        errors.append("schema_version must be 1.0")
    if value.get("run_id") != run_id:
        errors.append("run_id mismatch")
    if value.get("stage") != stage:
        errors.append("stage mismatch")
    try:
        Verdict(value.get("verdict"))
    except (ValueError, TypeError):
        errors.append("unknown verdict")
    if not isinstance(value.get("blocking"), bool):
        errors.append("blocking must be boolean")
    for field_name in ("issues", "changed_files", "tests"):
        if not isinstance(value.get(field_name), list):
            errors.append(f"{field_name} must be an array")
    if not isinstance(value.get("project"), str) or not isinstance(value.get("summary"), str):
        errors.append("project and summary must be strings")
    for index, issue in enumerate(value.get("issues", []) if isinstance(value.get("issues"), list) else []):
        if not isinstance(issue, dict):
            errors.append(f"issues[{index}] must be an object")
            continue
        for field_name in ISSUE_REQUIRED_FIELDS:
            if field_name not in issue:
                errors.append(f"issues[{index}] missing field: {field_name}")
        if "type" in issue and not isinstance(issue["type"], str):
            errors.append(f"issues[{index}].type must be a string")
        if "severity" in issue and not isinstance(issue["severity"], str):
            errors.append(f"issues[{index}].severity must be a string")
        if "requirement_ids" in issue and (
            not isinstance(issue["requirement_ids"], list)
            or not all(isinstance(item, str) for item in issue["requirement_ids"])
        ):
            errors.append(f"issues[{index}].requirement_ids must be an array of strings")
        if "message" in issue and not isinstance(issue["message"], str):
            errors.append(f"issues[{index}].message must be a string")
        if "blocking" in issue and not isinstance(issue["blocking"], bool):
            errors.append(f"issues[{index}].blocking must be boolean")
        if "recommended_stage" in issue and not isinstance(issue["recommended_stage"], str):
            errors.append(f"issues[{index}].recommended_stage must be a string")
    return not errors, value, errors


def render_outcome_contract(run_id: str, stage: str, project: str, group: str = "", task: str = "") -> str:
    """Render the canonical outcome contract for an exact agent invocation."""
    example = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "run_id": run_id,
        "stage": stage,
        "verdict": "<valid verdict for this stage>",
        "blocking": False,
        "project": project,
        "group": group,
        "task": task,
        "issues": [],
        "changed_files": [],
        "tests": [],
        "next_action": "<next action>",
        "summary": "<concise summary>",
    }
    issue = {
        "type": "IMPLEMENTATION_DEFECT",
        "severity": "medium",
        "requirement_ids": ["REQ-..."],
        "message": "specific issue",
        "blocking": False,
        "recommended_stage": "TASK_PATCH",
    }
    return (
        "The JSON written to CWO_OUTCOME_PATH is the machine-readable authority; human-readable prose "
        "cannot substitute for it. The exact invocation values must be preserved: "
        f"run_id={json.dumps(run_id)}, stage={json.dumps(stage)}.\n\n"
        "Top-level outcome shape (an APPROVED outcome with no issues uses `issues: []`):\n"
        "```json\n"
        f"{json.dumps(example, indent=2, ensure_ascii=False)}\n"
        "```\n\n"
        "Every `issues[]` entry MUST contain all fields below; do not use `code` or `details` "
        "as replacements for the required fields:\n"
        "```json\n"
        f"{json.dumps(issue, indent=2, ensure_ascii=False)}\n"
        "```\n"
    )


def make_outcome(run_id: str, stage: str, project: str, verdict: str, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": OUTCOME_SCHEMA_VERSION, "run_id": run_id, "stage": stage, "verdict": verdict,
        "blocking": verdict in {item.value for item in Verdict if item not in {Verdict.APPROVED, Verdict.REQUIRES_PATCH, Verdict.COMPLETED}},
        "project": project, "group": extra.pop("group", None), "task": extra.pop("task", None),
        "issues": extra.pop("issues", []), "changed_files": extra.pop("changed_files", []),
        "tests": extra.pop("tests", []), "next_action": extra.pop("next_action", ""),
        "summary": extra.pop("summary", ""),
    }
    value.update(extra)
    return value
