from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from typing import Any, Iterable

from .models import REVIEW_FINDING_PROVENANCES, REVIEW_FINDING_STATUSES, ReviewFinding, WorkflowState, now_iso


FINDING_ID_RE = re.compile(r"^RF-[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


class ReviewFindingError(ValueError):
    """Deterministic validation or identity conflict in review evidence."""


def finding_identity(project: str, work_item_id: str, task_id: str | None, text: str) -> str:
    payload = {
        "project": project,
        "work_item_id": work_item_id,
        "task_id": task_id,
        "text": text,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"RF-{digest[:24].upper()}"


def validate_finding_identity(finding_id: str) -> None:
    if not isinstance(finding_id, str) or not FINDING_ID_RE.fullmatch(finding_id):
        raise ReviewFindingError("finding_id must match RF-<stable identifier>")


def validate_finding_payload(
    *,
    project_path: str,
    known_work_items: Iterable[str],
    finding_id: str,
    work_item_id: str,
    task_id: str | None,
    text: str,
    status: str,
    provenance: str,
) -> None:
    if not project_path:
        raise ReviewFindingError("target project is required")
    if not isinstance(work_item_id, str) or not work_item_id.strip():
        raise ReviewFindingError("work_item_id is required")
    if work_item_id not in set(known_work_items):
        raise ReviewFindingError(f"unknown work item/task: {work_item_id}")
    if task_id is not None and task_id != work_item_id:
        raise ReviewFindingError("task_id must match work_item_id when supplied")
    if not isinstance(text, str) or not text.strip():
        raise ReviewFindingError("finding text must be non-empty")
    validate_finding_identity(finding_id)
    if status not in REVIEW_FINDING_STATUSES:
        raise ReviewFindingError(f"unsupported review finding status: {status}")
    if provenance not in REVIEW_FINDING_PROVENANCES:
        raise ReviewFindingError(f"unsupported review finding provenance: {provenance}")


def unresolved_for_task(state: WorkflowState, task_id: str) -> list[ReviewFinding]:
    return sorted(
        (
            finding for finding in state.review_findings.values()
            if finding.task_id == task_id and finding.status == "UNRESOLVED"
        ),
        key=lambda finding: finding.finding_id,
    )


def register_finding(state: WorkflowState, finding: ReviewFinding) -> tuple[WorkflowState, str, ReviewFinding]:
    validate_finding_identity(finding.finding_id)
    existing = state.review_findings.get(finding.finding_id)
    if existing is not None:
        immutable = (existing.project, existing.work_item_id, existing.task_id, existing.text, existing.provenance)
        incoming = (finding.project, finding.work_item_id, finding.task_id, finding.text, finding.provenance)
        if immutable != incoming:
            raise ReviewFindingError(f"REVIEW_FINDING_CONFLICT: {finding.finding_id} already exists with different content")
        return state, "ALREADY_REGISTERED", existing
    findings = {**state.review_findings, finding.finding_id: finding}
    return replace(state, review_findings=findings), "CREATED", finding


def resolve_finding(state: WorkflowState, finding_id: str, evidence: dict[str, Any]) -> tuple[WorkflowState, ReviewFinding]:
    validate_finding_identity(finding_id)
    try:
        finding = state.review_findings[finding_id]
    except KeyError as exc:
        raise ReviewFindingError(f"review finding not found: {finding_id}") from exc
    if finding.status != "UNRESOLVED":
        raise ReviewFindingError(f"review finding is not unresolved: {finding_id}")
    if not evidence:
        raise ReviewFindingError("resolution evidence is required")
    resolved = replace(finding, status="RESOLVED", resolved_at=now_iso(), resolution_evidence=dict(evidence))
    return replace(state, review_findings={**state.review_findings, finding_id: resolved}), resolved
