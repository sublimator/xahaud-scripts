from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from xahaud_scripts.binary_registry import (
    alias_name,
    binary_cache_dir,
    cache_dir,
    config_dir,
    is_binary_alias,
    load_manifest,
    manifest_path,
    resolve_binary_alias,
    resolve_binary_spec,
    save_binary,
)
from xahaud_scripts.run_tests import build_rippled, find_rippled_binary
from xahaud_scripts.run_tests import main as run_tests_main


def _write_fake_binary(path: Path, version: str = "xahaud-test 1.2.3") -> None:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_failing_binary(path: Path) -> None:
    path.write_text("#!/bin/sh\nprintf '%s\\n' 'usage: no version here' >&2\nexit 2\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_alias_name_requires_at_prefix() -> None:
    assert alias_name("@release-3350") == "release-3350"
    assert is_binary_alias("@release-3350")
    assert is_binary_alias(Path("@release-3350"))
    assert not is_binary_alias("/tmp/rippled")

    with pytest.raises(ValueError, match="start with @"):
        alias_name("release-3350")

    with pytest.raises(ValueError, match="letters, digits"):
        alias_name("@bad/name")


def test_registry_paths_honor_xdg_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-root"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-root"))

    assert config_dir() == tmp_path / "config-root" / "xahaud-scripts"
    assert cache_dir() == tmp_path / "cache-root" / "xahaud-scripts"
    assert (
        manifest_path() == tmp_path / "config-root" / "xahaud-scripts" / "binaries.json"
    )
    assert binary_cache_dir() == tmp_path / "cache-root" / "xahaud-scripts" / "binaries"


def test_save_binary_copies_and_writes_manifest(tmp_path: Path) -> None:
    source = tmp_path / "build" / "rippled"
    source.parent.mkdir()
    _write_fake_binary(source)
    manifest = tmp_path / "config" / "binaries.json"
    cache = tmp_path / "cache" / "binaries"

    saved = save_binary(
        "@rng-ce",
        source,
        build_type="Release",
        manifest=manifest,
        cache_dir=cache,
    )

    assert saved.name == "rng-ce"
    assert saved.path.name == "rippled"
    assert saved.path.parent.parent == cache / "rng-ce"
    assert saved.path.exists()
    assert saved.source_path == source.resolve()
    assert saved.build_type == "Release"
    assert saved.version == "xahaud-test 1.2.3"

    data = json.loads(manifest.read_text())
    assert data["rng-ce"]["path"] == str(saved.path)
    assert data["rng-ce"]["version"] == "xahaud-test 1.2.3"
    assert resolve_binary_alias("@rng-ce", manifest=manifest) == saved.path


def test_save_binary_overwrite_does_not_clobber_existing_on_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "build" / "rippled"
    source.parent.mkdir()
    _write_fake_binary(source, "v1")
    manifest = tmp_path / "config" / "binaries.json"
    cache = tmp_path / "cache" / "binaries"
    saved = save_binary("@rng-ce", source, manifest=manifest, cache_dir=cache)

    source.write_text("#!/bin/sh\nprintf '%s\\n' 'v2'\n")

    def fail_copy(*_args, **_kwargs) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr("xahaud_scripts.binary_registry.shutil.copy2", fail_copy)

    with pytest.raises(OSError, match="copy failed"):
        save_binary("@rng-ce", source, manifest=manifest, cache_dir=cache)

    assert saved.path.read_text() != source.read_text()
    assert resolve_binary_alias("@rng-ce", manifest=manifest) == saved.path


def test_save_binary_manifest_failure_does_not_change_active_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "build" / "rippled"
    source.parent.mkdir()
    _write_fake_binary(source, "v1")
    manifest = tmp_path / "config" / "binaries.json"
    cache = tmp_path / "cache" / "binaries"
    saved = save_binary("@rng-ce", source, manifest=manifest, cache_dir=cache)

    source.write_text("#!/bin/sh\nprintf '%s\\n' 'v2'\n")

    def fail_write(*_args, **_kwargs) -> None:
        raise OSError("manifest failed")

    monkeypatch.setattr("xahaud_scripts.binary_registry.write_manifest", fail_write)

    with pytest.raises(OSError, match="manifest failed"):
        save_binary("@rng-ce", source, manifest=manifest, cache_dir=cache)

    assert resolve_binary_alias("@rng-ce", manifest=manifest) == saved.path
    assert saved.path.read_text() != source.read_text()


