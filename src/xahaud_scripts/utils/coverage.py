"""Coverage report generation using LLVM tools.

Provides helpers for working with LLVM source-based coverage data
(profraw → profdata → llvm-cov reports).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from xahaud_scripts.utils.logging import make_logger
from xahaud_scripts.utils.paths import get_xahaud_root
from xahaud_scripts.utils.shell_utils import (
    change_directory,
    check_tool_exists,
    get_llvm_tool_command,
    run_command,
)

logger = make_logger(__name__)


def check_coverage_build(build_dir: str) -> bool:
    """Check if the build directory contains a coverage-instrumented build.

    Args:
        build_dir: Path to the build directory.

    Returns:
        True if this was a coverage build.
    """
    cmake_cache = os.path.join(build_dir, "CMakeCache.txt")
    if not os.path.exists(cmake_cache):
        logger.warning("Cannot check coverage build: CMakeCache.txt not found")
        return False

    try:
        with open(cmake_cache) as f:
            if "coverage:STRING=ON" not in f.read():
                logger.warning("This was not a coverage build")
                return False
    except Exception as e:
        logger.warning(f"Could not check coverage build config: {e}")
        return False

    return True


def get_llvm_tools() -> dict[str, list[str]] | None:
    """Find llvm-profdata and llvm-cov commands.

    Returns:
        Dict mapping tool name to command list, or None if tools not found.
    """
    tool_commands: dict[str, list[str]] = {}

    for tool in ["llvm-profdata", "llvm-cov"]:
        if not check_tool_exists(tool) and not (
            sys.platform == "darwin" and check_tool_exists("xcrun")
        ):
            logger.error(
                f"{tool} not found. Make sure llvm tools are in PATH "
                "or xcrun is available on macOS"
            )
            return None
        tool_commands[tool] = get_llvm_tool_command(tool)

    return tool_commands


def find_and_merge_profdata(
    build_dir: str,
    tool_commands: dict[str, list[str]],
    prefix: str | None = None,
) -> Path | None:
    """Find profraw files and merge them into a profdata file.

    Args:
        build_dir: Build directory containing profraw files.
        tool_commands: Dict from get_llvm_tools().
        prefix: If provided, only include profraw files with this prefix.

    Returns:
        Path to merged profdata file, or None if no profraw files found.
    """
    if prefix:
        profraw_files = [
            f
            for f in os.listdir(build_dir)
            if f.endswith(".profraw") and f.startswith(prefix)
        ]
        logger.info(f"Found {len(profraw_files)} profraw files with prefix '{prefix}'")
    else:
        profraw_files = [f for f in os.listdir(build_dir) if f.endswith(".profraw")]
        logger.info(f"Found {len(profraw_files)} profraw files (all prefixes)")

    if not profraw_files:
        logger.warning(
            "No profraw files found. Run tests with coverage instrumentation first."
        )
        return None

    merged_file = os.path.join(build_dir, "coverage.profdata")

    with change_directory(build_dir):
        run_command(
            tool_commands["llvm-profdata"]
            + ["merge", "-sparse"]
            + profraw_files
            + ["-o", "coverage.profdata"]
        )

    return Path(merged_file)


def find_binary(build_dir: str) -> Path | None:
    """Find the rippled/xrpld binary in the build directory.

    Args:
        build_dir: Build directory.

    Returns:
        Path to the binary, or None if not found.
    """
    for name in ["rippled", "rippled.exe", "xrpld", "xrpld.exe"]:
        path = Path(build_dir) / name
        if path.exists():
            return path

    logger.error("Could not find rippled/xrpld executable in build directory")
    return None


def do_generate_coverage_report(
    build_dir: str, specific_file: str | None = None, prefix: str | None = None
) -> bool:
    """Generate a coverage report if the build was instrumented for coverage.

    Args:
        build_dir: The build directory
        specific_file: If provided, show coverage for just this file
        prefix: If provided, only include profraw files with this prefix

    Returns:
        bool: True if report generation was successful
    """
    if not check_coverage_build(build_dir):
        return False

    logger.info("Generating coverage report...")

    tool_commands = get_llvm_tools()
    if tool_commands is None:
        return False

    profdata = find_and_merge_profdata(build_dir, tool_commands, prefix)
    if profdata is None:
        return False

    binary = find_binary(build_dir)
    if binary is None:
        return False

    try:
        with change_directory(build_dir):
            merged_file = "coverage.profdata"
            rippled_path = str(binary.relative_to(Path(build_dir)))

            # If a specific file was requested
            if specific_file:
                logger.info(
                    f"Generating coverage report for specific file: {specific_file}"
                )

                # Handle relative path
                if not os.path.isabs(specific_file):
                    specific_file = os.path.join(get_xahaud_root(), specific_file)

                if not os.path.exists(specific_file):
                    logger.error(f"Specified file not found: {specific_file}")
                    return False

                # Generate file-specific report
                result = run_command(
                    tool_commands["llvm-cov"]
                    + [
                        "show",
                        rippled_path,
                        "-instr-profile=" + merged_file,
                        specific_file,
                    ],
                    capture_output=True,
                )

                # Save to file
                file_basename = os.path.basename(specific_file)
                report_file = f"coverage_{file_basename}.txt"
                with open(report_file, "w") as f:
                    f.write(result.stdout)

                logger.info(
                    f"Coverage report for {file_basename} written to "
                    f"{os.path.abspath(report_file)}"
                )
                return True

            # Otherwise generate full report
            report_dir = "coverage_report"
            os.makedirs(report_dir, exist_ok=True)

            # Generate HTML report
            run_command(
                tool_commands["llvm-cov"]
                + [
                    "show",
                    rippled_path,
                    "-instr-profile=" + merged_file,
                    "-format=html",
                    "-output-dir=" + report_dir,
                ]
            )

            # Generate text summary
            summary_path = os.path.join(report_dir, "summary.txt")
            with open(summary_path, "w") as f:
                result = run_command(
                    tool_commands["llvm-cov"]
                    + [
                        "report",
                        rippled_path,
                        "-instr-profile=" + merged_file,
                    ],
                    capture_output=True,
                )
                f.write(result.stdout)

            logger.info(f"Coverage report generated in {os.path.abspath(report_dir)}")
            logger.info(f"Summary available at {os.path.abspath(summary_path)}")
            return True
    except Exception as e:
        logger.error(f"Failed to generate coverage report: {e}")
        return False
