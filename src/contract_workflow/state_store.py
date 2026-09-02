from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import HumanDecision, WorkflowState


class StateStoreError(RuntimeError):
    pass


class StateStore:
    def __init__(self, root: Path):
        self.root = root
        self.state_path = root / "state.json"
        self.events_path = root / "events.jsonl"
        self.runs_path = root / "runs"
        self.decisions_path = root / "decisions"
        self.adrs_path = root / "adrs"

    def ensure(self) -> None:
        self.runs_path.mkdir(parents=True, exist_ok=True)
        self.decisions_path.mkdir(parents=True, exist_ok=True)
        self.adrs_path.mkdir(parents=True, exist_ok=True)

    def load(self) -> WorkflowState | None:
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state root is not an object")
            required = {"schema_version", "project", "project_path", "workflow_file", "workflow_digest", "current_stage", "attempt", "total_steps", "status", "created_at", "updated_at"}
            missing = required - set(data)
            if missing:
                raise ValueError("missing state fields: " + ", ".join(sorted(missing)))
            return WorkflowState.from_dict(data)
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
            raise StateStoreError(f"corrupt state: {self.state_path}: {exc}") from exc

    def save(self, state: WorkflowState) -> None:
        self.ensure()
        payload = json.dumps(state.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        fd, name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.state_path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise StateStoreError(f"could not atomically save state: {exc}") from exc

    def run_dir(self, run_id: str) -> Path:
        path = self.runs_path / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_decision(self, decision: HumanDecision) -> None:
        self.ensure()
        if not decision.decision_id or Path(decision.decision_id).name != decision.decision_id:
            raise StateStoreError("decision_id must be a single safe path component")
        path = self.decisions_path / f"{decision.decision_id}.json"
        self._atomic_write(path, json.dumps(decision.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    def save_adr(self, adr: dict[str, object]) -> None:
        self.ensure()
        adr_id = str(adr.get("adr_id", ""))
        if not adr_id or Path(adr_id).name != adr_id:
            raise StateStoreError("ADR requires adr_id")
        path = self.adrs_path / f"{adr_id}.json"
        self._atomic_write(path, json.dumps(adr, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise StateStoreError(f"could not atomically save {path.name}: {exc}") from exc
