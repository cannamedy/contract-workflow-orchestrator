from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .models import AuthoritativeSource, WorkflowConfig


class GitClassification(str, Enum):
    EXPECTED_TARGET_ARTIFACT = "EXPECTED_TARGET_ARTIFACT"
    AUTHORIZED_MUTABLE_TRACKER = "AUTHORIZED_MUTABLE_TRACKER"
    FROZEN_AUTHORITY_CHANGE = "FROZEN_AUTHORITY_CHANGE"
    UNEXPECTED_RELATED_CHANGE = "UNEXPECTED_RELATED_CHANGE"
    UNEXPECTED_UNRELATED_CHANGE = "UNEXPECTED_UNRELATED_CHANGE"
    MERGE_CONFLICT = "MERGE_CONFLICT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GitChange:
    path: str
    status: str
    classification: GitClassification


@dataclass(frozen=True)
class GitAudit:
    is_repository: bool
    changes: tuple[GitChange, ...]
    classifications: tuple[GitClassification, ...]
    blocking: bool
    error: str | None = None


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(project), *args], text=True, capture_output=True, check=False)


def _source_paths(project: Path, sources: tuple[AuthoritativeSource, ...]) -> set[str]:
    result: set[str] = set()
    for source in sources:
        path = Path(source.path)
        result.add(str(path.resolve() if path.is_absolute() else (project / path).resolve()))
    return result


def _matches(path: str, patterns: tuple[str, ...], project: Path) -> bool:
    relative = str(Path(path).resolve().relative_to(project.resolve())) if Path(path).resolve().is_relative_to(project.resolve()) else path
    return any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path, pattern) for pattern in patterns)


def audit_git(project: Path, config: WorkflowConfig) -> GitAudit:
    root = _git(project, "rev-parse", "--show-toplevel")
    if root.returncode != 0:
        return GitAudit(False, (), (GitClassification.UNKNOWN,), True, root.stderr.strip() or "not a git repository")
    result = _git(project, "status", "--porcelain=v1", "-uall")
    if result.returncode != 0:
        return GitAudit(True, (), (GitClassification.UNKNOWN,), True, result.stderr.strip())
    frozen = _source_paths(project, tuple(source for source in config.authoritative_sources if not source.mutable_after_start))
    expected: list[str] = []
    for _, task in config.tasks:
        expected.extend(task.expected_outputs)
        expected.extend(task.allowed_paths)
    changes: list[GitChange] = []
    for line in result.stdout.splitlines():
        if len(line) < 3:
            continue
        status, raw_path = line[:2], line[3:]
        path = raw_path.split(" -> ")[-1]
        absolute = str((project / path).resolve())
        if "U" in status or status in {"AA", "DD"}:
            kind = GitClassification.MERGE_CONFLICT
        elif absolute in frozen:
            kind = GitClassification.FROZEN_AUTHORITY_CHANGE
        elif path == ".contract-workflow/workflow.yaml" or path.startswith(".contract-workflow/"):
            kind = GitClassification.AUTHORIZED_MUTABLE_TRACKER
        elif _matches(path, tuple(expected), project):
            kind = GitClassification.EXPECTED_TARGET_ARTIFACT
        elif any(part in path.lower() for part in ("plan", "task", "traceability", "contract")):
            kind = GitClassification.UNEXPECTED_RELATED_CHANGE
        else:
            kind = GitClassification.UNEXPECTED_UNRELATED_CHANGE
        changes.append(GitChange(path, status, kind))
    classes = tuple(sorted({change.classification for change in changes}, key=lambda item: item.value)) or (GitClassification.UNKNOWN,)
    blocking = any(item in {GitClassification.FROZEN_AUTHORITY_CHANGE, GitClassification.MERGE_CONFLICT, GitClassification.UNEXPECTED_UNRELATED_CHANGE} for item in classes)
    return GitAudit(True, tuple(changes), classes, blocking)


def source_integrity(project: Path, sources: tuple[AuthoritativeSource, ...]) -> list[str]:
    errors: list[str] = []
    for source in sources:
        path = Path(source.path)
        path = path if path.is_absolute() else project / path
        if not path.is_file():
            errors.append(f"authoritative source missing: {path}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not source.mutable_after_start and digest.lower() != source.sha256.lower():
            errors.append(f"FROZEN_SOURCE_MISMATCH: {path}")
        if source.git_commit:
            check = _git(project, "rev-parse", source.git_commit)
            if check.returncode != 0:
                errors.append(f"git commit not found: {source.git_commit}")
            else:
                head = _git(project, "rev-parse", "HEAD")
                if head.returncode == 0 and head.stdout.strip() != check.stdout.strip():
                    errors.append(f"FROZEN_SOURCE_MISMATCH: HEAD is not configured commit {source.git_commit}")
        if source.git_tag:
            check = _git(project, "rev-parse", "refs/tags/" + source.git_tag)
            if check.returncode != 0:
                errors.append(f"git tag not found: {source.git_tag}")
    return errors
