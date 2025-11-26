"""Conan package manager integration."""

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


def conan_install(
    xahaud_root: str,
    build_type: str = "Debug",
    dry_run: bool = False,
) -> bool:
    """Install dependencies using Conan.

    Uses the layout defined in conanfile.py (cmake_layout with
    self.folders.generators = 'build/generators').

    Args:
        xahaud_root: Path to the xahaud source root
        build_type: CMake build type (Debug or Release)
        dry_run: If True, print the command without executing

    Returns:
        True if successful, False otherwise
    """
    if not check_conan_available():
        return False

    logger.info("Installing dependencies with Conan...")
    logger.info(f"Using build type {build_type}")

    cmd = [
        "conan",
        "install",
        ".",
        "--build=missing",
        "-s",
        f"build_type={build_type}",
    ]

    if dry_run:
        print("\n[DRY RUN] Conan install command:")
        print(f"  Working directory: {xahaud_root}")
        print(f"  {' '.join(cmd)}")
        print()
        return True

    with change_directory(xahaud_root):
        try:
            run_command(cmd)
            logger.info("Conan dependencies installed successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to install dependencies with Conan: {e}")
            return False
