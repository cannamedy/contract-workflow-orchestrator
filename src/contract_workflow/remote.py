from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AuthoritativeSource, WorkflowConfig, WorkflowState


REMOTE_CHECK_FAILED = "REMOTE_CHECK_FAILED"
REMOTE_AUTHORITY_SOURCE_MISSING = "REMOTE_AUTHORITY_SOURCE_MISSING"
NEWER_REMOTE_REVISION_AVAILABLE = "NEWER_REMOTE_REVISION_AVAILABLE"


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
class RemoteCheck:
    snapshot: RemoteAuthoritySnapshot | None
    changed: bool = False
    new_change: dict[str, Any] | None = None
    errors: tuple[str, ...] = ()
    status: str = "NO_CHANGE"


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


def _new_change(config: WorkflowConfig, source: AuthoritativeSource, entry: dict[str, Any], snapshot: RemoteAuthoritySnapshot, store: Any) -> dict[str, Any]:
    import uuid
    change_id = f"CR-{uuid.uuid4().hex[:8].upper()}"
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
    }
    store.save_authority_change(value)
    return value


def check_remote_authority(config: WorkflowConfig, store: Any, state: WorkflowState | None = None, *, dry_run: bool = False) -> RemoteCheck:
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
        return RemoteCheck(snapshot, changed=not same_accepted, status="WOULD_CHANGE" if not same_accepted else "NO_CHANGE")
    if first_observation_matches_declared:
        ledger_entry.update({"accepted_remote_commit": commit, "accepted_remote_blob": blob, "accepted_content_sha256": content_sha, "accepted_authority_blob": blob, "accepted_authority_content_sha256": content_sha, "candidate_remote_commit": commit, "candidate_remote_blob": blob, "candidate_authority_blob": blob, "candidate_content_sha256": content_sha, "candidate_sha256": content_sha, "last_enqueued_authority_blob": blob, "status": "ACCEPTED"})
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
            return RemoteCheck(snapshot, changed=True, status="CHANGE_PENDING")
        ledger_entry.update({"newer_remote_commit": commit, "newer_remote_blob": blob, "newer_remote_content_sha256": content_sha, "status": "NEWER_REMOTE_REVISION_AVAILABLE"})
        remote_sources[sid]["status"] = "NEWER_REMOTE_REVISION_AVAILABLE"
        store.save_authority_ledger(ledger)
        store.save_remote_state(remote_state)
        return RemoteCheck(snapshot, changed=True, status=NEWER_REMOTE_REVISION_AVAILABLE)
    change = _new_change(config, source, ledger_entry, snapshot, store)
    ledger_entry.update({"path": source.path, "configured_path": source.path, "candidate_path": source.path, "candidate_snapshot_path": snapshot_path, "candidate_remote_commit": commit, "candidate_remote_blob": blob, "candidate_authority_blob": blob, "candidate_content_sha256": content_sha, "candidate_sha256": content_sha, "change_id": change["change_id"], "status": "CHANGE_PENDING", "last_enqueued_authority_blob": blob})
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
        sid = source_id(source) if source else "human-guide"
        entry = (ledger.get("sources", {}) or {}).get(sid, {})
        change_id = entry.get("change_id") if isinstance(entry, dict) else None
        if change_id:
            path = store.authority_changes_path / f"{change_id}.json"
            if path.is_file():
                import json
                value = json.loads(path.read_text(encoding="utf-8"))
                changes = (value,)
    registered = tuple(str((Path(config.project_path) / item.path).resolve()) for item in config.authoritative_sources if source_id(item) == "human-guide" or source_role(item) == "HUMAN_GUIDE")
    overrides: dict[str, tuple[str, str]] = {}
    for item in config.authoritative_sources:
        if source_id(item) == "human-guide" or source_role(item) == "HUMAN_GUIDE":
            local = Path(item.path)
            local = local if local.is_absolute() else Path(config.project_path) / local
            if local.is_file():
                overrides[str(local.resolve())] = (str(local), hashlib.sha256(local.read_bytes()).hexdigest())
    errors = result.errors
    return AuthorityScan(changes=changes, new_changes=(result.new_change,) if result.new_change else (), integrity_overrides=overrides, registered_paths=registered, errors=errors)
