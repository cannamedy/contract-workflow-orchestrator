from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from .base import RunnerResult, _time


class CodexCliRunner:
    def __init__(self, command: str | None = None):
        self.command = command

    @staticmethod
    def available() -> bool:
        return shutil.which("codex") is not None

    def run(self, cwd: Path, prompt: str, run_dir: Path, timeout: int, env: Mapping[str, str] | None = None) -> RunnerResult:
        if self.command:
            command = shlex.split(self.command)
        else:
            command = ["codex", "exec", "-C", str(cwd), "-"]
        command = _bind_execution_directory(command, cwd)
        if "{prompt}" in command:
            command = [prompt if item == "{prompt}" else item for item in command]
            stdin = None
        else:
            stdin = subprocess.PIPE
        stdout_path, stderr_path = run_dir / "stdout.log", run_dir / "stderr.log"
        started = _time()
        timed_out = False
        exit_code = 127
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
                process = subprocess.Popen(command, cwd=str(cwd), stdin=stdin, stdout=stdout, stderr=stderr, env=merged_env, text=True, encoding="utf-8")
                try:
                    process.communicate(None if stdin is None else prompt, timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.kill()
                    process.communicate()
                exit_code = process.returncode
        except (OSError, ValueError) as exc:
            stderr_path.write_text(str(exc) + "\n", encoding="utf-8")
        return RunnerResult(exit_code, stdout_path, stderr_path, started, _time(), timed_out, {"runner": "codex_cli", "command": " ".join(command[:3])})


def _bind_execution_directory(command: list[str], cwd: Path) -> list[str]:
    """Prevent a configured absolute Codex -C from escaping the run workspace."""
    result = list(command)
    for index, item in enumerate(result[:-1]):
        if item in {"-C", "--cd"}:
            result[index + 1] = str(cwd)
            return result
    return result
