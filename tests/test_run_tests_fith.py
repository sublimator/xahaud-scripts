from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from xahaud_scripts.ordinary_build_receipt import OrdinaryBuildReceiptError
from xahaud_scripts.run_tests import (
    FITH_BETA_ENV,
    build_rippled,
    env_flag_enabled,
    fith_enabled,
    run_fith_quick_build,
)
from xahaud_scripts.run_tests import (
    main as run_tests_main,
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


@pytest.mark.parametrize(
    ("extra_args", "env", "enabled_by"),
    [
        (["--fith"], {}, "--fith"),
        ([], {FITH_BETA_ENV: "1"}, f"{FITH_BETA_ENV}=1"),
    ],
)
def test_fith_binary_cannot_be_saved_without_provenance(
    extra_args: list[str],
    env: dict[str, str],
    enabled_by: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_root_probe() -> str:
        raise AssertionError("FITH/save validation must happen before build setup")

    monkeypatch.setattr(
        "xahaud_scripts.run_tests.get_xahaud_root", unexpected_root_probe
    )

    result = CliRunner().invoke(
        run_tests_main,
        [*extra_args, "--save-binary", "@candidate", "--times=0"],
        env=env,
    )

    assert result.exit_code != 0
    assert "--save-binary cannot be used while FITH is enabled" in result.output
    assert enabled_by in result.output
    assert "Use --no-fith" in result.output


def test_run_fith_quick_build_invokes_cppt_with_target(
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

    assert run_fith_quick_build(
        xahaud_root=str(root),
        build_dir=str(build_dir),
        base="origin/dev",
        strict=True,
        dry_run=False,
        jobs=8,
        target="rippled",
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
                "link-target=rippled",
                f"ordinary-baseline-receipt={build_dir / '.rippled.ordinary-build-receipt.json'}",
                "jobs=8",
            ],
            tee_file,
        )
    ]


def test_run_fith_quick_build_fails_without_compile_database(
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

    assert not run_fith_quick_build(
        xahaud_root=str(root),
        build_dir=str(build_dir),
        base="HEAD",
        strict=True,
        dry_run=False,
        jobs=None,
        target="rippled",
    )
    assert calls == []


def test_build_uses_fith_instead_of_ordinary_target_build(
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
        "xahaud_scripts.run_tests.run_fith_quick_build",
        lambda **kwargs: calls.append(
            f"fith:strict={str(kwargs['strict']).lower()}:target={kwargs['target']}"
        )
        or True,
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.cmake_build",
        lambda *_args, **_kwargs: calls.append("build") or True,
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.recompact_ninja_dbs",
        lambda _build_dir: calls.append("recompact"),
    )

    assert build_rippled(build_dir=str(build_dir), use_fith=True)
    assert calls == ["check", "fith:strict=false:target=rippled", "recompact"]


def _write_fake_binary(path: Path, version: str = "xahaud-test 1.2.3") -> None:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _stub_ordinary_build(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr("xahaud_scripts.run_tests.get_xahaud_root", lambda: str(root))
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.conan_toolchain_present", lambda _build_dir: True
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.check_config_mismatch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.cmake_build", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.recompact_ninja_dbs", lambda _build_dir: None
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.capture_ordinary_build_start",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.publish_ordinary_build_receipt",
        lambda _start: root / "build" / ".rippled.ordinary-build-receipt.json",
    )


def test_ordinary_build_keeps_matching_fith_receipt_and_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "CMakeCache.txt").write_text("# configured\n")
    binary = build_dir / "rippled"
    _write_fake_binary(binary)
    receipt = build_dir / ".rippled.fith-receipt.json"
    payload = {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    receipt.write_text(json.dumps(payload) + "\n")
    _stub_ordinary_build(monkeypatch, root)

    assert not build_rippled(build_dir=str(build_dir), use_fith=False)
    assert json.loads(receipt.read_text()) == payload


def test_ordinary_build_refuses_invalid_fith_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "CMakeCache.txt").write_text("# configured\n")
    _write_fake_binary(build_dir / "rippled")
    receipt = build_dir / ".rippled.fith-receipt.json"
    receipt.write_text("{}\n")
    _stub_ordinary_build(monkeypatch, root)

    assert not build_rippled(build_dir=str(build_dir), use_fith=False)
    assert receipt.exists()


def test_ordinary_build_removes_mismatched_fith_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "CMakeCache.txt").write_text("# configured\n")
    _write_fake_binary(build_dir / "rippled")
    receipt = build_dir / ".rippled.fith-receipt.json"
    receipt.write_text(json.dumps({"binary_sha256": "0" * 64}) + "\n")
    _stub_ordinary_build(monkeypatch, root)

    assert build_rippled(build_dir=str(build_dir), use_fith=False)
    assert not receipt.exists()


