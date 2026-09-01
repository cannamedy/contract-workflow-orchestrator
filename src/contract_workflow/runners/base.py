from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol


def _time() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunnerResult:
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    started_at: str
    finished_at: str
    timed_out: bool = False
    runner_metadata: dict[str, str] = field(default_factory=dict)


class AgentRunner(Protocol):
    def run(self, cwd: Path, prompt: str, run_dir: Path, timeout: int, env: Mapping[str, str] | None = None) -> RunnerResult:
        ...


class CodexAppServerRunner:
    """Reserved v0.1 extension seam; App Server transport is intentionally not implemented."""

    def run(self, cwd: Path, prompt: str, run_dir: Path, timeout: int, env: Mapping[str, str] | None = None) -> RunnerResult:
        raise NotImplementedError("CodexAppServerRunner is reserved for a future release")


def run_times() -> tuple[str, str]:
    started = _time()
    return started, _time()
