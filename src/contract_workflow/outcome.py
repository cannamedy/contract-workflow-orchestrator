from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Verdict


REQUIRED_FIELDS = {"schema_version", "run_id", "stage", "verdict", "blocking", "project", "group", "task", "issues", "changed_files", "tests", "next_action", "summary"}


def validate_outcome(path: Path, run_id: str, stage: str) -> tuple[bool, dict[str, Any] | None, list[str]]:
    if not path.is_file():
        return False, None, ["outcome.json is missing"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, None, [f"malformed outcome JSON: {exc}"]
    if not isinstance(value, dict):
        return False, None, ["outcome root must be an object"]
    errors = [f"missing field: {name}" for name in sorted(REQUIRED_FIELDS - set(value))]
    if value.get("schema_version") != "1.0":
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
        for field_name in ("type", "severity", "requirement_ids", "message", "blocking", "recommended_stage"):
            if field_name not in issue:
                errors.append(f"issues[{index}] missing field: {field_name}")
    return not errors, value, errors


def make_outcome(run_id: str, stage: str, project: str, verdict: str, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "1.0", "run_id": run_id, "stage": stage, "verdict": verdict,
        "blocking": verdict in {item.value for item in Verdict if item not in {Verdict.APPROVED, Verdict.REQUIRES_PATCH, Verdict.COMPLETED}},
        "project": project, "group": extra.pop("group", None), "task": extra.pop("task", None),
        "issues": extra.pop("issues", []), "changed_files": extra.pop("changed_files", []),
        "tests": extra.pop("tests", []), "next_action": extra.pop("next_action", ""),
        "summary": extra.pop("summary", ""),
    }
    value.update(extra)
    return value
