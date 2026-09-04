from __future__ import annotations

import os
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from .base import RunnerResult, _time


class RunnerBindingError(RuntimeError):
    """The runner could not be bound to the disposable execution workspace."""


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
        effective_cwd = str(cwd.resolve())
        if _configured_execution_directory(command) not in {None, effective_cwd}:
            raise RunnerBindingError("RUNNER_ORIGIN_CWD_FORBIDDEN: runner command is not bound to the RunWorkspace")
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
        sandbox_mode = "default"
        for index, item in enumerate(command[:-1]):
            if item in {"--sandbox", "-s"}:
                sandbox_mode = command[index + 1]
                break
            if item.startswith("--sandbox="):
                sandbox_mode = item.split("=", 1)[1]
                break
        return RunnerResult(
            exit_code,
            stdout_path,
            stderr_path,
            started,
            _time(),
            timed_out,
            {
                "runner": "codex_cli",
                "argv": json.dumps(command, ensure_ascii=False),
                "command": " ".join(command[:3]),
                "authoritative_origin": os.environ.get("CWO_AUTHORITATIVE_ORIGIN", ""),
                "effective_cwd": effective_cwd,
                "sandbox_mode": sandbox_mode,
            },
        )


def _bind_execution_directory(command: list[str], cwd: Path) -> list[str]:
    """Bind every Codex directory option to the disposable RunWorkspace."""
    result = list(command)
    bound = str(cwd.resolve())
    for index, item in enumerate(result[:-1]):
        if item in {"-C", "--cd"}:
            result[index + 1] = bound
        elif item.startswith("--cd="):
            result[index] = f"--cd={bound}"
        elif item.startswith("-C") and item != "-C":
            result[index] = f"-C{bound}"
    return result


def _configured_execution_directory(command: list[str]) -> str | None:
    """Return an explicit Codex directory binding, if one is present."""
    for index, item in enumerate(command):
        if item in {"-C", "--cd"} and index + 1 < len(command):
            return str(Path(command[index + 1]).resolve())
        if item.startswith("--cd="):
            return str(Path(item.split("=", 1)[1]).resolve())
        if item.startswith("-C") and item != "-C":
            return str(Path(item[2:]).resolve())
    return None
