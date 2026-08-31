"""Small registry for named local xahaud binaries.

Saved binaries are addressed as ``@name`` on the CLI. The name is the stable
operator-chosen id; the JSON manifest records where the binary came from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from contextlib import ExitStack, contextmanager, suppress
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

_BUILD_LOCK_NAME = ".x-build-lock"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


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


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    """Return the fields that change when a build replaces or rewrites a target."""
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fith_receipt_digest(source: Path) -> tuple[Path, str] | None:
    """Read the digest from an adjacent FITH receipt, failing closed if invalid."""
    receipt = source.with_name(f".{source.name}.fith-receipt.json")
    if not receipt.is_file():
        return None
    try:
        payload = json.loads(receipt.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"refusing to save FITH output with an unreadable receipt: {receipt}"
        ) from exc
    digest = payload.get("binary_sha256") if isinstance(payload, dict) else None
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ValueError(
            f"refusing to save FITH output with an invalid receipt: {receipt}"
        )
    return receipt, digest.lower()


def _reject_matching_fith_receipt(source: Path, candidate: Path) -> None:
    receipt = _fith_receipt_digest(source)
    if receipt is None:
        return
    receipt_path, expected_digest = receipt
    if _file_sha256(candidate) == expected_digest:
        raise ValueError(
            f"refusing to save FITH output as an ordinary alias: {source} "
            f"(receipt: {receipt_path}); run a successful ordinary build first"
        )


def _reject_matching_fith_receipts(sources: tuple[Path, ...], candidate: Path) -> None:
    """Reject a candidate covered by any operator-visible or canonical receipt."""
    for source in sources:
        _reject_matching_fith_receipt(source, candidate)


def _cleanup_failed_save(
    *,
    tmp_dest: Path,
    dest: Path,
    dest_dir: Path,
    alias_dir: Path,
    root: Path,
    dest_dir_created: bool,
    alias_dir_created: bool,
    root_created: bool,
) -> None:
    """Remove only artifacts and parent directories created by a failed save."""
    if dest_dir_created:
        for path in (tmp_dest, dest):
            with suppress(FileNotFoundError):
                path.unlink()
        with suppress(OSError):
            dest_dir.rmdir()
    if alias_dir_created:
        with suppress(OSError):
            alias_dir.rmdir()
    if root_created:
        with suppress(OSError):
            root.rmdir()


def _ensure_directory(path: Path, *, parents: bool = False) -> bool:
    """Ensure ``path`` is a directory and report whether this call created it."""
    try:
        path.mkdir(parents=parents, exist_ok=False)
    except FileExistsError:
        if not path.is_dir():
            raise
        return False
    return True


def _manifest_references_destination(
    name: str, dest: Path, manifest: Path | None
) -> bool:
    """Fail safely when a publication error may have happened after replacement."""
    try:
        data = load_manifest(manifest)
    except Exception:
        # If the manifest cannot be inspected, preserving an unreferenced
        # generation is safer than deleting a potentially active binary.
        return True
    entry = data.get(name)
    return isinstance(entry, dict) and entry.get("path") == str(dest)


@contextmanager
def _build_output_lock(source: Path):
    """Join x-run-tests' persistent build-dir lock when one is present."""
    lock_path = (source.parent / _BUILD_LOCK_NAME).resolve()
    if fcntl is None or not lock_path.is_file():
        yield
        return
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


