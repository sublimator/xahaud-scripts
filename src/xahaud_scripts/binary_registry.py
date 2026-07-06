"""Small registry for named local xahaud binaries.

Saved binaries are addressed as ``@name`` on the CLI. The name is the stable
operator-chosen id; the JSON manifest records where the binary came from.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

APP_DIR_NAME = "xahaud-scripts"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class SavedBinary:
    """One saved binary manifest entry."""

    name: str
    path: Path
    saved_at: str
    source_path: Path
    worktree: Path | None
    branch: str | None
    commit: str | None
    dirty: bool | None
    git_describe: str | None
    build_type: str | None
    version: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "saved_at": self.saved_at,
            "source_path": str(self.source_path),
            "worktree": str(self.worktree) if self.worktree else None,
            "branch": self.branch,
            "commit": self.commit,
            "dirty": self.dirty,
            "git_describe": self.git_describe,
            "build_type": self.build_type,
            "version": self.version,
        }


def is_binary_alias(spec: str | Path | None) -> bool:
    """Return whether a CLI value is a saved-binary alias."""
    return spec is not None and str(spec).startswith("@")


def alias_name(alias: str | Path) -> str:
    """Validate and return the manifest key for an ``@name`` alias."""
    value = str(alias)
    if not value.startswith("@"):
        raise ValueError("saved binary aliases must start with @")
    name = value[1:]
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            "saved binary alias must look like @name using letters, digits, '.', '_' or '-'"
        )
    return name


def config_dir() -> Path:
    """Return the config dir, honoring XDG_CONFIG_HOME when set."""
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".config") / APP_DIR_NAME


def cache_dir() -> Path:
    """Return the cache dir, honoring XDG_CACHE_HOME when set."""
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".cache") / APP_DIR_NAME


def manifest_path(path: Path | None = None) -> Path:
    return path or config_dir() / "binaries.json"


def binary_cache_dir(path: Path | None = None) -> Path:
    return path or cache_dir() / "binaries"


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """Load the saved-binary manifest, returning an empty manifest if missing."""
    resolved = manifest_path(path)
    if not resolved.exists():
        return {}
    data = json.loads(resolved.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{resolved}: expected JSON object")
    return data


def write_manifest(data: dict[str, Any], path: Path | None = None) -> None:
    """Write the saved-binary manifest atomically enough for local CLI use."""
    resolved = manifest_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    tmp = resolved.with_name(f".{resolved.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(resolved)


def resolve_binary_alias(alias: str | Path, *, manifest: Path | None = None) -> Path:
    """Resolve ``@name`` to a saved binary path."""
    name = alias_name(alias)
    data = load_manifest(manifest)
    entry = data.get(name)
    if not isinstance(entry, dict) or not entry.get("path"):
        raise FileNotFoundError(f"saved binary @{name} not found")
    path = Path(str(entry["path"])).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"saved binary @{name} path does not exist: {path}")
    if not _is_executable_file(path):
        raise PermissionError(f"saved binary @{name} path is not executable: {path}")
    return path


def resolve_binary_spec(spec: str | Path) -> Path:
    """Resolve a CLI binary spec if it is ``@name``; otherwise return a Path."""
    return resolve_binary_alias(spec) if is_binary_alias(spec) else Path(spec)


def save_binary(
    alias: str,
    source: Path,
    *,
    worktree: Path | None = None,
    build_type: str | None = None,
    manifest: Path | None = None,
    cache_dir: Path | None = None,
) -> SavedBinary:
    """Copy ``source`` into the saved-binary cache and update the manifest."""
    name = alias_name(alias)
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"binary not found: {source}")
    if not _is_executable_file(source):
        raise ValueError(f"binary path is not an executable file: {source}")

    root = binary_cache_dir(cache_dir)
    saved_at = datetime.now(UTC)
    token = saved_at.strftime("%Y%m%dT%H%M%S%fZ")
    dest_dir = root / name / f"{token}-{uuid4().hex[:12]}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    tmp_dest = dest_dir / f".{source.name}.{os.getpid()}.tmp"
    try:
        shutil.copy2(source, tmp_dest)
        tmp_dest.replace(dest)
    finally:
        if tmp_dest.exists():
            tmp_dest.unlink()

    worktree_path = _git_root(worktree or source.parent)
    entry = SavedBinary(
        name=name,
        path=dest,
        saved_at=saved_at.isoformat().replace("+00:00", "Z"),
        source_path=source,
        worktree=worktree_path,
        branch=_git(worktree_path, "branch", "--show-current")
        if worktree_path
        else None,
        commit=_git(worktree_path, "rev-parse", "HEAD") if worktree_path else None,
        dirty=_git_dirty(worktree_path) if worktree_path else None,
        git_describe=_git(
            worktree_path,
            "describe",
            "--tags",
            "--always",
            "--dirty",
        )
        if worktree_path
        else None,
        build_type=build_type,
        version=_binary_version(dest),
    )

    with _manifest_lock(manifest):
        data = load_manifest(manifest)
        data[name] = entry.as_dict()
        write_manifest(data, manifest)
    return entry


def _git_root(path: Path) -> Path | None:
    probe = path if path.is_dir() else path.parent
    result = _run_git(probe, "rev-parse", "--show-toplevel")
    return Path(result) if result else None


def _git(repo: Path | None, *args: str) -> str | None:
    if repo is None:
        return None
    return _run_git(repo, *args)


def _git_dirty(repo: Path | None) -> bool | None:
    if repo is None:
        return None
    result = _run_git(repo, "status", "--porcelain")
    return bool(result) if result is not None else None


def _run_git(cwd: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


@contextmanager
def _manifest_lock(path: Path | None = None):
    """Serialize manifest read/modify/write updates across local processes."""
    resolved = manifest_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    lock_path = resolved.with_name(f".{resolved.name}.lock")
    with lock_path.open("a") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _binary_version(path: Path) -> str | None:
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=3,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    return text.splitlines()[0] if text else None
