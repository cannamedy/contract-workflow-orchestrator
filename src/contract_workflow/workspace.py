from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


IGNORED_DIRECTORY_NAMES = frozenset({
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
})


class WorkspaceError(RuntimeError):
    pass


class TargetDriftError(WorkspaceError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _excluded(path: Path, root: Path, excluded_roots: tuple[Path, ...]) -> bool:
    if any(part in IGNORED_DIRECTORY_NAMES for part in path.relative_to(root).parts):
        return True
    return any(path == item or path.is_relative_to(item) for item in excluded_roots)


def tree_fingerprint(root: Path, excluded_roots: tuple[Path, ...] = ()) -> dict[str, dict[str, Any]]:
    """Fingerprint the visible project tree without reading Git internals."""
    root = root.resolve()
    excluded_roots = tuple(item.resolve() for item in excluded_roots)
    result: dict[str, dict[str, Any]] = {}
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories
            if not _excluded(current_path / name, root, excluded_roots)
        ]
        for name in files:
            path = current_path / name
            if _excluded(path, root, excluded_roots):
                continue
            relative = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                result[relative] = {
                    "kind": "symlink",
                    "sha256": hashlib.sha256(os.readlink(path).encode()).hexdigest(),
                    "mode": mode,
                }
            elif path.is_file():
                result[relative] = {"kind": "file", "sha256": _sha256(path), "mode": mode}
    return result


