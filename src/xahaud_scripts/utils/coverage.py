import os
import sys
from typing import Optional

from xahaud_scripts.utils.logging import make_logger
from xahaud_scripts.utils.paths import get_xahaud_root
from xahaud_scripts.utils.shell_utils import (
    check_tool_exists,
    get_llvm_tool_command,
    change_directory,
    run_command,
)

logger = make_logger(__name__)


def do_generate_coverage_report(
    build_dir: str, specific_file: Optional[str] = None, prefix: Optional[str] = None
) -> bool:
    """Generate a coverage report if the build was instrumented for coverage.

    Args:
        build_dir: The build directory
        specific_file: If provided, show coverage for just this file
        prefix: If provided, only include profraw files with this prefix

    Returns:
        bool: True if report generation was successful
    """

    # Check if this was a coverage build
    cmake_cache = os.path.join(build_dir, "CMakeCache.txt")
    if not os.path.exists(cmake_cache):
        logger.warning("Cannot generate coverage report: CMakeCache.txt not found")
        return False

    try:
        with open(cmake_cache, "r") as f:
            if "coverage:STRING=ON" not in f.read():
                logger.warning(
                    "Cannot generate coverage report: This was not a coverage build"
                )
                return False
    except Exception as e:
        logger.warning(f"Could not check if this was a coverage build: {e}")
        return False

    logger.info("Generating coverage report...")

    # Check for required tools
    required_tools = ["llvm-profdata", "llvm-cov"]
    tool_commands = {}

    for tool in required_tools:
        if not check_tool_exists(tool) and not (
            sys.platform == "darwin" and check_tool_exists("xcrun")
        ):
            logger.error(
                f"{tool} not found. Make sure llvm tools are in PATH or xcrun is available on macOS"
            )
            return False
        tool_commands[tool] = get_llvm_tool_command(tool)

    with change_directory(build_dir):
        try:
            # Find profraw files
            if prefix:
                profraw_files = [
                    f
                    for f in os.listdir(".")
                    if f.endswith(".profraw") and f.startswith(prefix)
                ]
                logger.info(
                    f"Found {len(profraw_files)} profraw files with prefix '{prefix}'"
                )
            else:
                profraw_files = [f for f in os.listdir(".") if f.endswith(".profraw")]
                logger.info(f"Found {len(profraw_files)} profraw files (all prefixes)")

            if not profraw_files:
                logger.warning(
                    "No profraw files found. Run tests with coverage instrumentation first."
                )
                return False

            # Merge profraw files
            merged_file = "coverage.profdata"
            run_command(
                tool_commands["llvm-profdata"]
                + ["merge", "-sparse"]
                + profraw_files
                + ["-o", merged_file]
            )

            # Find the rippled executable
            rippled_path = "./rippled"
            if not os.path.exists(rippled_path):
                rippled_path = "./rippled.exe"
                if not os.path.exists(rippled_path):
                    logger.error(
                        "Could not find rippled executable for coverage report"
                    )
                    return False

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
                    f"Coverage report for {file_basename} written to {os.path.abspath(report_file)}"
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
