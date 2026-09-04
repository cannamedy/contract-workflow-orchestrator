from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .authority_set import aggregate_authority_set_hash, authority_set_revision_id, canonical_member_records, member_change_sets, validate_member_specs
from .models import AuthorityMemberSpec, AuthoritativeSource, DecisionStatus, HumanDecision, WorkflowConfig, WorkflowState


REMOTE_CHECK_FAILED = "REMOTE_CHECK_FAILED"
REMOTE_AUTHORITY_SOURCE_MISSING = "REMOTE_AUTHORITY_SOURCE_MISSING"
NEWER_REMOTE_REVISION_AVAILABLE = "NEWER_REMOTE_REVISION_AVAILABLE"
NEWER_HUMAN_AUTHORITY_SUBMISSION = "NEWER_HUMAN_AUTHORITY_SUBMISSION"
CANDIDATE_REVISION_SUPERSEDED = "CANDIDATE_REVISION_SUPERSEDED"


@dataclass(frozen=True)
class RemoteAuthoritySnapshot:
    remote_url: str
    branch: str
    commit_sha: str
    authority_path: str
    git_blob_sha: str | None
    content_sha256: str | None
    snapshot_path: str | None
    observed_at: str
    status: str = "OK"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class RemoteAuthoritySetSnapshot:
    remote_url: str
    branch: str
    commit_sha: str
    members: tuple[dict[str, Any], ...]
    aggregate_hash: str
    manifest_path: str | None
    observed_at: str
    status: str = "OK"

    def to_dict(self) -> dict[str, Any]:
        value = dict(self.__dict__)
        value["members"] = [dict(item) for item in self.members]
        return value


@dataclass(frozen=True)
class RemoteCheck:
    snapshot: RemoteAuthoritySnapshot | None
    changed: bool = False
    new_change: dict[str, Any] | None = None
    rollover: dict[str, Any] | None = None
    errors: tuple[str, ...] = ()
    status: str = "NO_CHANGE"
    authority_set: RemoteAuthoritySetSnapshot | None = None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "--git-dir", str(repo), *args], text=True, capture_output=True, check=False)


def _project_git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(project), *args], text=True, capture_output=True, check=False)


def _remote_url(project: Path, remote: str) -> tuple[str | None, str | None]:
    if re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://|git@|/|\./|\.\./)", remote):
        return remote, None
    result = _project_git(project, "remote", "get-url", remote)
    if result.returncode:
        return None, f"{REMOTE_CHECK_FAILED}: cannot resolve git remote {remote}: {result.stderr.strip()}"
    return result.stdout.strip(), None


def _source(config: WorkflowConfig) -> AuthoritativeSource | None:
    for item in config.authoritative_sources:
        role = (item.role or "").upper()
        if role == "HUMAN_GUIDE" or (not role and (item.source_id == "human-guide" or "human" in item.path.lower() or "架构原理" in item.path)):
            return item
    return None


def _safe_source_path(path: str) -> bool:
    value = Path(path)
    return not value.is_absolute() and ".." not in value.parts


def _tree_entry(cache: Path, commit: str, path: str) -> tuple[str | None, str | None]:
    if not _safe_source_path(path):
        return None, f"{REMOTE_AUTHORITY_SOURCE_MISSING}: unsafe authority path {path}"
    result = _git(cache, "ls-tree", "-z", commit, "--", path)
    if result.returncode:
        return None, f"{REMOTE_CHECK_FAILED}: cannot inspect remote tree: {result.stderr.strip()}"
    raw = result.stdout.encode("utf-8", "surrogateescape")
    record = raw.split(b"\0", 1)[0]
    if not record:
        return None, None
    match = re.match(rb"\d+ blob ([0-9a-f]{40})\t", record)
    if not match:
        return None, f"{REMOTE_AUTHORITY_SOURCE_MISSING}: configured authority path is not a file: {path}"
    return match.group(1).decode(), None


def _snapshot_content(cache: Path, commit: str, path: str) -> bytes | None:
    result = _git(cache, "show", f"{commit}:{path}")
    return result.stdout.encode("utf-8", "surrogateescape") if result.returncode == 0 else None


def _snapshot_file(store: Any, commit: str, source: AuthoritativeSource, content: bytes) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", source.source_id or "human-guide")
    directory = store.remote_snapshots_path / commit
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{safe}.md"
    store._atomic_write(path, content.decode("utf-8", "surrogateescape"))
    return path


def _revision_id(commit: str, blob: str | None, content_sha: str | None) -> str:
    """Return a stable, storage-safe identity for one submitted revision."""
    value = f"{commit}:{blob or ''}:{content_sha or ''}"
    return f"REV-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16].upper()}"


def _candidate_revision(snapshot: RemoteAuthoritySnapshot, *, status: str, supersedes: str | None = None) -> dict[str, Any]:
    return {
        "revision_id": _revision_id(snapshot.commit_sha, snapshot.git_blob_sha, snapshot.content_sha256),
        "remote_commit": snapshot.commit_sha,
        "git_blob_sha": snapshot.git_blob_sha,
        "content_sha256": snapshot.content_sha256,
        "snapshot_path": snapshot.snapshot_path,
        "observed_at": snapshot.observed_at,
        "status": status,
        "supersedes": supersedes,
        "superseded_by": None,
        "superseded_reason": None,
    }


