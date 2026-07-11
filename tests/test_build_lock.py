"""Build-dir lock + ninja-db recompaction (incident 2026-07-11).

Overlapping or killed ninja invocations corrupt .ninja_deps; ninja reads the
valid prefix but never repairs the file, so every later build replays the
full compile graph. The lock prevents overlap; recompaction caps kill-mid-
write damage at a single rebuild.
"""

from __future__ import annotations

import fcntl
from pathlib import Path

from xahaud_scripts.run_tests import (
    BUILD_LOCK_NAME,
    build_dir_lock,
    recompact_ninja_dbs,
)


def test_lock_is_exclusive_while_held(tmp_path: Path) -> None:
    with build_dir_lock(tmp_path):
        lock_file = tmp_path / BUILD_LOCK_NAME
        assert lock_file.exists()
        # A second, non-blocking acquisition attempt must fail while held.
        with open(lock_file) as second:
            try:
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise AssertionError("lock was not exclusive")
            except BlockingIOError:
                pass

    # Released after the context exits: non-blocking acquisition succeeds.
    with open(tmp_path / BUILD_LOCK_NAME) as third:
        fcntl.flock(third, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(third, fcntl.LOCK_UN)


def test_lock_records_holder(tmp_path: Path) -> None:
    with build_dir_lock(tmp_path):
        content = (tmp_path / BUILD_LOCK_NAME).read_text()
        assert content.startswith("pid ")


def test_recompact_skips_non_ninja_dir(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.subprocess.run",
        lambda cmd, **kwargs: calls.append(cmd),
    )
    recompact_ninja_dbs(tmp_path)  # no build.ninja present
    assert calls == []


def test_recompact_runs_ninja_tool(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "build.ninja").write_text("# fake\n")

    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        "xahaud_scripts.run_tests.shutil.which", lambda name: "/fake/ninja"
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.subprocess.run",
        lambda cmd, **kwargs: calls.append(cmd) or Result(),
    )
    recompact_ninja_dbs(tmp_path)
    assert calls == [["/fake/ninja", "-C", str(tmp_path), "-t", "recompact"]]
