from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .models import TaskSpec, WorkItemState, WorkItemStatus, WorkflowConfig, WorkflowState


REQUIREMENT_ID = re.compile(r"(?:REQ|OCS)-[A-Za-z0-9_.-]+$")


def _anchor_traceable(anchor: str, contract_text: str) -> bool:
    if anchor in contract_text:
        return True
    # Human task anchors commonly use the compact `§3` form while Markdown
    # Contracts spell the same anchor as `## 3. ...` or `### 0.2 ...`.
    match = re.search(r"§\s*([0-9]+(?:\.[0-9]+)*)", anchor)
    label = match.group(1) if match else anchor.strip()
    return bool(re.search(rf"(?m)^\s*#{{2,6}}\s*{re.escape(label)}(?:\s|[.:]|$)", contract_text))


def graph_digest(graph: dict[str, Any]) -> str:
    payload = {key: value for key, value in graph.items() if key != "graph_sha256"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def validate_plan_graph(config: WorkflowConfig, graph: Any, *, contract_text: str = "") -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(graph, dict):
        return None, ["plan_graph must be an object"]
    if not isinstance(graph.get("plan_sha256"), str) or not graph["plan_sha256"]:
        return None, ["plan_graph.plan_sha256 is required"]
    raw_tasks = graph.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return None, ["plan_graph.tasks must be a non-empty array"]
    errors: list[str] = []
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    ownership: dict[str, str] = {}
    for index, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            errors.append(f"tasks[{index}] must be an object")
            continue
        task_id = raw.get("id")
        group = raw.get("group")
        if not isinstance(task_id, str) or not task_id:
            errors.append(f"tasks[{index}].id is required")
            continue
        if task_id in ids:
            errors.append(f"duplicate task id: {task_id}")
        ids.add(task_id)
        if not isinstance(group, str) or not group:
            errors.append(f"tasks[{index}].group is required")
        values: dict[str, Any] = {"id": task_id, "group": group or "", "dependencies": raw.get("dependencies", []), "requirement_ids": raw.get("requirement_ids", []), "contract_anchors": raw.get("contract_anchors", []), "allowed_paths": raw.get("allowed_paths", []), "expected_outputs": raw.get("expected_outputs", [])}
        for key in ("engineering_spec_anchors", "machine_contract_refs", "conformance_ids", "implementation_design_refs"):
            if key in raw:
                values[key] = raw[key]
        for key in ("dependencies", "requirement_ids", "contract_anchors", "allowed_paths", "expected_outputs", "engineering_spec_anchors", "machine_contract_refs", "conformance_ids", "implementation_design_refs"):
            value = values.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                errors.append(f"tasks[{index}].{key} must be an array of strings")
        for requirement in values["requirement_ids"] if isinstance(values["requirement_ids"], list) else []:
            if not REQUIREMENT_ID.fullmatch(requirement):
                errors.append(f"invalid requirement id: {requirement}")
        for anchor in values["contract_anchors"] if isinstance(values["contract_anchors"], list) else []:
            if contract_text and not _anchor_traceable(anchor, contract_text):
                errors.append(f"untraceable contract anchor: {anchor}")
        for path_key in ("allowed_paths", "expected_outputs"):
            for path in values[path_key] if isinstance(values[path_key], list) else []:
                if not path or path.startswith("/") or ".." in path.split("/"):
                    errors.append(f"unsafe {path_key} path: {path}")
                owner = ownership.get(path)
                if owner and owner != task_id:
                    errors.append(f"duplicate path ownership: {path} ({owner}, {task_id})")
                ownership[path] = task_id
        values["skill_role"] = raw.get("skill_role")
        normalized.append(values)
    if len(ids) != len(normalized):
        errors.append("task IDs are not unique")
    for task in normalized:
        for dependency in task["dependencies"] if isinstance(task["dependencies"], list) else []:
            if dependency not in ids:
                errors.append(f"unknown dependency {dependency} for {task['id']}")
    indegree = {task_id: 0 for task_id in ids}
    edges = {task_id: [] for task_id in ids}
    for task in normalized:
        for dependency in task["dependencies"] if isinstance(task["dependencies"], list) else []:
            if dependency in ids:
                indegree[task["id"]] += 1
                edges[dependency].append(task["id"])
    queue = [task_id for task_id, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for child in edges[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(ids):
        errors.append("plan graph contains a dependency cycle")
    if errors:
        return None, errors
    result = {"schema_version": "1.0", "plan_sha256": graph["plan_sha256"], "tasks": normalized}
    result["graph_sha256"] = graph_digest(result)
    if graph.get("graph_sha256") is not None and graph.get("graph_sha256") != result["graph_sha256"]:
        return None, ["plan_graph.graph_sha256 does not match the canonical graph digest"]
    return result, []


def reconcile_plan_graph(config: WorkflowConfig, state: WorkflowState, graph: dict[str, Any], affected: set[str]) -> dict[str, Any]:
    old = {item["id"]: item for item in (state.plan_graph or {}).get("tasks", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
    new = {item["id"]: item for item in graph.get("tasks", [])}
    unchanged = sorted(task_id for task_id in old.keys() & new.keys() if old[task_id] == new[task_id] and task_id not in affected)
    changed = sorted(task_id for task_id in old.keys() & new.keys() if task_id not in unchanged)
    added = sorted(new.keys() - old.keys())
    superseded = sorted(old.keys() - new.keys())
    completed_unaffected = sorted(task_id for task_id in unchanged if state.work_items.get(task_id, WorkItemState(task_id, "")).status == WorkItemStatus.COMPLETED.value)
    return {"unchanged": unchanged, "affected": sorted(set(changed) | affected), "new": added, "superseded": superseded, "completed_unaffected": completed_unaffected, "old_graph_sha256": (state.plan_graph or {}).get("graph_sha256"), "new_graph_sha256": graph.get("graph_sha256")}


def task_specs(graph: dict[str, Any]) -> tuple[tuple[str, TaskSpec], ...]:
    return tuple((str(raw.get("group", "default")), TaskSpec(id=str(raw["id"]), expected_outputs=tuple(raw.get("expected_outputs", ())), allowed_paths=tuple(raw.get("allowed_paths", ())), dependencies=tuple(raw.get("dependencies", ())), requirement_ids=tuple(raw.get("requirement_ids", ())), contract_anchors=tuple(raw.get("contract_anchors", ())), skill_role=raw.get("skill_role"), engineering_spec_anchors=tuple(raw.get("engineering_spec_anchors", ())), machine_contract_refs=tuple(raw.get("machine_contract_refs", ())), conformance_ids=tuple(raw.get("conformance_ids", ())), implementation_design_refs=tuple(raw.get("implementation_design_refs", ())))) for raw in graph.get("tasks", []) if isinstance(raw, dict) and isinstance(raw.get("id"), str))