def git_fingerprint(project: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(project), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else f"<git-error:{result.returncode}:{result.stderr.strip()}>"

    index = project / ".git" / "index"
    return {
        "head": run("rev-parse", "HEAD").strip(),
        "status": run("status", "--porcelain=v1", "-uall"),
        "index_sha256": _sha256(index) if index.is_file() else None,
        "index_lock": (project / ".git" / "index.lock").exists(),
        "head_lock": (project / ".git" / "HEAD.lock").exists(),
    }


def real_fingerprint(project: Path, excluded_roots: tuple[Path, ...] = ()) -> dict[str, Any]:
    return {"tree": tree_fingerprint(project, excluded_roots), "git": git_fingerprint(project)}


def _copy_tree(source: Path, destination: Path, root: Path, excluded_roots: tuple[Path, ...]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        if _excluded(entry, root, excluded_roots):
            continue
        target = destination / entry.name
        if entry.is_dir() and not entry.is_symlink():
            _copy_tree(entry, target, root, excluded_roots)
        elif entry.is_symlink() and entry.is_dir():
            # Never reproduce a symlink into the real project from a run
            # workspace.  Copy its visible contents as ordinary files.
            _copy_tree(entry, target, root, excluded_roots)
        elif entry.is_file() or entry.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry.resolve() if entry.is_symlink() else entry, target, follow_symlinks=True)


@dataclass
class RunWorkspace:
    real_project: Path
    path: Path
    baseline: dict[str, dict[str, Any]]
    real_baseline: dict[str, Any]
    excluded_roots: tuple[Path, ...] = ()

    @classmethod
    def create(cls, real_project: Path, state_root: Path, run_id: str) -> "RunWorkspace":
        real_project = real_project.resolve()
        workspace_root = (state_root / "workspaces" / run_id).resolve()
        path = workspace_root / "project"
        if path.exists():
            raise WorkspaceError(f"run workspace already exists: {path}")
        workspace_root.mkdir(parents=True, exist_ok=False)
        excluded = (state_root.resolve(),)
        before = real_fingerprint(real_project, excluded)
        _copy_tree(real_project, path, real_project, excluded)
        after = real_fingerprint(real_project, excluded)
        if before != after:
            shutil.rmtree(workspace_root, ignore_errors=True)
            raise TargetDriftError("real project changed while creating run workspace")
        return cls(real_project, path, tree_fingerprint(path), before, excluded)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any], real_project: Path) -> "RunWorkspace | None":
        raw_path = metadata.get("workspace_path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        path = Path(raw_path).resolve()
        baseline = metadata.get("workspace_baseline")
        real_baseline = metadata.get("real_baseline")
        if not isinstance(baseline, dict) or not isinstance(real_baseline, dict):
            raise WorkspaceError("run metadata has incomplete workspace baseline")
        raw_excluded = metadata.get("excluded_roots", [])
        excluded = tuple(Path(item).resolve() for item in raw_excluded if isinstance(item, str)) if isinstance(raw_excluded, list) else ()
        return cls(real_project.resolve(), path, baseline, real_baseline, excluded)

    def current_real_fingerprint(self) -> dict[str, Any]:
        return real_fingerprint(self.real_project, self.excluded_roots)

    def real_unchanged(self) -> bool:
        # The caller's state root is normally external.  The baseline already
        # contains its exact exclusion set; use the stored tree/git comparison
        # through the project-root default for the common case.
        return self.current_real_fingerprint() == self.real_baseline

    def diff(self) -> list[dict[str, Any]]:
        current = tree_fingerprint(self.path)
        changes: list[dict[str, Any]] = []
        for relative in sorted(set(self.baseline) | set(current)):
            before = self.baseline.get(relative)
            after = current.get(relative)
            if before != after:
                status = "ADDED" if before is None else ("DELETED" if after is None else "MODIFIED")
                changes.append({"path": relative, "status": status, "before": before, "after": after})
        return changes

    def record_diff(self, run_dir: Path, changes: list[dict[str, Any]]) -> None:
        (run_dir / "workspace-diff.json").write_text(json.dumps(changes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def discard(self) -> None:
        root = self.path.parent
        if root.exists():
            shutil.rmtree(root)


def _safe_relative(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise WorkspaceError(f"unsafe workspace path: {relative}")
    return path


def apply_validated_diff(
    workspace: RunWorkspace,
    changes: list[dict[str, Any]],
    allowed_patterns: tuple[str, ...],
) -> None:
    """Apply an already validated workspace diff with preconditions and rollback."""
    import fnmatch

    if not workspace.real_unchanged():
        raise TargetDriftError("real project changed before commit-back")
    for change in changes:
        relative = str(change.get("path", ""))
        _safe_relative(relative)
        if not any(fnmatch.fnmatch(relative, pattern) for pattern in allowed_patterns):
            raise WorkspaceError(f"workspace change is outside allowed scope: {relative}")
        destination = workspace.real_project / relative
        current = tree_fingerprint(workspace.real_project).get(relative)
        if current != change.get("before"):
            raise TargetDriftError(f"target drift at {relative}")

    backups: dict[Path, tuple[bytes, int] | None] = {}
    created_parents: list[Path] = []
    try:
        for change in changes:
            relative = str(change["path"])
            destination = workspace.real_project / _safe_relative(relative)
            if destination.exists() and destination.is_file():
                backups[destination] = (destination.read_bytes(), stat.S_IMODE(destination.stat().st_mode))
            elif destination.is_symlink():
                backups[destination] = (os.readlink(destination).encode(), stat.S_IMODE(destination.lstat().st_mode))
            else:
                backups[destination] = None

        for change in changes:
            relative = str(change["path"])
            destination = workspace.real_project / _safe_relative(relative)
            source = workspace.path / relative
            status = change["status"]
            if status == "DELETED":
                if destination.is_dir() and not destination.is_symlink():
                    raise WorkspaceError(f"cannot delete directory through file transaction: {relative}")
                if destination.exists() or destination.is_symlink():
                    destination.unlink()
                continue
            if not source.is_file() or source.is_symlink():
                raise WorkspaceError(f"workspace output is not a regular file: {relative}")
            if destination.exists() and destination.is_dir() and not destination.is_symlink():
                raise WorkspaceError(f"target is a directory: {relative}")
            if not destination.parent.exists():
                missing: list[Path] = []
                parent = destination.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for item in reversed(missing):
                    item.mkdir()
                    created_parents.append(item)
            fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".cwo-tmp", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(source.read_bytes())
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            os.chmod(destination, stat.S_IMODE(source.stat().st_mode))

        expected = dict(workspace.real_baseline.get("tree", {}))
        for change in changes:
            if change["status"] == "DELETED":
                expected.pop(change["path"], None)
            else:
                expected[change["path"]] = change["after"]
        actual = tree_fingerprint(workspace.real_project, workspace.excluded_roots)
        if actual != expected:
            raise WorkspaceError("post-commit-back fingerprint mismatch")
    except Exception:
        for destination, backup in backups.items():
            try:
                if backup is None:
                    if destination.is_file() or destination.is_symlink():
                        destination.unlink()
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.rollback.", dir=destination.parent)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(backup[0])
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, destination)
                    os.chmod(destination, backup[1])
            except OSError:
                pass
        for parent in reversed(created_parents):
            try:
                parent.rmdir()
            except OSError:
                pass
        raise
