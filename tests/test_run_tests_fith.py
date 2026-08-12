from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from xahaud_scripts.run_tests import (
    FITH_BETA_ENV,
    build_rippled,
    env_flag_enabled,
    fith_enabled,
    run_fith_preflight,
)


def test_fith_requires_beta_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FITH_BETA_ENV, raising=False)
    assert not env_flag_enabled(FITH_BETA_ENV)
    assert not fith_enabled(None)
    assert fith_enabled(True)
    assert not fith_enabled(False)

    for value in ["", "0", "false", "no", "off"]:
        monkeypatch.setenv(FITH_BETA_ENV, value)
        assert not env_flag_enabled(FITH_BETA_ENV)
        assert not fith_enabled(None)

    monkeypatch.setenv(FITH_BETA_ENV, "1")
    assert env_flag_enabled(FITH_BETA_ENV)
    assert fith_enabled(None)
    assert fith_enabled(True)
    assert not fith_enabled(False)


def test_run_fith_preflight_invokes_cppt_with_build_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    compile_commands = build_dir / "compile_commands.json"
    compile_commands.write_text("[]")
    tee_file = tmp_path / "tee.txt"
    calls: list[tuple[list[str], Path | None]] = []

    monkeypatch.setattr(
        "xahaud_scripts.run_tests.shutil.which",
        lambda name: "/fake/cppt" if name == "cppt" else None,
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.run_command",
        lambda cmd, tee_file=None: calls.append((cmd, tee_file))
        or subprocess.CompletedProcess(cmd, 0),
    )

    assert run_fith_preflight(
        xahaud_root=str(root),
        build_dir=str(build_dir),
        base="origin/dev",
        strict=True,
        dry_run=False,
        jobs=8,
        tee_file=tee_file,
    )

    assert calls == [
        (
            [
                "/fake/cppt",
                "beta",
                "fith",
                "--workspace",
                str(root),
                "--compile-commands",
                str(compile_commands),
                "base=origin/dev",
                "strict=true",
                "jobs=8",
            ],
            tee_file,
        )
    ]


def test_run_fith_preflight_fails_closed_without_compile_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    calls: list[str] = []
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.shutil.which",
        lambda name: calls.append(name) or "/fake/cppt",
    )

    assert not run_fith_preflight(
        xahaud_root=str(root),
        build_dir=str(build_dir),
        base="HEAD",
        strict=True,
        dry_run=False,
        jobs=None,
    )
    assert calls == []


def test_build_runs_fith_before_ordinary_target_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "CMakeCache.txt").write_text("# configured\n")
    calls: list[str] = []

    monkeypatch.setattr("xahaud_scripts.run_tests.get_xahaud_root", lambda: str(root))
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.conan_toolchain_present", lambda _build_dir: True
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.check_config_mismatch",
        lambda **_kwargs: calls.append("check"),
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.run_fith_preflight",
        lambda **_kwargs: calls.append("fith") or True,
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.cmake_build",
        lambda *_args, **_kwargs: calls.append("build") or True,
    )

    assert build_rippled(build_dir=str(build_dir), use_fith=True)
    assert calls == ["check", "fith", "build"]
