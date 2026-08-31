"""Ordinary-build baseline receipts consumed by cpp-tools FITH.

The receipt proves that the wrapper completed one ordinary build without a
Git HEAD/ref/reflog transition and records the CMake/Ninja graph identity at
that point. It deliberately does not claim that later direct Ninja/CMake
commands left the build directory untouched; cpp-tools remains responsible
for classifying the graph it observes at each FITH invocation.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


class OrdinaryBuildReceiptError(ValueError):
    """An ordinary build could not publish trustworthy baseline evidence."""


@dataclass(frozen=True)
class _GitObservation:
    head: str
    branch: str | None
    reflog_path: Path
    reflog_bytes: int
    reflog_sha256: str


@dataclass(frozen=True)
class _BuildGraphObservation:
    compile_commands_sha256: str
    build_ninja_sha256: str
    cmake_cache_sha256: str


@dataclass(frozen=True)
class OrdinaryBuildStart:
    """Git/build identity captured before an ordinary wrapper build starts."""

    workspace: Path
    build_dir: Path
    target: str
    git: _GitObservation
    started_at: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _package_version() -> str:
    try:
        return version("xahaud-scripts")
    except PackageNotFoundError:
        return "0.1.0+unknown"


def _target_path(build_dir: Path, target: str) -> Path:
    output = Path(target)
    return output if output.is_absolute() else build_dir / output


def ordinary_build_receipt_path(build_dir: str | Path, target: str) -> Path:
    """Return the canonical target-adjacent ordinary-build receipt path."""
    base = Path(build_dir).expanduser().resolve()
    output = _target_path(base, target).expanduser().resolve()
    return output.with_name(f".{output.name}.ordinary-build-receipt.json")


def _stable_file_sha256(
    path: Path,
    *,
    expected_bytes: int | None = None,
) -> tuple[Path, int, str]:
    try:
        resolved = path.resolve(strict=True)
        before = resolved.stat()
        if not stat.S_ISREG(before.st_mode):
            raise OrdinaryBuildReceiptError(f"not a regular file: {resolved}")
        if expected_bytes is not None and before.st_size != expected_bytes:
            raise OrdinaryBuildReceiptError(
                f"file length changed: {resolved} has {before.st_size} bytes, "
                f"expected {expected_bytes}"
            )
        digest = hashlib.sha256()
        bytes_read = 0
        with resolved.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                bytes_read += len(chunk)
            opened_after = os.fstat(stream.fileno())
        after = resolved.stat()
    except OSError as exc:
        raise OrdinaryBuildReceiptError(
            f"cannot read stable file {path}: {exc}"
        ) from exc

    signatures = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, opened_before, opened_after, after)
    }
    if len(signatures) != 1 or bytes_read != before.st_size:
        raise OrdinaryBuildReceiptError(f"file changed while it was read: {resolved}")
    return resolved, bytes_read, digest.hexdigest()


def _controlled_git_environment() -> dict[str, str]:
    """Observe the workspace repository, not GIT_* overrides.

    cpp-tools FITH (4366ddd) strips every GIT_* key and pins config/locks
    before reading HEAD/ref/reflog. The writer must use the same identity or
    a receipt can bind this workspace to another repository's Git state.
    """
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        env=_controlled_git_environment(),
    )
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise OrdinaryBuildReceiptError(
            f"git {' '.join(args)} failed with exit status {result.returncode}: {message}"
        )
    return result.stdout


def _current_branch(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "symbolic-ref", "--quiet", "HEAD"],
        check=False,
        capture_output=True,
        env=_controlled_git_environment(),
    )
    if result.returncode == 1:
        return None
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise OrdinaryBuildReceiptError(
            "git symbolic-ref --quiet HEAD failed with exit status "
            f"{result.returncode}: {message}"
        )
    return result.stdout.decode(errors="surrogateescape").strip()


def _head_reflog_path(root: Path) -> Path:
    value = _git_bytes(root, "rev-parse", "--git-path", "logs/HEAD").decode(
        errors="surrogateescape"
    )
    path = Path(value.strip())
    return path if path.is_absolute() else root / path


def _observe_git(root: Path) -> _GitObservation:
    head_before = (
        _git_bytes(root, "rev-parse", "--verify", "HEAD^{commit}")
        .decode(errors="strict")
        .strip()
    )
    branch_before = _current_branch(root)
    reflog_path = _head_reflog_path(root)
    resolved_reflog, reflog_bytes, reflog_sha256 = _stable_file_sha256(reflog_path)
    head_after = (
        _git_bytes(root, "rev-parse", "--verify", "HEAD^{commit}")
        .decode(errors="strict")
        .strip()
    )
    branch_after = _current_branch(root)
    reflog_after = _stable_file_sha256(
        _head_reflog_path(root),
        expected_bytes=reflog_bytes,
    )
    if (head_before, branch_before) != (head_after, branch_after):
        raise OrdinaryBuildReceiptError(
            "Git HEAD or symbolic branch changed while baseline evidence was read"
        )
    if reflog_after != (resolved_reflog, reflog_bytes, reflog_sha256):
        raise OrdinaryBuildReceiptError(
            "per-worktree HEAD reflog changed while baseline evidence was read"
        )
    return _GitObservation(
        head=head_after,
        branch=branch_after,
        reflog_path=resolved_reflog,
        reflog_bytes=reflog_bytes,
        reflog_sha256=reflog_sha256,
    )


def _observe_build_graph(build_dir: Path) -> _BuildGraphObservation:
    return _BuildGraphObservation(
        compile_commands_sha256=_stable_file_sha256(
            build_dir / "compile_commands.json"
        )[2],
        build_ninja_sha256=_stable_file_sha256(build_dir / "build.ninja")[2],
        cmake_cache_sha256=_stable_file_sha256(build_dir / "CMakeCache.txt")[2],
    )


def capture_ordinary_build_start(
    workspace: str | Path,
    build_dir: str | Path,
    target: str,
) -> OrdinaryBuildStart:
    """Capture the state that must remain stable across an ordinary build."""
    root = Path(workspace).expanduser().resolve(strict=True)
    build = Path(build_dir).expanduser().resolve(strict=True)
    return OrdinaryBuildStart(
        workspace=root,
        build_dir=build,
        target=target,
        git=_observe_git(root),
        started_at=_utc_now(),
    )


def _receipt_payload(
    start: OrdinaryBuildStart,
    *,
    git: _GitObservation,
    graph: _BuildGraphObservation,
    target_path: Path,
    target_sha256: str,
    completed_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "xahaud-scripts.ordinary-build",
        "workspace_realpath": str(start.workspace),
        "build_dir_realpath": str(start.build_dir),
        "target": {
            "requested": start.target,
            "path": str(target_path),
            "sha256": target_sha256,
        },
        "git": {
            "head": git.head,
            "branch": git.branch,
            "head_reflog": {
                "prefix_bytes": git.reflog_bytes,
                "prefix_sha256": git.reflog_sha256,
            },
        },
        "build": {
            "compile_commands_sha256": graph.compile_commands_sha256,
            "build_ninja_sha256": graph.build_ninja_sha256,
            "cmake_cache_sha256": graph.cmake_cache_sha256,
        },
        "started_at": start.started_at,
        "completed_at": completed_at,
        "producer": {"name": "xahaud-scripts", "version": _package_version()},
    }


def publish_ordinary_build_receipt(start: OrdinaryBuildStart) -> Path:
    """Atomically publish a successful ordinary-build baseline receipt.

    The caller must still hold the build-directory lock and must call this only
    after the ordinary target succeeded and FITH artifact provenance was
    reconciled. Any failed/interrupted build therefore leaves the prior receipt
    untouched.
    """
    git = _observe_git(start.workspace)
    if git != start.git:
        raise OrdinaryBuildReceiptError(
            "Git HEAD, symbolic branch, or HEAD reflog changed during ordinary build"
        )
    graph = _observe_build_graph(start.build_dir)
    output = _target_path(start.build_dir, start.target).resolve(strict=True)
    if not output.is_file():
        raise OrdinaryBuildReceiptError(f"ordinary target is not a file: {output}")
    resolved_output, output_bytes, output_sha256 = _stable_file_sha256(output)
    completed_at = _utc_now()
    payload = _receipt_payload(
        start,
        git=git,
        graph=graph,
        target_path=output,
        target_sha256=output_sha256,
        completed_at=completed_at,
    )
    receipt = output.with_name(f".{output.name}.ordinary-build-receipt.json")
    receipt.parent.mkdir(parents=True, exist_ok=True)

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=receipt.parent,
            prefix=f".{receipt.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        if _observe_git(start.workspace) != git:
            raise OrdinaryBuildReceiptError(
                "Git baseline changed before ordinary-build receipt publication"
            )
        if _observe_build_graph(start.build_dir) != graph:
            raise OrdinaryBuildReceiptError(
                "CMake/Ninja graph changed before ordinary-build receipt publication"
            )
        if _stable_file_sha256(output, expected_bytes=output_bytes) != (
            resolved_output,
            output_bytes,
            output_sha256,
        ):
            raise OrdinaryBuildReceiptError(
                "ordinary target changed before ordinary-build receipt publication"
            )
        os.replace(temporary_name, receipt)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return receipt
