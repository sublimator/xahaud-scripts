from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from xahaud_scripts.ordinary_build_receipt import (
    OrdinaryBuildReceiptError,
    capture_ordinary_build_start,
    ordinary_build_receipt_path,
    publish_ordinary_build_receipt,
)


def _git(repo: Path, *args: str) -> str:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _make_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Receipt Test")
    _git(root, "config", "user.email", "receipt@example.invalid")
    (root / "source.cpp").write_text("int main() { return 0; }\n")
    _git(root, "add", "source.cpp")
    _git(root, "commit", "-m", "baseline")

    build = root / "build"
    build.mkdir()
    (build / "compile_commands.json").write_text("[]\n")
    (build / "build.ninja").write_text("# ninja\n")
    (build / "CMakeCache.txt").write_text("# cache\n")
    target = build / "rippled"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return root, build, target


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_publish_ordinary_build_receipt_matches_consumer_schema(
    tmp_path: Path,
) -> None:
    root, build, target = _make_workspace(tmp_path)

    started = capture_ordinary_build_start(root, build, "rippled")
    receipt = publish_ordinary_build_receipt(started)
    payload = json.loads(receipt.read_text())

    reflog_name = _git(root, "rev-parse", "--git-path", "logs/HEAD")
    reflog = Path(reflog_name)
    if not reflog.is_absolute():
        reflog = root / reflog
    branch = _git(root, "symbolic-ref", "HEAD")
    assert receipt == build / ".rippled.ordinary-build-receipt.json"
    assert ordinary_build_receipt_path(build, "rippled") == receipt
    assert set(payload) == {
        "schema_version",
        "kind",
        "workspace_realpath",
        "build_dir_realpath",
        "target",
        "git",
        "build",
        "started_at",
        "completed_at",
        "producer",
    }
    assert payload["schema_version"] == 1
    assert payload["kind"] == "xahaud-scripts.ordinary-build"
    assert payload["workspace_realpath"] == str(root.resolve())
    assert payload["build_dir_realpath"] == str(build.resolve())
    assert payload["target"] == {
        "requested": "rippled",
        "path": str(target.resolve()),
        "sha256": _sha256(target),
    }
    assert payload["git"] == {
        "head": _git(root, "rev-parse", "HEAD"),
        "branch": branch,
        "head_reflog": {
            "prefix_bytes": reflog.stat().st_size,
            "prefix_sha256": _sha256(reflog),
        },
    }
    assert payload["build"] == {
        "compile_commands_sha256": _sha256(build / "compile_commands.json"),
        "build_ninja_sha256": _sha256(build / "build.ninja"),
        "cmake_cache_sha256": _sha256(build / "CMakeCache.txt"),
    }
    assert payload["producer"]["name"] == "xahaud-scripts"
    assert payload["producer"]["version"]
    started_at = datetime.fromisoformat(payload["started_at"].replace("Z", "+00:00"))
    completed_at = datetime.fromisoformat(
        payload["completed_at"].replace("Z", "+00:00")
    )
    assert completed_at >= started_at
    assert receipt.read_bytes().endswith(b"\n")


def test_git_transition_refuses_publication_and_preserves_previous_receipt(
    tmp_path: Path,
) -> None:
    root, build, _target = _make_workspace(tmp_path)
    receipt = ordinary_build_receipt_path(build, "rippled")
    previous = b'{"previous":true}\n'
    receipt.write_bytes(previous)
    started = capture_ordinary_build_start(root, build, "rippled")

    _git(root, "switch", "-c", "other")

    with pytest.raises(
        OrdinaryBuildReceiptError,
        match="changed during ordinary build",
    ):
        publish_ordinary_build_receipt(started)
    assert receipt.read_bytes() == previous


def test_switch_away_and_back_refuses_from_reflog_and_preserves_previous_receipt(
    tmp_path: Path,
) -> None:
    root, build, _target = _make_workspace(tmp_path)
    receipt = ordinary_build_receipt_path(build, "rippled")
    previous = b'{"previous":true}\n'
    receipt.write_bytes(previous)
    original_head = _git(root, "rev-parse", "HEAD")
    original_branch = _git(root, "symbolic-ref", "--short", "HEAD")
    started = capture_ordinary_build_start(root, build, "rippled")

    _git(root, "switch", "-c", "temporary-branch")
    _git(root, "switch", original_branch)

    assert _git(root, "rev-parse", "HEAD") == original_head
    assert _git(root, "symbolic-ref", "--short", "HEAD") == original_branch
    with pytest.raises(
        OrdinaryBuildReceiptError,
        match="changed during ordinary build",
    ):
        publish_ordinary_build_receipt(started)
    assert receipt.read_bytes() == previous


def test_git_identity_ignores_git_dir_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, build, _target = _make_workspace(tmp_path)
    poison = tmp_path / "poison"
    poison.mkdir()
    _git(poison, "init")
    _git(poison, "config", "user.name", "Poison")
    _git(poison, "config", "user.email", "poison@example.invalid")
    (poison / "other.cpp").write_text("int x;\n")
    _git(poison, "add", "other.cpp")
    _git(poison, "commit", "-m", "poison")
    workspace_head = _git(root, "rev-parse", "HEAD")
    workspace_branch = _git(root, "symbolic-ref", "HEAD")
    poison_head = _git(poison, "rev-parse", "HEAD")
    assert workspace_head != poison_head

    monkeypatch.setenv("GIT_DIR", str((poison / ".git").resolve()))
    monkeypatch.setenv("GIT_WORK_TREE", str(poison.resolve()))
    started = capture_ordinary_build_start(root, build, "rippled")
    receipt = publish_ordinary_build_receipt(started)
    payload = json.loads(receipt.read_text())

    assert payload["workspace_realpath"] == str(root.resolve())
    assert payload["git"]["head"] == workspace_head
    assert payload["git"]["branch"] == workspace_branch
    assert payload["git"]["head"] != poison_head


def test_publish_failure_preserves_previous_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, build, _target = _make_workspace(tmp_path)
    receipt = ordinary_build_receipt_path(build, "rippled")
    previous = b'{"previous":true}\n'
    receipt.write_bytes(previous)
    started = capture_ordinary_build_start(root, build, "rippled")

    def fail_replace(_source: str, _destination: Path) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(
        "xahaud_scripts.ordinary_build_receipt.os.replace",
        fail_replace,
    )
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        publish_ordinary_build_receipt(started)
    assert receipt.read_bytes() == previous
    assert list(build.glob(".*.tmp")) == []