def _historical_candidate_revision(change: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any] | None:
    content_sha = change.get("candidate_sha256") or entry.get("candidate_content_sha256") or entry.get("candidate_sha256")
    commit = change.get("candidate_commit") or entry.get("candidate_remote_commit")
    blob = change.get("candidate_blob_sha") or entry.get("candidate_remote_blob") or entry.get("candidate_authority_blob")
    snapshot_path = change.get("candidate_snapshot_path") or entry.get("candidate_snapshot_path")
    if not all(isinstance(item, str) and item for item in (content_sha, commit, blob, snapshot_path)):
        return None
    revision_id = _revision_id(commit, blob, content_sha)
    analysis_keys = (
        "classification", "semantic_change", "affected_requirements", "affected_contract_anchors",
        "directly_affected_tasks", "dependency_affected_tasks", "unaffected_tasks",
        "machine_resolvable", "human_decision_required", "human_decision_requests",
        "required_propagation", "affected_artifacts", "directly_affected_artifacts",
        "dependency_affected_artifacts", "analysis_summary", "analyzed_at",
    )
    analysis = {key: change[key] for key in analysis_keys if key in change}
    revision: dict[str, Any] = {
        "revision_id": revision_id,
        "remote_commit": commit,
        "git_blob_sha": blob,
        "content_sha256": content_sha,
        "snapshot_path": snapshot_path,
        "observed_at": change.get("detected_at"),
        "status": "ACTIVE",
        "supersedes": None,
        "superseded_by": None,
        "superseded_reason": None,
    }
    if analysis:
        revision["analysis_evidence"] = analysis
    if change.get("review_evidence") is not None:
        revision["review_evidence"] = change["review_evidence"]
    if change.get("review_status") is not None:
        revision["review_status"] = change["review_status"]
    return revision


def _decision_candidate_hash(decision: HumanDecision) -> str | None:
    if decision.source_candidate_hash:
        return decision.source_candidate_hash
    match = re.search(r"(?:revision|SHA256)\s*[=:]?\s*([0-9a-f]{64})", f"{decision.question} {decision.context}", re.IGNORECASE)
    return match.group(1).lower() if match else None


def _supersede_candidate_decisions(store: Any, state: WorkflowState | None, change_id: str, old_hash: str, new_decision_id: str) -> list[str]:
    """Supersede only pending Decisions bound to the replaced candidate."""
    decisions: dict[str, HumanDecision] = {}
    if state is not None:
        decisions.update(state.decisions)
    if store.decisions_path.exists():
        for path in store.decisions_path.glob("*.json"):
            try:
                value = __import__("json").loads(path.read_text(encoding="utf-8"))
                decision = HumanDecision.from_dict(value)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            decisions[decision.decision_id] = decision
    superseded: list[str] = []
    for decision_id, decision in decisions.items():
        if decision.status != DecisionStatus.PENDING.value or decision.source_change != change_id:
            continue
        if (_decision_candidate_hash(decision) or "").lower() != old_hash.lower():
            continue
        updated = __import__("dataclasses").replace(
            decision,
            status=DecisionStatus.SUPERSEDED.value,
            superseded_by=new_decision_id,
            superseded_reason=CANDIDATE_REVISION_SUPERSEDED,
        )
        store.save_decision(updated)
        if state is not None:
            state.decisions[decision_id] = updated
        superseded.append(decision_id)
    return sorted(set(superseded))


