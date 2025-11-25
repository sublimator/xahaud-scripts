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


def conan_install_v1(
    xahaud_root: str,
    build_type: str = "Debug",
    dry_run: bool = False,
) -> bool:
    """Install dependencies using Conan 1.x.

    Args:
        xahaud_root: Path to the xahaud source root
        build_type: CMake build type (Debug or Release)
        dry_run: If True, print the command without executing

    Returns:
        True if successful, False otherwise
    """
    if not check_conan_available():
        return False

    logger.info("Installing dependencies with Conan 1.x...")
    logger.info(f"Using build type {build_type}")

    cmd = [
        "conan",
        "install",
        ".",
        "--build=missing",
        "--install-folder=build",
        "-s",
        f"build_type={build_type}",
    ]

    if dry_run:
        print("\n[DRY RUN] Conan 1.x install command:")
        print(f"  Working directory: {xahaud_root}")
        print(f"  {' '.join(cmd)}")
        print()
        return True

    with change_directory(xahaud_root):
        try:
            run_command(cmd)
            logger.info("Conan 1.x dependencies installed successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to install dependencies with Conan 1.x: {e}")
            return False


def conan_install_v2(
    xahaud_root: str,
    build_type: str = "Debug",
    dry_run: bool = False,
) -> bool:
    """Install dependencies using Conan 2.x.

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

    logger.info("Installing dependencies with Conan 2.x...")
    logger.info(f"Using build type {build_type}")

    # Don't specify --output-folder, let cmake_layout() handle it
    cmd = [
        "conan",
        "install",
        ".",
        "--build=missing",
        "-s",
        f"build_type={build_type}",
    ]

    if dry_run:
        print("\n[DRY RUN] Conan 2.x install command:")
        print(f"  Working directory: {xahaud_root}")
        print(f"  {' '.join(cmd)}")
        print()
        return True

    with change_directory(xahaud_root):
        try:
            run_command(cmd)
            logger.info("Conan 2.x dependencies installed successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to install dependencies with Conan 2.x: {e}")
            return False


def conan_install(
    xahaud_root: str,
    build_type: str = "Debug",
    use_v2: bool = False,
    dry_run: bool = False,
) -> bool:
    """Install dependencies using Conan.

    Args:
        xahaud_root: Path to the xahaud source root
        build_type: CMake build type (Debug or Release)
        use_v2: If True, use Conan 2.x, otherwise use Conan 1.x
        dry_run: If True, print the command without executing

    Returns:
        True if successful, False otherwise
    """
    if use_v2:
        return conan_install_v2(xahaud_root, build_type, dry_run=dry_run)
    else:
        return conan_install_v1(xahaud_root, build_type, dry_run=dry_run)
