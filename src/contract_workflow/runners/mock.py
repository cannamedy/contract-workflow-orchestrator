from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .base import RunnerResult, run_times
from ..outcome import make_outcome


class MockRunner:
    """Deterministic, model-free runner used by tests and dry-run fixtures."""

    def __init__(self, fixtures: Mapping[str, Any] | None = None):
        self.fixtures = dict(fixtures or {})
        self.calls: list[str] = []

    def run(self, cwd: Path, prompt: str, run_dir: Path, timeout: int, env: Mapping[str, str] | None = None) -> RunnerResult:
        stage = _prompt_value(prompt, "CURRENT STAGE") or _prompt_value(prompt, "current stage") or ""
        run_id = _prompt_value(prompt, "RUN ID") or run_dir.name
        project = _prompt_value(prompt, "PROJECT") or cwd.name
        self.calls.append(stage)
        spec = self.fixtures.get(stage)
        if isinstance(spec, list):
            spec = spec[min(self.calls.count(stage) - 1, len(spec) - 1)]
        if isinstance(spec, dict) and "runner_failure" in spec:
            started, finished = run_times()
            (run_dir / "stdout.log").write_text("mock runner failure\n", encoding="utf-8")
            (run_dir / "stderr.log").write_text(str(spec["runner_failure"]) + "\n", encoding="utf-8")
            return RunnerResult(1, run_dir / "stdout.log", run_dir / "stderr.log", started, finished, runner_metadata={"runner": "mock"})
        if not isinstance(spec, dict):
            verdict = "COMPLETED" if stage == "FINAL_VERIFICATION" else "APPROVED"
            spec = {"verdict": verdict}
        outcome = make_outcome(run_id, stage, project, str(spec.get("verdict", "APPROVED")), **{key: value for key, value in spec.items() if key != "verdict"})
        (run_dir / "outcome.json").write_text(json.dumps(outcome, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (run_dir / "stdout.log").write_text(f"mock stage completed: {stage}\n", encoding="utf-8")
        (run_dir / "stderr.log").write_text("", encoding="utf-8")
        started, finished = run_times()
        return RunnerResult(0, run_dir / "stdout.log", run_dir / "stderr.log", started, finished, runner_metadata={"runner": "mock"})


def _prompt_value(prompt: str, label: str) -> str | None:
    for line in prompt.splitlines():
        if line.strip().startswith(label + ":"):
            return line.split(":", 1)[1].strip()
    return None