def _load_change(store: Any, change_id: str) -> dict[str, Any] | None:
    path = store.authority_changes_path / f"{change_id}.json"
    if not path.is_file():
        return None
    try:
        value = __import__("json").loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _rollover_pending_candidate(
    config: WorkflowConfig,
    source: AuthoritativeSource,
    entry: dict[str, Any],
    snapshot: RemoteAuthoritySnapshot,
    store: Any,
    state: WorkflowState | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Replace an unaccepted remote candidate without replacing its evidence."""
    change_id = entry.get("change_id")
    if not isinstance(change_id, str) or not change_id:
        return None, None
    change = _load_change(store, change_id)
    if not change:
        return None, None
    old_hash = str(change.get("candidate_sha256") or entry.get("candidate_content_sha256") or entry.get("candidate_sha256") or "")
    accepted_hash = str(entry.get("accepted_content_sha256") or entry.get("accepted_sha256") or source.sha256)
    if not old_hash or old_hash.lower() == str(snapshot.content_sha256 or "").lower():
        return change, None
    if old_hash.lower() == accepted_hash.lower() or str(entry.get("status")) == "ACCEPTED":
        # An accepted candidate starts a new AuthorityChange; it is never
        # rolled over inside the already accepted change.
        return None, None
    if str(change.get("source_path")) not in {source.path, str(change.get("configured_source_path"))} and str(change.get("configured_source_path")) != source.path:
        return None, None
    if state is not None:
        for artifact in state.artifacts.values():
            if artifact.change_id == change_id and artifact.kind != "HUMAN_GUIDE" and artifact.status == "ACCEPTED" and artifact.accepted_hash:
                return None, None

    revisions = list(change.get("candidate_revisions") or [])
    old_revision = next((item for item in revisions if isinstance(item, dict) and str(item.get("content_sha256")).lower() == old_hash.lower()), None)
    if old_revision is None:
        old_revision = _historical_candidate_revision(change, entry)
    if old_revision is None:
        return None, None
    old_revision = {**old_revision, "status": "SUPERSEDED", "superseded_by": _revision_id(snapshot.commit_sha, snapshot.git_blob_sha, snapshot.content_sha256), "superseded_reason": NEWER_HUMAN_AUTHORITY_SUBMISSION}
    revisions = [item for item in revisions if not (isinstance(item, dict) and str(item.get("content_sha256")).lower() == old_hash.lower())]
    new_revision = _candidate_revision(snapshot, status="ACTIVE", supersedes=old_revision["revision_id"])
    revisions.extend([old_revision, new_revision])
    revision_number = len(revisions)
    new_decision_id = f"ADR-HUMAN-GUIDE-PROMOTION-{change_id}-{str(snapshot.content_sha256)[:12].upper()}"
    superseded_decisions = _supersede_candidate_decisions(store, state, change_id, old_hash, new_decision_id)
    updated = dict(change)
    updated.update({
        "candidate_sha256": snapshot.content_sha256,
        "candidate_commit": snapshot.commit_sha,
        "candidate_blob_sha": snapshot.git_blob_sha,
        "candidate_snapshot_path": snapshot.snapshot_path,
        "candidate_revision_id": new_revision["revision_id"],
        "candidate_revision_number": revision_number,
        "candidate_revisions": revisions,
        "classification": None,
        "semantic_change": None,
        "affected_requirements": [],
        "affected_contract_anchors": [],
        "directly_affected_tasks": [],
        "dependency_affected_tasks": [],
        "unaffected_tasks": [],
        "machine_resolvable": None,
        "human_decision_required": None,
        "human_decision_requests": [],
        "required_propagation": [],
        "affected_artifacts": [],
        "directly_affected_artifacts": [],
        "dependency_affected_artifacts": [],
        "analysis_summary": "",
        "status": "CHANGE_PENDING",
        "analysis_required": True,
        "analysis_revision_id": new_revision["revision_id"],
        "rollover": {
            "from_revision_id": old_revision["revision_id"],
            "to_revision_id": new_revision["revision_id"],
            "reason": NEWER_HUMAN_AUTHORITY_SUBMISSION,
            "superseded_decision_ids": superseded_decisions,
            "new_decision_id": new_decision_id,
            "rolled_over_at": snapshot.observed_at,
        },
    })
    store.save_authority_change(updated)
    rollover = {
        "change_id": change_id,
        "old_candidate_sha256": old_hash,
        "new_candidate_sha256": snapshot.content_sha256,
        "old_revision_id": old_revision["revision_id"],
        "new_revision_id": new_revision["revision_id"],
        "superseded_decision_ids": superseded_decisions,
        "new_decision_id": new_decision_id,
    }
    return updated, rollover


def _new_change(config: WorkflowConfig, source: AuthoritativeSource, entry: dict[str, Any], snapshot: RemoteAuthoritySnapshot, store: Any) -> dict[str, Any]:
    import uuid
    change_id = f"CR-{uuid.uuid4().hex[:8].upper()}"
    candidate_revision = _candidate_revision(snapshot, status="ACTIVE")
    value = {
        "schema_version": "1.0", "change_id": change_id, "source_id": source.source_id or "human-guide",
        "source_path": source.path, "configured_source_path": source.path, "source_role": source.role or "HUMAN_GUIDE",
        "authority_origin": "git-remote", "candidate_snapshot_path": snapshot.snapshot_path,
        "base_sha256": str(entry.get("accepted_content_sha256") or entry.get("accepted_sha256") or source.sha256),
        "candidate_sha256": snapshot.content_sha256, "base_commit": entry.get("accepted_remote_commit"),
        "candidate_commit": snapshot.commit_sha, "base_blob_sha": entry.get("accepted_remote_blob"),
        "candidate_blob_sha": snapshot.git_blob_sha, "detected_at": snapshot.observed_at,
        "classification": None, "semantic_change": None, "affected_requirements": [],
        "affected_contract_anchors": [], "directly_affected_tasks": [], "dependency_affected_tasks": [],
        "unaffected_tasks": [], "machine_resolvable": None, "human_decision_required": None,
        "human_decision_requests": [], "required_propagation": [], "analysis_summary": "", "status": "CHANGE_PENDING",
        "candidate_revision_id": candidate_revision["revision_id"], "candidate_revision_number": 1,
        "candidate_revisions": [candidate_revision],
    }
    store.save_authority_change(value)
    return value


def _authority_set_guide_source(config: WorkflowConfig) -> AuthoritativeSource | None:
    for source in config.authoritative_sources:
        if (source.role or "").upper() == "HUMAN_GUIDE" or source.source_id == "human-guide":
            return source
    member = next((item for item in config.authority_members if item.role == "ARCHITECTURE_GUIDE"), None)
    if member is None:
        return None
    return AuthoritativeSource(path=member.path, sha256="", source_id=member.id, role=member.role)


def _authority_set_member_snapshot(store: Any, commit: str, member: AuthorityMemberSpec, content: bytes) -> Path:
    directory = store.remote_snapshots_path / commit / "authority-members"
    directory.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", member.id)
    path = directory / f"{safe}.md"
    store._atomic_write(path, content.decode("utf-8", "surrogateescape"))
    return path


def _authority_set_manifest(store: Any, commit: str, payload: dict[str, Any], dry_run: bool) -> Path | None:
    if dry_run:
        return None
    path = store.remote_snapshots_path / commit / "authority-set.json"
    store._atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return path


def _authority_set_accepted_members(config: WorkflowConfig, ledger: dict[str, Any]) -> list[dict[str, Any]]:
    configured = ledger.get("authority_set", {})
    if isinstance(configured, dict) and isinstance(configured.get("accepted_members"), list):
        return canonical_member_records(configured["accepted_members"])
    guide = _authority_set_guide_source(config)
    entry = (ledger.get("sources", {}) or {}).get("human-guide", {})
    if guide is None or not isinstance(entry, dict):
        return []
    content_sha = entry.get("accepted_content_sha256") or entry.get("accepted_sha256") or guide.sha256
    return [{
        "member_id": next((item.id for item in config.authority_members if item.role == "ARCHITECTURE_GUIDE"), "architecture-guide"),
        "role": "ARCHITECTURE_GUIDE", "path": guide.path,
        "git_blob_sha": entry.get("accepted_remote_blob") or entry.get("accepted_authority_blob"),
        "content_sha256": content_sha,
        "snapshot_path": entry.get("accepted_snapshot_path"),
        "source_revision": entry.get("accepted_remote_commit"),
    }]


def _set_candidate_revision(snapshot: RemoteAuthoritySetSnapshot, *, status: str, supersedes: str | None = None) -> dict[str, Any]:
    return {
        "revision_id": authority_set_revision_id(snapshot.aggregate_hash),
        "authority_set_hash": snapshot.aggregate_hash,
        "members": [dict(item) for item in snapshot.members],
        "remote_commit": snapshot.commit_sha,
        "snapshot_path": snapshot.manifest_path,
        "observed_at": snapshot.observed_at,
        "status": status,
        "supersedes": supersedes,
        "superseded_by": None,
        "superseded_reason": None,
    }


def _rollover_pending_authority_set(
    config: WorkflowConfig,
    guide: AuthoritativeSource,
    ledger_entry: dict[str, Any],
    snapshot: RemoteAuthoritySetSnapshot,
    store: Any,
    state: WorkflowState | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    change_id = ledger_entry.get("change_id")
    if not isinstance(change_id, str) or not change_id:
        return None, None
    change = _load_change(store, change_id)
    if not change:
        return None, None
    if str(ledger_entry.get("status")) == "ACCEPTED":
        return None, None
    old_set_hash = str(ledger_entry.get("candidate_authority_set_hash") or "")
    old_hash = str(change.get("candidate_sha256") or ledger_entry.get("candidate_content_sha256") or "")
    if old_set_hash and old_set_hash.lower() == snapshot.aggregate_hash.lower():
        return change, None
    if not old_set_hash and old_hash and old_hash.lower() == snapshot.aggregate_hash.lower():
        return change, None
    revisions = list(change.get("candidate_revisions") or [])
    old_revision = None
    if old_set_hash:
        old_revision = next((item for item in revisions if isinstance(item, dict) and str(item.get("authority_set_hash", "")).lower() == old_set_hash.lower()), None)
    if old_revision is None and old_hash:
        old_revision = next((item for item in revisions if isinstance(item, dict) and str(item.get("content_sha256", "")).lower() == old_hash.lower()), None)
    if old_revision is None:
        old_revision = _historical_candidate_revision(change, ledger_entry)
    if old_revision is None:
        return None, None
    new_revision = _set_candidate_revision(snapshot, status="ACTIVE", supersedes=old_revision.get("revision_id"))
    old_revision = {
        **old_revision,
        "status": "SUPERSEDED",
        "superseded_by": new_revision["revision_id"],
        "superseded_reason": NEWER_HUMAN_AUTHORITY_SUBMISSION,
    }
    def is_old_revision(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        if old_set_hash:
            return str(item.get("authority_set_hash", "")).lower() == old_set_hash.lower()
        return str(item.get("content_sha256", "")).lower() == old_hash.lower()

    revisions = [item for item in revisions if not is_old_revision(item)]
    revisions.extend([old_revision, new_revision])
    new_decision_id = f"ADR-AUTHORITY-SET-PROMOTION-{change_id}-{snapshot.aggregate_hash[:12].upper()}"
    superseded_decisions = _supersede_candidate_decisions(store, state, change_id, old_hash or old_set_hash, new_decision_id)
    updated = {
        **change,
        "source_role": "HUMAN_AUTHORITY_SET",
        "candidate_sha256": next((item.get("content_sha256") for item in snapshot.members if item.get("role") == "ARCHITECTURE_GUIDE"), change.get("candidate_sha256")),
        "candidate_commit": snapshot.commit_sha,
        "candidate_blob_sha": next((item.get("git_blob_sha") for item in snapshot.members if item.get("role") == "ARCHITECTURE_GUIDE"), None),
        "candidate_snapshot_path": next((item.get("snapshot_path") for item in snapshot.members if item.get("role") == "ARCHITECTURE_GUIDE"), None),
        "candidate_authority_set_snapshot_path": snapshot.manifest_path,
        "candidate_authority_set_hash": snapshot.aggregate_hash,
        "authority_set_hash": snapshot.aggregate_hash,
        "authority_set_members": [dict(item) for item in snapshot.members],
        "authority_set_member_changes": member_change_sets(_authority_set_accepted_members(config, store.load_authority_ledger() or {}), snapshot.members),
        "candidate_revision_id": new_revision["revision_id"],
        "candidate_revision_number": len(revisions),
        "candidate_revisions": revisions,
        "classification": None, "semantic_change": None, "affected_requirements": [],
        "affected_contract_anchors": [], "directly_affected_tasks": [], "dependency_affected_tasks": [],
        "unaffected_tasks": [], "machine_resolvable": None, "human_decision_required": None,
        "human_decision_requests": [], "required_propagation": [], "affected_artifacts": [],
        "directly_affected_artifacts": [], "dependency_affected_artifacts": [], "analysis_summary": "",
        "status": "CHANGE_PENDING", "analysis_required": True,
        "analysis_revision_id": new_revision["revision_id"],
        "rollover": {
            "from_revision_id": old_revision.get("revision_id"), "to_revision_id": new_revision["revision_id"],
            "reason": NEWER_HUMAN_AUTHORITY_SUBMISSION, "superseded_decision_ids": superseded_decisions,
            "new_decision_id": new_decision_id, "rolled_over_at": snapshot.observed_at,
        },
    }
    store.save_authority_change(updated)
    return updated, {
        "change_id": change_id, "old_candidate_sha256": old_hash,
        "new_candidate_sha256": updated["candidate_sha256"], "old_revision_id": old_revision.get("revision_id"),
        "new_revision_id": new_revision["revision_id"], "new_authority_set_hash": snapshot.aggregate_hash,
        "superseded_decision_ids": superseded_decisions, "new_decision_id": new_decision_id,
    }


def _new_authority_set_change(config: WorkflowConfig, guide: AuthoritativeSource, ledger: dict[str, Any], snapshot: RemoteAuthoritySetSnapshot, store: Any) -> dict[str, Any]:
    change_id = f"CR-{__import__('uuid').uuid4().hex[:8].upper()}"
    guide_member = next((item for item in snapshot.members if item.get("role") == "ARCHITECTURE_GUIDE"), {})
    revision = _set_candidate_revision(snapshot, status="ACTIVE")
    value = {
        "schema_version": "1.0", "change_id": change_id, "source_id": "human-guide",
        "source_path": guide.path, "configured_source_path": guide.path,
        "source_role": "HUMAN_AUTHORITY_SET", "authority_origin": "git-remote",
        "candidate_snapshot_path": guide_member.get("snapshot_path"), "candidate_authority_set_snapshot_path": snapshot.manifest_path,
        "base_sha256": str((ledger.get("sources", {}).get("human-guide", {}) or {}).get("accepted_sha256", guide.sha256)),
        "candidate_sha256": guide_member.get("content_sha256"), "base_commit": (ledger.get("sources", {}).get("human-guide", {}) or {}).get("accepted_remote_commit"),
        "candidate_commit": snapshot.commit_sha, "base_blob_sha": (ledger.get("sources", {}).get("human-guide", {}) or {}).get("accepted_remote_blob"),
        "candidate_blob_sha": guide_member.get("git_blob_sha"), "detected_at": snapshot.observed_at,
        "authority_set_hash": snapshot.aggregate_hash, "candidate_authority_set_hash": snapshot.aggregate_hash,
        "authority_set_members": [dict(item) for item in snapshot.members],
        "authority_set_member_changes": member_change_sets(_authority_set_accepted_members(config, ledger), snapshot.members),
        "candidate_revision_id": revision["revision_id"], "candidate_revision_number": 1, "candidate_revisions": [revision],
        "classification": None, "semantic_change": None, "affected_requirements": [], "affected_contract_anchors": [],
        "directly_affected_tasks": [], "dependency_affected_tasks": [], "unaffected_tasks": [],
        "machine_resolvable": None, "human_decision_required": None, "human_decision_requests": [],
        "required_propagation": [], "affected_artifacts": [], "directly_affected_artifacts": [],
        "dependency_affected_artifacts": [], "analysis_summary": "", "status": "CHANGE_PENDING",
    }
    store.save_authority_change(value)
    return value


def _check_remote_authority_set(config: WorkflowConfig, store: Any, state: WorkflowState | None, *, dry_run: bool) -> RemoteCheck:
    members = config.authority_members
    try:
        validate_member_specs(members)
    except ValueError as exc:
        return RemoteCheck(None, errors=(f"REMOTE_CHECK_FAILED: invalid authority set: {exc}",), status=REMOTE_CHECK_FAILED)
    guide = _authority_set_guide_source(config)
    if guide is None:
        return RemoteCheck(None, status="NO_CONFIGURED_HUMAN_GUIDE")
    project = Path(config.project_path).resolve()
    url, error = _remote_url(project, config.authority_remote)
    if error or not url:
        return RemoteCheck(None, errors=(error or f"{REMOTE_CHECK_FAILED}: remote URL is empty",), status=REMOTE_CHECK_FAILED)
    cache = store.remote_cache_path
    if not (cache / "HEAD").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["git", "init", "--bare", "-q", str(cache)], text=True, capture_output=True, check=False)
        if result.returncode:
            return RemoteCheck(None, errors=(f"{REMOTE_CHECK_FAILED}: cannot initialize remote cache: {result.stderr.strip()}",), status=REMOTE_CHECK_FAILED)
    ref = f"refs/remotes/cwo/{config.authority_branch}"
    fetch = _git(cache, "fetch", "--prune", url, f"+refs/heads/{config.authority_branch}:{ref}")
    if fetch.returncode:
        return RemoteCheck(None, errors=(f"{REMOTE_CHECK_FAILED}: git fetch failed: {fetch.stderr.strip()}",), status=REMOTE_CHECK_FAILED)
    commit_result = _git(cache, "rev-parse", ref)
    if commit_result.returncode:
        return RemoteCheck(None, errors=(f"{REMOTE_CHECK_FAILED}: remote branch {config.authority_branch} not found",), status=REMOTE_CHECK_FAILED)
    commit = commit_result.stdout.strip()
    observed = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    for member in members:
        blob, entry_error = _tree_entry(cache, commit, member.path)
        if entry_error or blob is None:
            error_text = entry_error or f"{REMOTE_AUTHORITY_SOURCE_MISSING}: {member.path} is absent at {commit}"
            if not dry_run:
                value = store.load_remote_state() or {"schema_version": "1.0", "sources": {}}
                value["authority_set"] = {**(value.get("authority_set") or {}), "last_seen_remote_commit": commit, "status": REMOTE_AUTHORITY_SOURCE_MISSING}
                store.save_remote_state(value)
            return RemoteCheck(None, errors=(error_text,), status=REMOTE_AUTHORITY_SOURCE_MISSING)
        content = _snapshot_content(cache, commit, member.path)
        if content is None:
            return RemoteCheck(None, errors=(f"{REMOTE_CHECK_FAILED}: cannot read authority member blob {blob}",), status=REMOTE_CHECK_FAILED)
        path = _authority_set_member_snapshot(store, commit, member, content) if not dry_run else None
        records.append({"member_id": member.id, "role": member.role, "path": member.path, "git_blob_sha": blob, "content_sha256": hashlib.sha256(content).hexdigest(), "snapshot_path": str(path) if path else None, "source_revision": commit})
    records = canonical_member_records(records)
    aggregate = aggregate_authority_set_hash(records)
    manifest_payload = {"schema_version": "1.0", "remote_url": url, "branch": config.authority_branch, "commit_sha": commit, "aggregate_hash": aggregate, "members": records, "observed_at": observed}
    manifest_path = _authority_set_manifest(store, commit, manifest_payload, dry_run)
    set_snapshot = RemoteAuthoritySetSnapshot(url, config.authority_branch, commit, tuple(records), aggregate, str(manifest_path) if manifest_path else None, observed)
    primary = next((item for item in records if item.get("role") == "ARCHITECTURE_GUIDE"), records[0])
    primary_snapshot = RemoteAuthoritySnapshot(url, config.authority_branch, commit, primary["path"], primary.get("git_blob_sha"), primary.get("content_sha256"), primary.get("snapshot_path"), observed)
    remote_state = store.load_remote_state() or {"schema_version": "1.0", "sources": {}}
    prior_set = remote_state.get("authority_set", {}) if isinstance(remote_state.get("authority_set"), dict) else {}
    ledger = store.load_authority_ledger() or {"schema_version": "1.0", "sources": {}}
    if not dry_run:
        from .authority import bootstrap_ledger
        ledger = bootstrap_ledger(config, store)
    accepted_members = _authority_set_accepted_members(config, ledger)
    accepted_hash = str((ledger.get("authority_set", {}) or {}).get("accepted_hash") or aggregate_authority_set_hash(accepted_members))
    candidate_hash = str((ledger.get("authority_set", {}) or {}).get("candidate_hash") or "")
    entry = (ledger.get("sources", {}) or {}).get("human-guide", {})
    first_set_observation = not (ledger.get("authority_set") or {}).get("accepted_hash") and not entry.get("accepted_remote_blob")
    pending = str((ledger.get("authority_set", {}) or {}).get("status") or entry.get("status", "ACCEPTED")) in {"CHANGE_PENDING", "PROPAGATING", "WAITING_DECISION", "NEWER_REMOTE_REVISION_AVAILABLE"} and bool((ledger.get("authority_set", {}) or {}).get("change_id") or entry.get("change_id"))
    remote_state["authority_set"] = {**prior_set, **set_snapshot.to_dict(), "last_seen_remote_commit": commit, "last_seen_authority_set_hash": aggregate, "status": "NO_CHANGE" if aggregate == accepted_hash else ("CHANGE_PENDING" if aggregate == candidate_hash else "OBSERVED")}
    if dry_run:
        if aggregate == accepted_hash:
            return RemoteCheck(primary_snapshot, status="NO_CHANGE", authority_set=set_snapshot)
        return RemoteCheck(primary_snapshot, changed=True, status="WOULD_CHANGE", authority_set=set_snapshot)
    if first_set_observation:
        aset = ledger.setdefault("authority_set", {})
        aset.update({"accepted_hash": aggregate, "accepted_members": records, "candidate_hash": aggregate, "candidate_members": records, "accepted_commit": commit, "accepted_revision_id": authority_set_revision_id(aggregate), "status": "ACCEPTED"})
        entry.update({"accepted_remote_commit": commit, "accepted_remote_blob": primary.get("git_blob_sha"), "accepted_content_sha256": primary.get("content_sha256"), "accepted_snapshot_path": primary.get("snapshot_path"), "candidate_remote_commit": commit, "candidate_remote_blob": primary.get("git_blob_sha"), "candidate_content_sha256": primary.get("content_sha256"), "candidate_sha256": primary.get("content_sha256"), "status": "ACCEPTED"})
        ledger.setdefault("sources", {})["human-guide"] = entry
        remote_state["authority_set"]["status"] = "ACCEPTED"
        store.save_authority_ledger(ledger)
        store.save_remote_state(remote_state)
        return RemoteCheck(primary_snapshot, status="NO_CHANGE", authority_set=set_snapshot)
    if aggregate == accepted_hash:
        remote_state["authority_set"]["status"] = "ACCEPTED"
        store.save_remote_state(remote_state)
        return RemoteCheck(primary_snapshot, status="NO_CHANGE", authority_set=set_snapshot)
    if pending:
        current_set_hash = str((ledger.get("authority_set", {}) or {}).get("candidate_hash") or candidate_hash)
        if current_set_hash.lower() == aggregate.lower():
            store.save_remote_state(remote_state)
            return RemoteCheck(primary_snapshot, status="CHANGE_PENDING", authority_set=set_snapshot)
        change_id = (ledger.get("authority_set", {}) or {}).get("change_id") or entry.get("change_id")
        if change_id:
            rolled_change, rollover = _rollover_pending_authority_set(config, guide, {**entry, "change_id": change_id}, set_snapshot, store, state)
            if rolled_change and rollover:
                aset = ledger.setdefault("authority_set", {})
                aset.update({"candidate_hash": aggregate, "candidate_set_hash": aggregate, "candidate_members": records, "candidate_commit": commit, "candidate_revision_id": rollover["new_revision_id"], "change_id": change_id, "status": "CHANGE_PENDING"})
                entry.update({"candidate_authority_set_hash": aggregate, "candidate_authority_set_members": records, "candidate_authority_set_snapshot_path": str(manifest_path) if manifest_path else None, "candidate_remote_commit": commit, "candidate_remote_blob": primary.get("git_blob_sha"), "candidate_content_sha256": primary.get("content_sha256"), "candidate_sha256": primary.get("content_sha256"), "status": "CHANGE_PENDING", "change_id": change_id})
                ledger.setdefault("sources", {})["human-guide"] = entry
                remote_state["authority_set"]["status"] = "CHANGE_PENDING"
                store.save_authority_ledger(ledger)
                store.save_remote_state(remote_state)
                if state is not None and store.state_path.is_file():
                    state.authority_changes[change_id] = rolled_change
                    store.save(state)
                return RemoteCheck(primary_snapshot, changed=True, rollover=rollover, status=NEWER_REMOTE_REVISION_AVAILABLE, authority_set=set_snapshot)
        aset = ledger.setdefault("authority_set", {})
        aset.update({"newer_candidate_hash": aggregate, "newer_candidate_members": records, "newer_candidate_commit": commit, "status": "NEWER_REMOTE_REVISION_AVAILABLE"})
        store.save_authority_ledger(ledger)
        store.save_remote_state(remote_state)
        return RemoteCheck(primary_snapshot, changed=True, status=NEWER_REMOTE_REVISION_AVAILABLE, authority_set=set_snapshot)
    change = _new_authority_set_change(config, guide, ledger, set_snapshot, store)
    aset = ledger.setdefault("authority_set", {})
    aset.update({"accepted_hash": accepted_hash, "accepted_members": accepted_members, "candidate_hash": aggregate, "candidate_set_hash": aggregate, "candidate_members": records, "candidate_commit": commit, "candidate_revision_id": change["candidate_revision_id"], "change_id": change["change_id"], "status": "CHANGE_PENDING"})
    entry = ledger.setdefault("sources", {}).setdefault("human-guide", {})
    entry.update({"candidate_authority_set_hash": aggregate, "candidate_authority_set_members": records, "candidate_authority_set_snapshot_path": str(manifest_path) if manifest_path else None, "candidate_remote_commit": commit, "candidate_remote_blob": primary.get("git_blob_sha"), "candidate_content_sha256": primary.get("content_sha256"), "candidate_sha256": primary.get("content_sha256"), "change_id": change["change_id"], "status": "CHANGE_PENDING"})
    remote_state["authority_set"]["status"] = "CHANGE_PENDING"
    store.save_authority_ledger(ledger)
    store.save_remote_state(remote_state)
    return RemoteCheck(primary_snapshot, changed=True, new_change=change, status="AUTHORITY_CHANGE_DETECTED", authority_set=set_snapshot)


def check_remote_authority(config: WorkflowConfig, store: Any, state: WorkflowState | None = None, *, dry_run: bool = False) -> RemoteCheck:
    if config.authority_members_explicit:
        return _check_remote_authority_set(config, store, state, dry_run=dry_run)
    source = _source(config)
    if source is None:
        return RemoteCheck(None, status="NO_CONFIGURED_HUMAN_GUIDE")
    project = Path(config.project_path).resolve()
    url, error = _remote_url(project, config.authority_remote)
    if error or not url:
        return RemoteCheck(None, errors=(error or f"{REMOTE_CHECK_FAILED}: remote URL is empty",), status=REMOTE_CHECK_FAILED)
    cache = store.remote_cache_path
    if not (cache / "HEAD").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["git", "init", "--bare", "-q", str(cache)], text=True, capture_output=True, check=False)
        if result.returncode:
            return RemoteCheck(None, errors=(f"{REMOTE_CHECK_FAILED}: cannot initialize remote cache: {result.stderr.strip()}",), status=REMOTE_CHECK_FAILED)
    ref = f"refs/remotes/cwo/{config.authority_branch}"
    fetch = _git(cache, "fetch", "--prune", url, f"+refs/heads/{config.authority_branch}:{ref}")
    if fetch.returncode:
        return RemoteCheck(None, errors=(f"{REMOTE_CHECK_FAILED}: git fetch failed: {fetch.stderr.strip()}",), status=REMOTE_CHECK_FAILED)
    commit_result = _git(cache, "rev-parse", ref)
    if commit_result.returncode:
        return RemoteCheck(None, errors=(f"{REMOTE_CHECK_FAILED}: remote branch {config.authority_branch} not found",), status=REMOTE_CHECK_FAILED)
    commit = commit_result.stdout.strip()
    blob, error = _tree_entry(cache, commit, source.path)
    observed = datetime.now(timezone.utc).isoformat()
    if error:
        snapshot = RemoteAuthoritySnapshot(url, config.authority_branch, commit, source.path, None, None, None, observed, REMOTE_AUTHORITY_SOURCE_MISSING)
        _record_missing_remote_state(store, source, snapshot, dry_run)
        return RemoteCheck(snapshot, errors=(error,), status=REMOTE_AUTHORITY_SOURCE_MISSING)
    if blob is None:
        snapshot = RemoteAuthoritySnapshot(url, config.authority_branch, commit, source.path, None, None, None, observed, REMOTE_AUTHORITY_SOURCE_MISSING)
        _record_missing_remote_state(store, source, snapshot, dry_run)
        return RemoteCheck(snapshot, errors=(f"{REMOTE_AUTHORITY_SOURCE_MISSING}: {source.path} is absent at {commit}",), status=REMOTE_AUTHORITY_SOURCE_MISSING)
    content = _snapshot_content(cache, commit, source.path)
    if content is None:
        return RemoteCheck(None, errors=(f"{REMOTE_CHECK_FAILED}: cannot read remote authority blob {blob}",), status=REMOTE_CHECK_FAILED)
    content_sha = hashlib.sha256(content).hexdigest()
    remote_state = store.load_remote_state() or {"schema_version": "1.0", "sources": {}}
    remote_sources = remote_state.setdefault("sources", {})
    from .authority import bootstrap_ledger, source_id, source_role
    sid = source_id(source)
    prior = remote_sources.get(sid, {}) if isinstance(remote_sources.get(sid, {}), dict) else {}
    if dry_run:
        ledger = store.load_authority_ledger() or {"schema_version": "1.0", "sources": {}}
        ledger.setdefault("sources", {}).setdefault(sid, {"source_id": sid, "path": source.path, "configured_path": source.path, "role": source_role(source), "accepted_sha256": source.sha256, "candidate_sha256": source.sha256, "status": "ACCEPTED", "change_id": None})
    else:
        ledger = bootstrap_ledger(config, store)
    ledger_entry = ledger.setdefault("sources", {}).setdefault(sid, {})
    first_observation_matches_declared = not ledger_entry.get("accepted_remote_blob") and content_sha == source.sha256
    same_accepted = first_observation_matches_declared or (blob == ledger_entry.get("accepted_remote_blob") and content_sha == ledger_entry.get("accepted_content_sha256"))
    same_candidate = blob == ledger_entry.get("candidate_remote_blob") and content_sha == ledger_entry.get("candidate_content_sha256")
    snapshot_path = None
    if not dry_run:
        snapshot_path = str(_snapshot_file(store, commit, source, content))
    snapshot = RemoteAuthoritySnapshot(url, config.authority_branch, commit, source.path, blob, content_sha, snapshot_path, observed)
    if dry_run:
        if same_accepted:
            return RemoteCheck(snapshot, status="NO_CHANGE")
        if same_candidate:
            return RemoteCheck(snapshot, status="CHANGE_PENDING")
        return RemoteCheck(snapshot, changed=True, status="WOULD_CHANGE")
    if first_observation_matches_declared:
        ledger_entry.update({"accepted_remote_commit": commit, "accepted_remote_blob": blob, "accepted_content_sha256": content_sha, "accepted_authority_blob": blob, "accepted_authority_content_sha256": content_sha, "accepted_snapshot_path": snapshot_path, "candidate_remote_commit": commit, "candidate_remote_blob": blob, "candidate_authority_blob": blob, "candidate_content_sha256": content_sha, "candidate_sha256": content_sha, "last_enqueued_authority_blob": blob, "status": "ACCEPTED"})
    remote_sources[sid] = {**prior, **snapshot.to_dict(), "last_seen_remote_commit": commit, "last_seen_remote_blob": blob, "last_seen_remote_content_sha256": content_sha}
    active_status = str(ledger_entry.get("status", "ACCEPTED"))
    pending = active_status in {"CHANGE_PENDING", "PROPAGATING", "WAITING_DECISION", "NEWER_REMOTE_REVISION_AVAILABLE"} and isinstance(ledger_entry.get("change_id"), str) and ledger_entry.get("change_id")
    if same_accepted:
        ledger_entry.update({"last_seen_remote_commit": commit, "last_seen_remote_blob": blob, "last_seen_remote_content_sha256": content_sha})
        store.save_authority_ledger(ledger)
        store.save_remote_state(remote_state)
        return RemoteCheck(snapshot, status="NO_CHANGE")
    if pending:
        if same_candidate:
            store.save_authority_ledger(ledger)
            store.save_remote_state(remote_state)
            return RemoteCheck(snapshot, status="CHANGE_PENDING")
        rolled_change, rollover = _rollover_pending_candidate(config, source, ledger_entry, snapshot, store, state)
        if rolled_change is not None and rollover is not None:
            if state is not None:
                state.authority_changes[rollover["change_id"]] = rolled_change
            ledger_entry.update({
                "candidate_remote_commit": commit,
                "candidate_remote_blob": blob,
                "candidate_authority_blob": blob,
                "candidate_content_sha256": content_sha,
                "candidate_sha256": content_sha,
                "candidate_snapshot_path": snapshot_path,
                "candidate_revision_id": rollover["new_revision_id"],
                "candidate_revision_number": len(rolled_change.get("candidate_revisions", [])),
                "candidate_revisions": rolled_change.get("candidate_revisions", []),
                "status": "CHANGE_PENDING",
            })
            remote_sources[sid]["status"] = "CHANGE_PENDING"
            store.save_authority_ledger(ledger)
            store.save_remote_state(remote_state)
            if state is not None and store.state_path.is_file():
                store.save(state)
            return RemoteCheck(snapshot, changed=True, rollover=rollover, status=NEWER_REMOTE_REVISION_AVAILABLE)
        ledger_entry.update({"newer_remote_commit": commit, "newer_remote_blob": blob, "newer_remote_content_sha256": content_sha, "status": "NEWER_REMOTE_REVISION_AVAILABLE"})
        remote_sources[sid]["status"] = "NEWER_REMOTE_REVISION_AVAILABLE"
        store.save_authority_ledger(ledger)
        store.save_remote_state(remote_state)
        return RemoteCheck(snapshot, changed=True, status=NEWER_REMOTE_REVISION_AVAILABLE)
    change = _new_change(config, source, ledger_entry, snapshot, store)
    ledger_entry.update({"path": source.path, "configured_path": source.path, "candidate_path": source.path, "candidate_snapshot_path": snapshot_path, "candidate_remote_commit": commit, "candidate_remote_blob": blob, "candidate_authority_blob": blob, "candidate_content_sha256": content_sha, "candidate_sha256": content_sha, "candidate_revisions": change.get("candidate_revisions", []), "candidate_revision_id": change.get("candidate_revision_id"), "candidate_revision_number": 1, "change_id": change["change_id"], "status": "CHANGE_PENDING", "last_enqueued_authority_blob": blob})
    remote_sources[sid]["status"] = "CHANGE_PENDING"
    store.save_authority_ledger(ledger)
    store.save_remote_state(remote_state)
    return RemoteCheck(snapshot, changed=True, new_change=change, status="AUTHORITY_CHANGE_DETECTED")


def _record_missing_remote_state(store: Any, source: AuthoritativeSource, snapshot: RemoteAuthoritySnapshot, dry_run: bool) -> None:
    if dry_run:
        return
    from .authority import source_id
    value = store.load_remote_state() or {"schema_version": "1.0", "sources": {}}
    sources = value.setdefault("sources", {})
    sid = source_id(source)
    prior = sources.get(sid, {}) if isinstance(sources.get(sid, {}), dict) else {}
    sources[sid] = {**prior, **snapshot.to_dict(), "last_seen_remote_commit": snapshot.commit_sha, "status": REMOTE_AUTHORITY_SOURCE_MISSING}
    store.save_remote_state(value)


def scan_remote_authority_changes(config: WorkflowConfig, store: Any, state: WorkflowState):
    from .authority import AuthorityScan, source_id, source_role
    result = check_remote_authority(config, store, state)
    changes = (result.new_change,) if result.new_change else ()
    if result.status in {"CHANGE_PENDING", NEWER_REMOTE_REVISION_AVAILABLE}:
        ledger = store.load_authority_ledger() or {}
        source = _source(config)
        if config.authority_members_explicit:
            entry = (ledger.get("authority_set", {}) or {})
            sid = "human-guide"
        else:
            sid = source_id(source) if source else "human-guide"
            entry = (ledger.get("sources", {}) or {}).get(sid, {})
        change_id = entry.get("change_id") if isinstance(entry, dict) else None
        if change_id:
            path = store.authority_changes_path / f"{change_id}.json"
            if path.is_file():
                import json
                value = json.loads(path.read_text(encoding="utf-8"))
                changes = (value,)
    if config.authority_members_explicit:
        registered = tuple(str((Path(config.project_path) / item.path).resolve()) for item in config.authority_members)
    else:
        registered = tuple(str((Path(config.project_path) / item.path).resolve()) for item in config.authoritative_sources if source_id(item) == "human-guide" or source_role(item) == "HUMAN_GUIDE")
    overrides: dict[str, tuple[str, str]] = {}
    override_items = config.authority_members if config.authority_members_explicit else config.authoritative_sources
    for item in override_items:
        role = item.role if hasattr(item, "role") else source_role(item)
        if (str(role).upper() == "ARCHITECTURE_GUIDE" or str(role).upper() == "HUMAN_GUIDE"):
            local = Path(item.path)
            local = local if local.is_absolute() else Path(config.project_path) / local
            if local.is_file():
                overrides[str(local.resolve())] = (str(local), hashlib.sha256(local.read_bytes()).hexdigest())
    errors = result.errors
    return AuthorityScan(changes=changes, new_changes=(result.new_change,) if result.new_change else (), integrity_overrides=overrides, registered_paths=registered, errors=errors)
