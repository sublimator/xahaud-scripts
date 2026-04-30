"""Conan package manager integration."""

import os
from pathlib import Path

from xahaud_scripts.utils.logging import make_logger
from xahaud_scripts.utils.shell_utils import (
    change_directory,
    check_tool_exists,
    run_command,
)

logger = make_logger(__name__)


def check_conan_available() -> bool:
    """Check if conan is available in PATH.

    Returns:
        True if conan is available, False otherwise
    """
    if not check_tool_exists("conan"):
        logger.error("Conan is required but not found in PATH")
        return False
    return True


def conan_toolchain_present(build_dir: str) -> bool:
    """Return True if the conan-generated toolchain exists in build_dir.

    Looks for ``<build_dir>/generators/conan_toolchain.cmake`` (the path the
    cmake configure step expects). When this is missing, ``cmake --preset`` /
    ``-DCMAKE_TOOLCHAIN_FILE=generators/conan_toolchain.cmake`` will fail.
    """
    return (Path(build_dir) / "generators" / "conan_toolchain.cmake").is_file()


def conan_install(
    xahaud_root: str,
    build_type: str = "Debug",
    build_dir: str | None = None,
    dry_run: bool = False,
) -> bool:
    """Install dependencies using Conan into the given build dir.

    When ``build_dir`` is provided, runs from that dir with
    ``--output-folder=.`` so the generators land at
    ``<build_dir>/generators/`` regardless of the dir name (build-debug,
    build-debug-llvm, build-debug-cov, etc.). When ``build_dir`` is None
    we fall back to the legacy behaviour (conan layout default under
    ``<xahaud_root>/build/``).

    Args:
        xahaud_root: Path to the xahaud source root.
        build_type: CMake build type (Debug or Release).
        build_dir: Build directory to scope the conan output to.
        dry_run: If True, print the command without executing.

    Returns:
        True if successful, False otherwise.
    """
    if not check_conan_available():
        return False

    logger.info("Installing dependencies with Conan...")
    logger.info(f"Using build type {build_type}")

    if build_dir is not None:
        # Scope conan's output to this exact build dir so generators land
        # under <build_dir>/generators/ — what cmake_configure expects.
        cmd = [
            "conan",
            "install",
            "--output-folder",
            ".",
            "--build=missing",
            "-s",
            f"build_type={build_type}",
            xahaud_root,
        ]
        cwd = build_dir
    else:
        cmd = [
            "conan",
            "install",
            ".",
            "--build=missing",
            "-s",
            f"build_type={build_type}",
        ]
        cwd = xahaud_root

    if dry_run:
        print("\n[DRY RUN] Conan install command:")
        print(f"  Working directory: {cwd}")
        print(f"  {' '.join(cmd)}")
        print()
        return True

    if build_dir is not None:
        os.makedirs(build_dir, exist_ok=True)

    with change_directory(cwd):
        try:
            run_command(cmd)
            logger.info("Conan dependencies installed successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to install dependencies with Conan: {e}")
            return False
