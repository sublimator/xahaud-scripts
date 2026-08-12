from pathlib import Path

import pytest
from click import ClickException

from xahaud_scripts.build_xrpld import (
    _cached_cmake_generator,
    _refuse_xahau_repo,
    _require_ninja_cache,
)


def write_cache(path: Path, generator: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"CMAKE_BUILD_TYPE:STRING=Release\nCMAKE_GENERATOR:INTERNAL={generator}\n"
    )


def test_cached_cmake_generator_reads_internal_entry(tmp_path: Path) -> None:
    cache = tmp_path / "CMakeCache.txt"
    write_cache(cache, "Ninja")

    assert _cached_cmake_generator(cache) == "Ninja"


def test_ninja_cache_is_accepted_for_conan_layout(tmp_path: Path) -> None:
    cache = tmp_path / "build/Release/CMakeCache.txt"
    write_cache(cache, "Ninja")

    _require_ninja_cache(tmp_path, "Release", use_preset=True)


def test_make_cache_is_refused_for_conan_layout(tmp_path: Path) -> None:
    cache = tmp_path / "build/Release/CMakeCache.txt"
    write_cache(cache, "Unix Makefiles")

    with pytest.raises(ClickException, match="refusing non-Ninja"):
        _require_ninja_cache(tmp_path, "Release", use_preset=True)


def test_missing_cache_is_valid_for_fresh_build(tmp_path: Path) -> None:
    _require_ninja_cache(tmp_path, "Release", use_preset=False)


def test_xahau_repo_is_refused_by_hook_source_tree(tmp_path: Path) -> None:
    (tmp_path / "src/ripple/app/hook").mkdir(parents=True)

    with pytest.raises(ClickException, match="x-run-tests|Xahau"):
        _refuse_xahau_repo(tmp_path)


def test_xahau_repo_is_refused_by_root_hook_headers(tmp_path: Path) -> None:
    (tmp_path / "hook").mkdir()
    (tmp_path / "hook/hookapi.h").write_text("#define HOOKAPI\n")

    with pytest.raises(ClickException, match="x-run-tests|Xahau"):
        _refuse_xahau_repo(tmp_path)


def test_xrpld_repo_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "src/xrpld").mkdir(parents=True)
    (tmp_path / "include/xrpl").mkdir(parents=True)

    _refuse_xahau_repo(tmp_path)


def test_bare_repo_without_xahau_markers_is_accepted(tmp_path: Path) -> None:
    _refuse_xahau_repo(tmp_path)
