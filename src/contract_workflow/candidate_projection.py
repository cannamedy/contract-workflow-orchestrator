from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CandidateProjectionFile:
    """One project-relative file in an exact artifact candidate projection."""

    path: str
    content: bytes
    sha256: str
    primary: bool = False
    expected_before_sha256: str | None = None


def _project_relative(project: Path, raw: str) -> str | None:
    path = Path(raw)
    if not path.parts or ".." in path.parts or any(part in {".git", ".contract-workflow"} for part in path.parts):
        return None
    try:
        resolved = path.resolve() if path.is_absolute() else (project / path).resolve()
        path = resolved.relative_to(project.resolve())
    except (OSError, ValueError):
        return None
    return path.as_posix()


def candidate_projection(
    project: Path,
    candidate: Path,
    accepted_path: str,
    *,
    previous_accepted_sha256: str | None = None,
) -> tuple[list[CandidateProjectionFile], list[str]]:
    """Return the exact single-file or linked-file candidate projection.

    A JSON candidate opts into linked-file semantics by declaring
    ``candidate_files``. Linked files are confined to the configured accepted
    artifact's directory tree. Existing linked targets require an explicit
    baseline hash, supplied either on the file entry or by a matching
    ``components[].accepted_sha256`` record; entries without a baseline are
    creation-only.
    """

    errors: list[str] = []
    project = project.resolve()
    primary_path = _project_relative(project, accepted_path)
    if primary_path is None:
        return [], [f"unsafe artifact accepted_path: {accepted_path}"]
    try:
        primary_content = candidate.read_bytes()
    except OSError as exc:
        return [], [f"candidate artifact is unreadable: {exc}"]
    projection = [
        CandidateProjectionFile(
            primary_path,
            primary_content,
            hashlib.sha256(primary_content).hexdigest(),
            primary=True,
            expected_before_sha256=previous_accepted_sha256,
        )
    ]
    try:
        document = json.loads(primary_content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return projection, []
    if not isinstance(document, dict) or "candidate_files" not in document:
        return projection, []
    raw_files = document.get("candidate_files")
    if not isinstance(raw_files, list) or not raw_files:
        return [], ["candidate_files must be a non-empty array"]

    accepted_by_path: dict[str, str] = {}
    raw_components = document.get("components", [])
    if isinstance(raw_components, list):
        for item in raw_components:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            accepted_sha = item.get("accepted_sha256")
            if isinstance(accepted_sha, str):
                accepted_by_path[item["path"]] = accepted_sha.lower()

    primary_parent = Path(primary_path).parent
    seen = {primary_path}
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            errors.append(f"candidate_files[{index}] must be an object")
            continue
        raw_path = item.get("path")
        content = item.get("content")
        declared_sha = item.get("sha256")
        if not isinstance(raw_path, str):
            errors.append(f"candidate_files[{index}].path must be a string")
            continue
        relative = _project_relative(project, raw_path)
        if relative is None:
            errors.append(f"candidate_files[{index}].path is unsafe: {raw_path}")
            continue
        try:
            Path(relative).relative_to(primary_parent)
        except ValueError:
            errors.append(f"candidate_files[{index}].path escapes accepted artifact directory: {raw_path}")
            continue
        if relative in seen:
            errors.append(f"candidate_files contains duplicate projection path: {relative}")
            continue
        seen.add(relative)
        if not isinstance(content, str):
            errors.append(f"candidate_files[{index}].content must be a string")
            continue
        encoded = content.encode("utf-8")
        actual_sha = hashlib.sha256(encoded).hexdigest()
        if not isinstance(declared_sha, str) or declared_sha.lower() != actual_sha:
            errors.append(f"candidate_files[{index}].sha256 does not match content: {relative}")
            continue
        expected_before = item.get("accepted_sha256")
        if expected_before is None:
            expected_before = accepted_by_path.get(raw_path)
        if expected_before is not None and (
            not isinstance(expected_before, str)
            or len(expected_before) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in expected_before)
        ):
            errors.append(f"candidate_files[{index}].accepted_sha256 is invalid: {relative}")
            continue
        projection.append(
            CandidateProjectionFile(
                relative,
                encoded,
                actual_sha,
                expected_before_sha256=expected_before.lower() if isinstance(expected_before, str) else None,
            )
        )
    return (projection if not errors else []), errors


def projection_target_errors(project: Path, projection: list[CandidateProjectionFile]) -> list[str]:
    """Reject linked-target drift before validation or promotion."""

    errors: list[str] = []
    for item in projection:
        if item.primary:
            continue
        target = project / item.path
        actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        if actual == item.sha256:
            continue
        if item.expected_before_sha256 is not None:
            if actual != item.expected_before_sha256:
                errors.append(
                    f"linked candidate target drifted: {item.path} expected {item.expected_before_sha256} but found {actual or 'missing'}"
                )
        elif actual is not None:
            errors.append(f"linked candidate creation target already exists without an accepted baseline: {item.path}")
    return errors


def projection_evidence(projection: list[CandidateProjectionFile]) -> list[dict[str, Any]]:
    return [
        {
            "path": item.path,
            "sha256": item.sha256,
            "primary": item.primary,
            "expected_before_sha256": item.expected_before_sha256,
        }
        for item in projection
    ]
