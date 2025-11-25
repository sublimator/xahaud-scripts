"""Build configuration detection and utilities."""

import os
import time
from dataclasses import dataclass

from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)


@dataclass
class BuildConfig:
    """Configuration options for building xahaud."""

    coverage: bool = False
    conan: bool = False
    conan_2: bool = False
    verbose: bool = False
    ccache: bool = False
    build_type: str = "Debug"
    target: str = "rippled"
    log_line_numbers: bool = True
    reconfigure: bool = False

    @property
    def use_conan(self) -> bool:
        """Return True if any conan version is enabled."""
        return self.conan or self.conan_2


def detect_previous_build_config(build_dir: str) -> dict:
    """Try to detect the previous build configuration from CMakeCache.txt.

    Args:
        build_dir: Path to the build directory

    Returns:
        dict with keys: coverage, conan, verbose, ccache, build_type
    """
    config = {
        "coverage": False,
        "conan": False,
        "verbose": False,
        "ccache": False,
        "build_type": "Debug",
    }

    cmake_cache_path = os.path.join(build_dir, "CMakeCache.txt")
    if not os.path.exists(cmake_cache_path):
        logger.debug("No previous CMake cache found")
        return config

    logger.debug(f"Analyzing previous build configuration from {cmake_cache_path}")
    try:
        with open(cmake_cache_path) as f:
            cache_content = f.read()

            # Check for coverage
            if "coverage:STRING=ON" in cache_content:
                config["coverage"] = True
                logger.debug("Detected previous build with coverage enabled")

            # Check for conan toolchain
            if (
                "CMAKE_TOOLCHAIN_FILE" in cache_content
                and "conan_toolchain.cmake" in cache_content
            ):
                config["conan"] = True
                logger.debug("Detected previous build with conan")

            # Check for verbose
            if "CMAKE_VERBOSE_MAKEFILE:BOOL=ON" in cache_content:
                config["verbose"] = True
                logger.debug("Detected previous build with verbose output")

            # Check for ccache
            if (
                "CMAKE_CXX_COMPILER_LAUNCHER" in cache_content
                and "ccache" in cache_content
            ):
                config["ccache"] = True
                logger.debug("Detected previous build with ccache")

            # Check for build type
            if "CMAKE_BUILD_TYPE:STRING=Release" in cache_content:
                config["build_type"] = "Release"
                logger.debug("Detected previous build with Release build type")
            elif "CMAKE_BUILD_TYPE:STRING=Debug" in cache_content:
                config["build_type"] = "Debug"
                logger.debug("Detected previous build with Debug build type")
    except Exception as e:
        logger.warning(f"Could not analyze previous build configuration: {e}")

    return config


def generate_coverage_prefix() -> str:
    """Generate a prefix for coverage files to identify specific test runs.

    Returns:
        A string like 'coverage_run_1234567890123' with millisecond timestamp
    """
    timestamp_ms = int(time.time() * 1000)
    return "coverage_run_" + str(timestamp_ms)


def check_config_mismatch(
    build_dir: str,
    coverage: bool,
    use_conan: bool,
    verbose: bool,
    ccache: bool,
    build_type: str,
) -> bool:
    """Check if current config differs from previous build.

    Args:
        build_dir: Path to the build directory
        coverage: Current coverage setting
        use_conan: Current conan setting (True if using any conan version)
        verbose: Current verbose setting
        ccache: Current ccache setting
        build_type: Current build type

    Returns:
        True if there's a mismatch, False otherwise
    """
    prev_config = detect_previous_build_config(build_dir)
    mismatch = (
        prev_config["coverage"] != coverage
        or prev_config["conan"] != use_conan
        or prev_config["verbose"] != verbose
        or prev_config["ccache"] != ccache
        or prev_config["build_type"] != build_type
    )

    if mismatch:
        logger.warning("Current build configuration differs from previous build.")
        logger.warning(
            "Previous build: "
            + f"coverage={prev_config['coverage']}, "
            + f"conan={prev_config['conan']}, "
            + f"verbose={prev_config['verbose']}, "
            + f"ccache={prev_config['ccache']}, "
            + f"build_type={prev_config['build_type']}"
        )
        logger.warning(
            f"Current request: coverage={coverage}, conan={use_conan}, "
            f"verbose={verbose}, ccache={ccache}, build_type={build_type}"
        )
        logger.warning(
            "Consider using --reconfigure-build to ensure consistent configuration"
        )

    return mismatch
