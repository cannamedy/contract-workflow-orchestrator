from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .models import AuthoritativeSource, WorkflowConfig, WorkflowState
from .scheduler import AGENT_STAGE_NAMES, dependency_closure


CHANGE_CLASSES = {"C0", "C1", "C2", "C3", "C4"}
PENDING_STATUSES = {"CHANGE_PENDING", "PROPAGATING"}


@dataclass(frozen=True)
class AuthorityScan:
    changes: tuple[dict[str, Any], ...] = ()
    new_changes: tuple[dict[str, Any], ...] = ()
    integrity_overrides: dict[str, tuple[str, str]] | None = None
    registered_paths: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    unauthorized: tuple[str, ...] = ()


def source_id(source: AuthoritativeSource) -> str:
    if source.source_id:
        return source.source_id
    lower = source.path.lower()
    if "human" in lower or "架构原理" in source.path:
        return "human-guide"
    if "contract" in lower or "契约" in source.path:
        return "engineering-contract"
    if "plan" in lower or "计划" in source.path:
        return "implementation-plan"
    return Path(source.path).stem or "authority-source"


def source_role(source: AuthoritativeSource) -> str:
    if source.role:
        return source.role
    sid = source_id(source)
    return {"human-guide": "HUMAN_GUIDE", "engineering-contract": "ENGINEERING_CONTRACT", "implementation-plan": "IMPLEMENTATION_PLAN"}.get(sid, "AUTHORITY")


def _relative(project: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project.resolve()))
    except ValueError:
        return str(path.resolve())


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_name(path: Path) -> str:
    return re.sub(r"[^\w]+", "", path.stem, flags=re.UNICODE).casefold()


def _candidate_path(project: Path, configured: Path, ledger_entry: dict[str, Any]) -> Path | None:
    remembered = ledger_entry.get("candidate_path") or ledger_entry.get("path")
    if isinstance(remembered, str):
        remembered_path = Path(remembered)
        remembered_path = remembered_path if remembered_path.is_absolute() else project / remembered_path
        if remembered_path.is_file():
            return remembered_path
    if configured.is_file():
        return configured
    parent = configured.parent if configured.parent.exists() else project
    target = _normalized_name(configured)
    files = [path for path in parent.iterdir() if path.is_file() and path.name != configured.name]
    exact = [path for path in files if _normalized_name(path) == target]
    if exact:
        return sorted(exact, key=lambda path: str(path))[0]
    if target:
        similar = sorted(((SequenceMatcher(None, target, _normalized_name(path)).ratio(), path) for path in files), reverse=True)
        if similar and similar[0][0] >= 0.72:
            return similar[0][1]
    return None


def _active_agent(state: WorkflowState, store: Any) -> bool:
    if not state.run_id:
        return False
    if state.current_stage not in AGENT_STAGE_NAMES:
        return False
    metadata_path = store.run_dir(state.run_id) / "metadata.json"
    if not metadata_path.exists():
        return True
    try:
        import json
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
        return value.get("status") == "running"
    except (OSError, ValueError, TypeError):
        return True


def bootstrap_ledger(config: WorkflowConfig, store: Any) -> dict[str, Any]:
    ledger = store.load_authority_ledger()
    if ledger is None:
        ledger = {"schema_version": "1.0", "sources": {}}
    sources = ledger.setdefault("sources", {})
    changed = False
    for source in config.authoritative_sources:
        sid = source_id(source)
        if sid not in sources:
            sources[sid] = {
                "source_id": sid,
                "path": source.path,
                "configured_path": source.path,
                "role": source_role(source),
                "accepted_sha256": source.sha256,
                "candidate_sha256": source.sha256,
                "status": "ACCEPTED",
                "change_id": None,
            }
            changed = True
    if changed or store.load_authority_ledger() is None:
        store.save_authority_ledger(ledger)
    return ledger


def _new_change(config: WorkflowConfig, source: AuthoritativeSource, sid: str, candidate_path: Path, candidate_sha: str, ledger_entry: dict[str, Any], store: Any) -> dict[str, Any]:
    change_id = ledger_entry.get("change_id")
    if str(ledger_entry.get("candidate_sha256", "")).lower() != candidate_sha.lower():
        change_id = None
    if not isinstance(change_id, str) or not change_id:
        change_id = f"CR-{uuid.uuid4().hex[:8].upper()}"
    change = {
        "schema_version": "1.0",
        "change_id": change_id,
        "source_id": sid,
        "source_path": _relative(Path(config.project_path), candidate_path),
        "configured_source_path": source.path,
        "source_role": source_role(source),
        "base_sha256": str(ledger_entry.get("accepted_sha256", source.sha256)),
        "candidate_sha256": candidate_sha,
        "detected_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
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
        "analysis_summary": "",
        "status": "CHANGE_PENDING",
    }
    store.save_authority_change(change)
    return change