def test_ordinary_build_publishes_baseline_after_successful_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "CMakeCache.txt").write_text("# configured\n")
    binary = build_dir / "rippled"
    _write_fake_binary(binary)
    calls: list[str] = []
    baseline_start = object()

    monkeypatch.setattr("xahaud_scripts.run_tests.get_xahaud_root", lambda: str(root))
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.check_config_mismatch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.capture_ordinary_build_start",
        lambda *_args: calls.append("capture") or baseline_start,
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.cmake_build",
        lambda *_args, **_kwargs: calls.append("build") or True,
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.recompact_ninja_dbs",
        lambda _build_dir: calls.append("recompact"),
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.discard_stale_fith_receipts",
        lambda output: calls.append(f"reconcile:{output.name}"),
    )

    def publish(start: object) -> Path:
        assert start is baseline_start
        calls.append("publish")
        return build_dir / ".rippled.ordinary-build-receipt.json"

    monkeypatch.setattr(
        "xahaud_scripts.run_tests.publish_ordinary_build_receipt", publish
    )

    assert build_rippled(
        build_dir=str(build_dir),
        use_conan=False,
        use_fith=False,
    )
    assert calls == ["capture", "build", "recompact", "reconcile:rippled", "publish"]


def test_failed_ordinary_build_does_not_replace_previous_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "CMakeCache.txt").write_text("# configured\n")
    receipt = build_dir / ".rippled.ordinary-build-receipt.json"
    previous = b'{"previous":true}\n'
    receipt.write_bytes(previous)

    monkeypatch.setattr("xahaud_scripts.run_tests.get_xahaud_root", lambda: str(root))
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.check_config_mismatch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.capture_ordinary_build_start",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.cmake_build", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.publish_ordinary_build_receipt",
        lambda _start: pytest.fail("failed build must not publish a baseline"),
    )

    assert not build_rippled(
        build_dir=str(build_dir),
        use_conan=False,
        use_fith=False,
    )
    assert receipt.read_bytes() == previous


def test_interrupted_ordinary_build_does_not_replace_previous_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "CMakeCache.txt").write_text("# configured\n")
    receipt = build_dir / ".rippled.ordinary-build-receipt.json"
    previous = b'{"previous":true}\n'
    receipt.write_bytes(previous)

    monkeypatch.setattr("xahaud_scripts.run_tests.get_xahaud_root", lambda: str(root))
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.check_config_mismatch", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.capture_ordinary_build_start",
        lambda *_args: object(),
    )

    def interrupt(*_args: object, **_kwargs: object) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr("xahaud_scripts.run_tests.cmake_build", interrupt)
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.publish_ordinary_build_receipt",
        lambda _start: pytest.fail("interrupted build must not publish a baseline"),
    )

    with pytest.raises(KeyboardInterrupt):
        build_rippled(
            build_dir=str(build_dir),
            use_conan=False,
            use_fith=False,
        )
    assert receipt.read_bytes() == previous


def test_baseline_publication_failure_requires_another_ordinary_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    (build_dir / "CMakeCache.txt").write_text("# configured\n")
    _write_fake_binary(build_dir / "rippled")
    _stub_ordinary_build(monkeypatch, root)

    def refuse(_start: object) -> Path:
        raise OrdinaryBuildReceiptError("Git HEAD changed during ordinary build")

    monkeypatch.setattr(
        "xahaud_scripts.run_tests.publish_ordinary_build_receipt", refuse
    )

    assert not build_rippled(
        build_dir=str(build_dir),
        use_conan=False,
        use_fith=False,
    )
    assert "run one full ordinary build again" in caplog.text