@contextmanager
def _build_output_locks(source: Path):
    """Join locks beside both the lexical output and its canonical target."""
    canonical_source = source.resolve()
    # Resolve the parent directories before de-duplicating. A lexical build
    # directory may itself be a symlink to the canonical target directory; in
    # that case opening and flocking the same inode twice would self-deadlock.
    lock_parents = {source.parent.resolve(), canonical_source.parent.resolve()}
    with ExitStack() as stack:
        for lock_parent in sorted(lock_parents, key=os.fspath):
            stack.enter_context(_build_output_lock(lock_parent / source.name))
        yield canonical_source


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
    # Preserve the operator-visible output path for adjacent lock/receipt
    # discovery. Resolving a symlink first would look beside its target and
    # could miss ``build/.rippled.fith-receipt.json`` beside ``build/rippled``.
    output_path = Path(os.path.abspath(source.expanduser()))

    generation_ready = False
    try:
        with _build_output_locks(output_path) as locked_canonical_source:
            # Recheck after acquiring the build lock: the output may have changed
            # while this process waited for a build to finish.
            if not output_path.exists():
                raise FileNotFoundError(f"binary not found: {output_path}")
            if not _is_executable_file(output_path):
                raise ValueError(
                    f"binary path is not an executable file: {output_path}"
                )
            canonical_source = output_path.resolve()
            if canonical_source != locked_canonical_source:
                raise OSError(
                    f"binary target changed while waiting for its build lock: {output_path}"
                )
            source_identity = _file_identity(output_path)
            receipt_sources: tuple[Path, ...] = (output_path,)
            if canonical_source != output_path:
                receipt_sources += (canonical_source,)

            # cppt installs the receipt before atomically activating its quick-linked
            # output. Match the receipt's binary digest rather than its mere presence:
            # a failed activation deliberately leaves a safely stale receipt beside
            # the previous ordinary binary. Keep this guard in the registry as well as
            # the x-run-tests CLI so direct API callers cannot launder a FITH artifact.
            _reject_matching_fith_receipts(receipt_sources, output_path)

            root = binary_cache_dir(cache_dir)
            alias_dir = root / name
            saved_at = datetime.now(UTC)
            token = saved_at.strftime("%Y%m%dT%H%M%S%fZ")
            dest_dir = alias_dir / f"{token}-{uuid4().hex[:12]}"
            dest = dest_dir / canonical_source.name
            tmp_dest = dest_dir / f".{canonical_source.name}.{os.getpid()}.tmp"
            root_created = False
            alias_dir_created = False
            dest_dir_created = False
            try:
                root_created = _ensure_directory(root, parents=True)
                alias_dir_created = _ensure_directory(alias_dir)
                dest_dir.mkdir(exist_ok=False)
                dest_dir_created = True
                shutil.copy2(output_path, tmp_dest)
                # Re-read the receipt before the source identity. This ordering catches
                # both FITH's receipt-before-activation protocol and an ordinary build
                # that removes a receipt while replacing the target during this copy.
                current_canonical_source = output_path.resolve()
                current_receipt_sources = receipt_sources
                if current_canonical_source not in current_receipt_sources:
                    current_receipt_sources += (current_canonical_source,)
                _reject_matching_fith_receipts(current_receipt_sources, tmp_dest)
                if (
                    current_canonical_source != canonical_source
                    or _file_identity(output_path) != source_identity
                ):
                    raise OSError(
                        f"binary changed while it was being saved: {output_path}"
                    )
                tmp_dest.replace(dest)
                generation_ready = True
            except BaseException:
                _cleanup_failed_save(
                    tmp_dest=tmp_dest,
                    dest=dest,
                    dest_dir=dest_dir,
                    alias_dir=alias_dir,
                    root=root,
                    dest_dir_created=dest_dir_created,
                    alias_dir_created=alias_dir_created,
                    root_created=root_created,
                )
                raise
            with suppress(FileNotFoundError):
                tmp_dest.unlink()
    except BaseException:
        # Releasing the build lock is still part of the pre-publication phase.
        # If it fails after the copy was activated, do not leak an unreferenced
        # cache generation that can never be reached through the manifest.
        if generation_ready:
            _cleanup_failed_save(
                tmp_dest=tmp_dest,
                dest=dest,
                dest_dir=dest_dir,
                alias_dir=alias_dir,
                root=root,
                dest_dir_created=dest_dir_created,
                alias_dir_created=alias_dir_created,
                root_created=root_created,
            )
        raise

    published = False
    try:
        worktree_path = _git_root(worktree or output_path.parent)
        entry = SavedBinary(
            name=name,
            path=dest,
            saved_at=saved_at.isoformat().replace("+00:00", "Z"),
            source_path=canonical_source,
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
            try:
                write_manifest(data, manifest)
            except BaseException:
                # Inspect while still holding the manifest lock. Once this
                # generation has crossed the atomic publication boundary it
                # must survive, even if unlock fails or another publisher
                # supersedes the alias before this caller handles the error.
                published = _manifest_references_destination(name, dest, manifest)
                raise
            else:
                published = True
    except BaseException:
        if not published:
            _cleanup_failed_save(
                tmp_dest=tmp_dest,
                dest=dest,
                dest_dir=dest_dir,
                alias_dir=alias_dir,
                root=root,
                dest_dir_created=dest_dir_created,
                alias_dir_created=alias_dir_created,
                root_created=root_created,
            )
        raise
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