def scan_authority_changes(config: WorkflowConfig, store: Any, state: WorkflowState) -> AuthorityScan:
    ledger = bootstrap_ledger(config, store)
    sources = ledger.setdefault("sources", {})
    changes: list[dict[str, Any]] = []
    new_changes: list[dict[str, Any]] = []
    overrides: dict[str, tuple[str, str]] = {}
    registered: set[str] = set()
    errors: list[str] = []
    unauthorized: list[str] = []
    active = _active_agent(state, store)
    ledger_changed = False
    for source in config.authoritative_sources:
        if source.mutable_after_start:
            continue
        sid = source_id(source)
        entry = sources.setdefault(sid, {"source_id": sid, "path": source.path, "accepted_sha256": source.sha256, "candidate_sha256": source.sha256, "status": "ACCEPTED", "change_id": None})
        configured = Path(source.path)
        configured = configured if configured.is_absolute() else Path(config.project_path) / configured
        candidate = _candidate_path(Path(config.project_path), configured, entry)
        if candidate is None:
            errors.append(f"authoritative source missing: {configured}")
            continue
        try:
            digest = _hash(candidate)
        except OSError as exc:
            errors.append(f"could not hash authoritative source {candidate}: {exc}")
            continue
        accepted = str(entry.get("accepted_sha256", source.sha256))
        if digest.lower() == accepted.lower():
            continue
        registered.update({str(configured.resolve()), str(candidate.resolve())})
        if active:
            unauthorized.append(f"UNAUTHORIZED_AUTHORITY_MUTATION: {candidate}")
            continue
        change_id = entry.get("change_id")
        existing = None
        if isinstance(change_id, str) and change_id:
            existing_path = store.authority_changes_path / f"{change_id}.json"
            if existing_path.exists():
                try:
                    import json
                    existing = json.loads(existing_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    existing = None
        if not isinstance(existing, dict) or existing.get("candidate_sha256") != digest:
            existing = _new_change(config, source, sid, candidate, digest, entry, store)
            new_changes.append(existing)
            entry.update({"path": _relative(Path(config.project_path), candidate), "candidate_path": _relative(Path(config.project_path), candidate), "candidate_sha256": digest, "change_id": existing["change_id"], "status": "CHANGE_PENDING"})
            ledger_changed = True
        changes.append(existing)
        overrides[str(configured.resolve())] = (str(candidate), digest)
    if ledger_changed:
        store.save_authority_ledger(ledger)
    return AuthorityScan(tuple(changes), tuple(new_changes), overrides, tuple(sorted(registered)), tuple(errors), tuple(unauthorized))


def authority_snapshot(config: WorkflowConfig, store: Any) -> dict[str, str]:
    ledger = bootstrap_ledger(config, store)
    snapshot: dict[str, str] = {}
    root = Path(config.project_path)
    for source in config.authoritative_sources:
        if source.mutable_after_start:
            continue
        configured = Path(source.path)
        configured = configured if configured.is_absolute() else root / configured
        entry = ledger.get("sources", {}).get(source_id(source), {})
        candidate = _candidate_path(root, configured, entry if isinstance(entry, dict) else {})
        if candidate and candidate.is_file():
            snapshot[source_id(source)] = _hash(candidate)
    return snapshot


def dependency_tasks(config: WorkflowConfig, direct: list[str]) -> list[str]:
    return sorted(dependency_closure(config, direct))


def _known_authority_text(config: WorkflowConfig) -> str:
    chunks: list[str] = []
    root = Path(config.project_path)
    for source in config.authoritative_sources:
        path = Path(source.path)
        path = path if path.is_absolute() else root / path
        if path.is_file():
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    for path in root.rglob("*.md"):
        if any(part in {".git", ".contract-workflow"} for part in path.parts):
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(chunks)


def validate_analysis(config: WorkflowConfig, store: Any, state: WorkflowState, outcome: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    raw = outcome.get("authority_change")
    if not isinstance(raw, dict):
        return None, ["authority_change must be an object"]
    required = ("change_id", "base_sha256", "candidate_sha256", "classification", "semantic_change", "affected_requirements", "affected_contract_anchors", "directly_affected_tasks", "dependency_affected_tasks", "unaffected_tasks", "machine_resolvable", "human_decision_required", "human_decision_requests", "required_propagation", "analysis_summary")
    errors = [f"authority_change missing {key}" for key in required if key not in raw]
    change_id = raw.get("change_id")
    scan = scan_authority_changes(config, store, state)
    change = next((item for item in scan.changes if item.get("change_id") == change_id), None)
    if change is None:
        errors.append("change_id is not a registered pending authority change")
    if raw.get("classification") not in CHANGE_CLASSES:
        errors.append("classification must be C0, C1, C2, C3, or C4")
    for key in ("semantic_change", "machine_resolvable", "human_decision_required"):
        if not isinstance(raw.get(key), bool):
            errors.append(f"{key} must be boolean")
    lists = ("affected_requirements", "affected_contract_anchors", "directly_affected_tasks", "dependency_affected_tasks", "unaffected_tasks", "required_propagation")
    for key in lists:
        if not isinstance(raw.get(key), list) or not all(isinstance(item, str) for item in raw.get(key, [])):
            errors.append(f"{key} must be a list of strings")
    if not isinstance(raw.get("human_decision_requests"), list) or not all(isinstance(item, dict) for item in raw.get("human_decision_requests", [])):
        errors.append("human_decision_requests must be a list of objects")
    requirements = raw.get("affected_requirements", [])
    if isinstance(requirements, list):
        errors.extend(f"invalid requirement id: {item}" for item in requirements if not re.fullmatch(r"(?:REQ|OCS)-[A-Za-z0-9_.-]+", item))
    anchors = raw.get("affected_contract_anchors", [])
    if isinstance(anchors, list):
        text = _known_authority_text(config)
        errors.extend(f"untraceable contract anchor: {item}" for item in anchors if item not in text)
    known = {task.id for _, task in config.tasks}
    direct = raw.get("directly_affected_tasks", [])
    reported_dependencies = raw.get("dependency_affected_tasks", [])
    unaffected = raw.get("unaffected_tasks", [])
    for key, values in (("directly_affected_tasks", direct), ("dependency_affected_tasks", reported_dependencies), ("unaffected_tasks", unaffected)):
        if isinstance(values, list):
            errors.extend(f"unknown task id in {key}: {item}" for item in values if item not in known)
    computed = dependency_tasks(config, direct) if isinstance(direct, list) else []
    if isinstance(reported_dependencies, list) and sorted(reported_dependencies) != computed:
        errors.append(f"dependency closure mismatch: expected {computed}")
    if isinstance(direct, list) and isinstance(reported_dependencies, list) and set(direct) & set(reported_dependencies):
        errors.append("directly_affected_tasks and dependency_affected_tasks overlap")
    if isinstance(unaffected, list) and (set(unaffected) & (set(direct) | set(reported_dependencies))):
        errors.append("unaffected_tasks overlaps affected tasks")
    if known and isinstance(direct, list) and isinstance(reported_dependencies, list) and isinstance(unaffected, list) and set(direct) | set(reported_dependencies) | set(unaffected) != known:
        errors.append("task impact sets do not cover the configured task graph")
    if raw.get("classification") in {"C0", "C1"} and (raw.get("semantic_change") or direct or reported_dependencies):
        errors.append("C0/C1 non-semantic analysis cannot affect tasks")
    if raw.get("classification") in {"C2", "C3", "C4"} and raw.get("semantic_change") is not True:
        errors.append("C2-C4 analysis must declare semantic_change=true")
    if raw.get("human_decision_required") and not (raw.get("human_decision_requests") or outcome.get("decision_requests")):
        errors.append("human_decision_required requires a Decision Request")
    if change:
        if str(raw.get("base_sha256", "")).lower() != str(change.get("base_sha256", "")).lower():
            errors.append("base_sha256 does not match the registered change")
        if str(raw.get("candidate_sha256", "")).lower() != str(change.get("candidate_sha256", "")).lower():
            errors.append("candidate_sha256 does not match the registered change")
        candidate = Path(config.project_path) / str(change.get("source_path", ""))
        if not candidate.is_file() or _hash(candidate).lower() != str(change.get("candidate_sha256", "")).lower():
            errors.append("candidate hash does not match the current authority file")
    return (raw, errors) if not errors else (None, errors)