def test_save_binary_rejects_non_executable_file(tmp_path: Path) -> None:
    source = tmp_path / "rippled"
    source.write_text("")

    with pytest.raises(ValueError, match="not an executable file"):
        save_binary("@not-executable", source, manifest=tmp_path / "binaries.json")


def test_save_binary_ignores_nonzero_version_output(tmp_path: Path) -> None:
    source = tmp_path / "rippled"
    _write_failing_binary(source)

    saved = save_binary(
        "@no-version",
        source,
        manifest=tmp_path / "binaries.json",
        cache_dir=tmp_path / "cache",
    )

    assert saved.version is None


def test_load_manifest_missing_is_empty(tmp_path: Path) -> None:
    assert load_manifest(tmp_path / "missing.json") == {}


def test_resolve_binary_alias_errors_for_missing_alias(tmp_path: Path) -> None:
    manifest = tmp_path / "binaries.json"
    manifest.write_text("{}")

    with pytest.raises(FileNotFoundError, match="saved binary @missing not found"):
        resolve_binary_alias("@missing", manifest=manifest)


def test_resolve_binary_alias_rejects_non_executable_manifest_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "rippled"
    target.write_text("")
    manifest = tmp_path / "binaries.json"
    manifest.write_text(json.dumps({"bad": {"path": str(target)}}))

    with pytest.raises(PermissionError, match="path is not executable"):
        resolve_binary_alias("@bad", manifest=manifest)


def test_resolve_binary_spec_only_interprets_at_alias(tmp_path: Path) -> None:
    plain = tmp_path / "rippled"

    assert resolve_binary_spec(plain) == plain


def test_find_rippled_binary(tmp_path: Path) -> None:
    assert find_rippled_binary(tmp_path) is None

    rippled = tmp_path / "rippled"
    rippled.write_text("")
    assert find_rippled_binary(tmp_path) is None

    rippled.chmod(rippled.stat().st_mode | stat.S_IXUSR)
    assert find_rippled_binary(tmp_path) == rippled


def test_build_reconfigures_existing_build_dir_without_cmake_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    build_dir = root / "build"
    build_dir.mkdir(parents=True)
    _write_fake_binary(build_dir / "rippled")

    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "xahaud_scripts.run_tests.get_xahaud_root",
        lambda: str(root),
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.conan_toolchain_present",
        lambda _build_dir: True,
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.check_config_mismatch",
        lambda **_kwargs: calls.append(("mismatch", "")),
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.conan_install",
        lambda **kwargs: calls.append(("conan", kwargs["build_dir"])) or True,
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.cmake_configure",
        lambda build_dir_arg, *_args, **_kwargs: calls.append(
            ("configure", build_dir_arg)
        )
        or True,
    )
    monkeypatch.setattr(
        "xahaud_scripts.run_tests.cmake_build",
        lambda build_dir_arg, **_kwargs: calls.append(("build", build_dir_arg)) or True,
    )

    assert build_rippled(
        build_dir=str(build_dir),
        build_type="Release",
        use_conan=True,
    )
    assert calls == [
        ("conan", str(build_dir)),
        ("configure", str(build_dir)),
        ("build", str(build_dir)),
    ]


def test_no_build_flag_stays_dead() -> None:
    # Tombstone: --no-build let tests run against a stale binary and present
    # green results as evidence for code they never executed. It was removed
    # deliberately (2026-07-11); this test guarantees it never comes back.
    result = CliRunner().invoke(
        run_tests_main,
        ["--no-build", "--times=0"],
    )

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_save_binary_requires_rippled_target() -> None:
    result = CliRunner().invoke(
        run_tests_main,
        ["--target", "xrpld", "--save-binary", "@wrong-target", "--times=0"],
    )

    assert result.exit_code != 0
    assert "--save-binary only supports --target rippled" in result.output


def test_save_binary_validates_alias_before_dry_run_build() -> None:
    result = CliRunner().invoke(
        run_tests_main,
        ["--dry-run", "--save-binary", "@bad/name", "--times=0"],
    )

    assert result.exit_code != 0
    assert "saved binary alias must look like @name" in result.output
