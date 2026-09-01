from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import WorkflowState


class StateStoreError(RuntimeError):
    pass


class StateStore:
    def __init__(self, root: Path):
        self.root = root
        self.state_path = root / "state.json"
        self.events_path = root / "events.jsonl"
        self.runs_path = root / "runs"

    def ensure(self) -> None:
        self.runs_path.mkdir(parents=True, exist_ok=True)

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
