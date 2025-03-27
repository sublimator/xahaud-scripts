#!/usr/bin/env python3
"""
Script to build and run rippled tests with support for debugging with lldb.
Features include:
- Coverage instrumentation and reporting
- Conan package management integration
- CCache support for faster builds
- LLDB debugging
- Comprehensive logging
"""
import json

import click
import os
import subprocess
import sys
import shutil
import logging
import tempfile
import random
import string
from typing import List, Optional, Dict, Any
from contextlib import contextmanager

from xahaud_scripts.utils.paths import get_xahaud_root

# Set up logger
logger = logging.getLogger("run_tests")

GDB_SCRIPT = """
# Set breakpoints
breakpoint set --name malloc_error_break
breakpoint set --name abort

# Run the program
run

# Check process status after run completes or crashes
process status

# Use Python script to conditionally execute backtrace
script
import lldb
import sys
import os

lldb.debugger.SetOutputFileHandle(sys.stdout, True)

def log(s, *args):
    print('\n' + s, *args)

process = lldb.debugger.GetSelectedTarget().GetProcess()

exit_desc = process.GetExitDescription()
exit_status = process.GetExitStatus()
state = process.GetState()

if exit_desc:
    log(f"Process exited with status {exit_status}: {exit_desc}")
else:
    log(f"Process state: {state} stopped={lldb.eStateStopped}")

if state == lldb.eStateStopped:
    log("Getting backtrace:")
    lldb.debugger.HandleCommand('bt')
else:
    log("Process not stopped, can't get backtrace")
    log('Exiting')
    lldb.debugger.HandleCommand('quit')
    os._exit(0)

log('End of script')
"""


def setup_logging(log_level: str) -> None:
    """Set up logging with the specified level."""
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    # Configure the root logger
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Set our module logger level
    logger.setLevel(numeric_level)
    logger.info(f"Logging initialized at level {log_level.upper()}")


def get_llvm_tool_command(tool_name: str) -> List[str]:
    """Get the command to run an LLVM tool, using xcrun on macOS if available."""
    if sys.platform == "darwin" and check_tool_exists("xcrun"):
        logger.debug(f"Using xcrun to invoke {tool_name} on macOS")
        return ["xcrun", tool_name]
    else:
        return [tool_name]


def run_command(
    cmd: List[str],
    check: bool = True,
    capture_output: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    cmd_str = json.dumps(cmd)
    logger.info(f"Running command: {cmd_str}")

    try:
        if capture_output:
            result = subprocess.run(
                cmd, check=check, capture_output=True, text=True, env=env
            )
            if result.stdout:
                logger.debug(f"Command stdout: {result.stdout}")
            if result.stderr:
                logger.debug(f"Command stderr: {result.stderr}")
        else:
            result = subprocess.run(cmd, check=check, env=env)

        logger.info(f"Command completed with return code: {result.returncode}")
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed with return code: {e.returncode}")
        if hasattr(e, "output") and e.output:
            logger.error(f"Command output: {e.output}")
        if hasattr(e, "stderr") and e.stderr:
            logger.error(f"Command stderr: {e.stderr}")
        if check:
            raise
        # Create a proper CompletedProcess object with the exception data
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=e.returncode,
            stdout=e.stdout if hasattr(e, "stdout") else None,
            stderr=e.stderr if hasattr(e, "stderr") else None,
        )
    except FileNotFoundError as e:
        logger.error(f"Command not found: {cmd[0]}")
        raise


def check_tool_exists(tool_name: str) -> bool:
    """Check if a command-line tool exists."""
    exists = shutil.which(tool_name) is not None
    if exists:
        logger.debug(f"Tool '{tool_name}' is available")
    else:
        logger.warning(f"Tool '{tool_name}' not found in PATH")
    return exists


def get_logical_cpu_count() -> int:
    """Get the number of logical CPUs."""
    try:
        if sys.platform == "darwin":
            count = int(
                subprocess.check_output(["sysctl", "-n", "hw.logicalcpu"]).strip()
            )
        else:
            count = os.cpu_count() or 4  # Default to 4 if we can't determine

        logger.debug(f"Detected {count} logical CPU cores")
        return count
    except Exception as e:
        logger.warning(f"Could not determine CPU count: {e}. Using default of 4.")
        return 4


@contextmanager
def change_directory(path: str):
    """Context manager for changing directories safely."""
    old_dir = os.getcwd()
    logger.debug(f"Changing directory from {old_dir} to {path}")
    try:
        os.chdir(path)
        yield
    finally:
        logger.debug(f"Changing directory back to {old_dir}")
        os.chdir(old_dir)


def do_build_jshooks_header() -> None:
    """Build the JS hooks header."""
    logger.info("Building JS hooks header...")

    script_path = os.path.join("./.scripts/src/xahaud_scripts/build_jshooks_header.py")
    if not os.path.exists(script_path):
        script_path = "./src/xahaud_scripts/build_jshooks_header.py"
        if not os.path.exists(script_path):
            logger.error("Could not find build_jshooks_header.py script")
            raise FileNotFoundError("Could not find build_jshooks_header.py script")

    try:
        run_command(["python", script_path, "--canonical"])
        logger.info("JS hooks header built successfully")
    except Exception as e:
        logger.error(f"Failed to build JS hooks header: {e}")
        raise


def detect_previous_build_config(build_dir: str) -> dict:
    """Try to detect the previous build configuration."""
    config = {"coverage": False, "conan": False, "verbose": False, "ccache": False}

    cmake_cache_path = os.path.join(build_dir, "CMakeCache.txt")
    if not os.path.exists(cmake_cache_path):
        logger.debug("No previous CMake cache found")
        return config

    logger.debug(f"Analyzing previous build configuration from {cmake_cache_path}")
    try:
        with open(cmake_cache_path, "r") as f:
            cache_content = f.read()

            # Check for coverage
            if "coverage:BOOL=ON" in cache_content:
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
    except Exception as e:
        logger.warning(f"Could not analyze previous build configuration: {e}")

    return config


def generate_random_prefix() -> str:
    """Generate a random prefix for coverage files to identify specific test runs."""
    chars = string.ascii_lowercase + string.digits
    return "rip_" + "".join(random.choice(chars) for _ in range(8))


def build_rippled(
    reconfigure_build: bool = False,
    coverage: bool = False,
    use_conan: bool = False,
    verbose: bool = False,
    use_ccache: bool = False,
) -> bool:
    """Build the rippled executable.

    Args:
        reconfigure_build: If True, force CMake reconfiguration even if build directory exists
        coverage: If True, enable code coverage
        use_conan: If True, use Conan package manager for dependencies
        verbose: If True, enable verbose output during build
        use_ccache: If True, use ccache to speed up compilation

    Returns:
        bool: True if build was successful, False otherwise
    """
    xahaud_root = get_xahaud_root()
    build_dir = os.path.join(xahaud_root, "build")
    logger.info(f"Building rippled in {build_dir}")

    # Check if build directory exists
    build_dir_exists = os.path.exists(build_dir)

    # Determine if we need to configure
    need_configure = not build_dir_exists or reconfigure_build

    # If build directory exists and we're not reconfiguring, check for configuration mismatch
    if build_dir_exists and not need_configure:
        prev_config = detect_previous_build_config(build_dir)
        config_mismatch = (
            prev_config["coverage"] != coverage
            or prev_config["conan"] != use_conan
            or prev_config["verbose"] != verbose
            or prev_config["ccache"] != use_ccache
        )

        if config_mismatch:
            logger.warning("Current build configuration differs from previous build.")
            logger.warning(
                "Previous build: "
                + f"coverage={prev_config['coverage']}, "
                + f"conan={prev_config['conan']}, "
                + f"verbose={prev_config['verbose']}, "
                + f"ccache={prev_config['ccache']}"
            )
            logger.warning(
                f"Current request: coverage={coverage}, conan={use_conan}, verbose={verbose}, ccache={use_ccache}"
            )
            logger.warning(
                "Consider using --reconfigure-build to ensure consistent configuration"
            )

    # Create build directory if needed
    os.makedirs(build_dir, exist_ok=True)

    # Check for conan if requested
    if use_conan:
        if not check_tool_exists("conan"):
            logger.error("Conan is required but not found in PATH")
            return False

        if need_configure:
            logger.info("Installing dependencies with Conan...")
            # Determine build type and generator
            build_type = "Debug"  # Always use Debug build type
            generator = "Ninja" if check_tool_exists("ninja") else "Unix Makefiles"
            logger.info(f"Using build type {build_type} with generator {generator}")

            with change_directory(xahaud_root):
                try:
                    # Run conan install
                    run_command(
                        [
                            "conan",
                            "install",
                            ".",
                            "--build=missing",
                            "--install-folder=build",
                            f"-s",
                            f"build_type={build_type}",
                            "-e",
                            f"CMAKE_GENERATOR={generator}",
                        ]
                    )
                    logger.info("Conan dependencies installed successfully")
                except Exception as e:
                    logger.error(f"Failed to install dependencies with Conan: {e}")
                    return False

    # Configure cmake if needed
    if need_configure:
        logger.info("Configuring CMake build...")
        with change_directory(build_dir):
            # Get environment variables
            llvm_dir = os.environ.get("LLVM_DIR", "")
            llvm_library_dir = os.environ.get("LLVM_LIBRARY_DIR", "")

            # Build cmake command
            cmake_cmd = ["cmake"]

            # Add generator if using conan and ninja is available
            if use_conan and check_tool_exists("ninja"):
                cmake_cmd.extend(["-G", "Ninja"])

            # Base build type is always Debug
            build_type = "Debug"
            cmake_cmd.append(f"-DCMAKE_BUILD_TYPE={build_type}")

            # Common flags for both regular and coverage builds
            if verbose:
                cmake_cmd.append("-DCMAKE_VERBOSE_MAKEFILE=ON")

            cmake_cmd.append("-Dassert=TRUE")

            # Add ccache if requested
            if use_ccache:
                if check_tool_exists("ccache"):
                    logger.info("Using ccache to speed up compilation")
                    cmake_cmd.extend(
                        [
                            "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                            "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
                        ]
                    )
                else:
                    logger.warning(
                        "ccache requested but not found in PATH, continuing without it"
                    )

            # Add coverage settings if requested
            if coverage:
                logger.info("Configuring build with coverage instrumentation")
                cmake_cmd.extend(
                    [
                        "-Dcoverage=ON",
                        "-Dcoverage_core_only=ON",
                        "-DCMAKE_CXX_FLAGS=-O0 -fcoverage-mapping -fprofile-instr-generate",
                        "-DCMAKE_C_FLAGS=-O0 -fcoverage-mapping -fprofile-instr-generate",
                    ]
                )
            else:
                # Standard debug build settings
                logger.info("Configuring standard debug build")
                # cmake_cmd.insert(1, "-Dsan") # just after `cmake`

            # Add conan toolchain if using conan
            if use_conan:
                toolchain_path = "generators/conan_toolchain.cmake"
                logger.debug(f"Using Conan toolchain at {toolchain_path}")
                cmake_cmd.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain_path}")
                cmake_cmd.append("-Dunity=OFF")  # Disable unity builds when using conan

            # Add LLVM settings if provided
            if llvm_dir:
                logger.debug(f"Using LLVM directory: {llvm_dir}")
                cmake_cmd.append(f"-DLLVM_DIR={llvm_dir}")

            if llvm_library_dir:
                logger.debug(f"Using LLVM library directory: {llvm_library_dir}")
                cmake_cmd.append(f"-DLLVM_LIBRARY_DIR={llvm_library_dir}")

            # Add source directory
            cmake_cmd.append("..")  # We're already in the build directory

            try:
                # Run cmake configuration
                run_command(cmake_cmd)
                logger.info("CMake configuration completed successfully")
            except Exception as e:
                logger.error(f"CMake configuration failed: {e}")
                return False

    # Build rippled
    logger.info("Building rippled...")
    with change_directory(build_dir):
        cpu_count = get_logical_cpu_count()
        build_cmd = ["cmake", "--build", "."]

        # Add target
        build_cmd.extend(["--target", "rippled"])

        # Add parallel flag
        build_cmd.extend(["--parallel", f"{cpu_count}"])

        # Add verbose flag if requested
        if verbose:
            # Not all generators support --verbose, so we use CMAKE_VERBOSE_MAKEFILE instead
            # This was set during configuration if verbose was enabled
            logger.debug(
                "Build will use verbose output if configured with CMAKE_VERBOSE_MAKEFILE=ON"
            )

        try:
            run_command(build_cmd)
            logger.info("Build completed successfully")

            # Verify the build output exists
            rippled_path = os.path.join(build_dir, "rippled")
            if not os.path.exists(rippled_path):
                # On some platforms the executable might have a different name or location
                logger.warning(f"Could not find rippled executable at {rippled_path}")
                rippled_path = os.path.join(build_dir, "rippled.exe")
                if not os.path.exists(rippled_path):
                    logger.error("Could not find rippled executable after build")
                    return False

            logger.debug(f"Verified rippled executable exists at {rippled_path}")
            return True
        except Exception as e:
            logger.error(f"Build failed: {e}")
            return False


def create_lldb_script() -> str:
    """Create a temporary file with LLDB commands."""
    fd, path = tempfile.mkstemp(suffix=".lldb")
    logger.debug(f"Creating temporary LLDB script at {path}")

    with os.fdopen(fd, "w") as f:
        f.write(GDB_SCRIPT)

    return path


def run_rippled(
    args: List[str],
    use_lldb: bool,
    times: int = 1,
    stop_on_fail: bool = True,
    lldb_commands_file: Optional[str] = None,
    env: Optional[dict] = None,
) -> int:
    """Run the rippled executable, optionally with lldb, multiple times.

    Args:
        args: Arguments to pass to rippled
        use_lldb: Whether to run with lldb
        times: Number of times to run the command
        stop_on_fail: Whether to stop on first failure
        lldb_commands_file: Path to LLDB commands file
        env: Environment variables to set for the process

    Returns:
        tuple: (exit_code, coverage_prefix) - the exit code of the last run and the coverage file prefix if used
    """
    build_dir = os.path.join(get_xahaud_root(), "build")

    # Verify the rippled executable exists
    rippled_path = os.path.join(build_dir, "rippled")
    if not os.path.exists(rippled_path):
        rippled_path = os.path.join(build_dir, "rippled.exe")
        if not os.path.exists(rippled_path):
            logger.error("Rippled executable not found. Build may have failed.")
            return 1

    logger.info(f"Found rippled at {rippled_path}")

    test_args = ["-u"] + args
    exit_code = 0
    temp_lldb_script = None

    # If lldb is requested, check if it's available
    if use_lldb and not check_tool_exists("lldb"):
        logger.error("LLDB is required but not found in PATH")
        return 1

    # Create a default LLDB script if requested but none provided
    if use_lldb and not lldb_commands_file:
        temp_lldb_script = create_lldb_script()
        lldb_commands_file = temp_lldb_script
        logger.info(f"Created default LLDB script at {lldb_commands_file}")

    try:
        with change_directory(build_dir):
            for i in range(times):
                if times > 1:
                    logger.info(f"\nRun {i + 1}/{times}")

                if use_lldb:
                    cmd = ["lldb", "--", "./rippled"] + test_args
                    if lldb_commands_file:
                        cmd = cmd[0:1] + ["-s", lldb_commands_file] + cmd[1:]
                else:
                    cmd = ["./rippled"] + test_args

                # Don't use check=True here to allow lldb to exit naturally
                # Pass the environment with coverage settings
                process = run_command(cmd, check=False, env=env)
                exit_code = process.returncode

                # If a run fails and we're not at the last iteration
                if exit_code != 0 and i < times - 1:
                    logger.warning(f"Run {i + 1} failed with exit code {exit_code}")

                    if stop_on_fail:
                        logger.info(
                            "Stopping due to failure (use --no-stop-on-fail to continue on failures)"
                        )
                        break
                    else:
                        logger.info("Continuing to next run...")
    finally:
        # Clean up temporary LLDB script if we created one
        if temp_lldb_script and os.path.exists(temp_lldb_script):
            try:
                os.remove(temp_lldb_script)
                logger.debug(f"Removed temporary LLDB script {temp_lldb_script}")
            except Exception as e:
                logger.warning(
                    f"Could not remove temporary LLDB script {temp_lldb_script}: {e}"
                )

    return exit_code


def do_generate_coverage_report(
    specific_file: Optional[str] = None, prefix: Optional[str] = None
) -> bool:
    """Generate a coverage report if the build was instrumented for coverage.

    Args:
        specific_file: If provided, show coverage for just this file
        prefix: If provided, only include profraw files with this prefix

    Returns:
        bool: True if report generation was successful
    """
    build_dir = os.path.join(get_xahaud_root(), "build")

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


@click.command(context_settings=dict(ignore_unknown_options=True))
@click.option(
    "--log-level",
    type=click.Choice(
        ["debug", "info", "warning", "error", "critical"], case_sensitive=False
    ),
    default="info",
    help="Set the logging level",
)
@click.option(
    "--build-jshooks-header/--no-build-jshooks-header",
    is_flag=True,
    default=False,
    help="Build JS hooks header",
)
@click.option(
    "--lldb/--no-lldb", is_flag=True, default=False, help="Run with lldb debugger"
)
@click.option(
    "--lldb-commands-file",
    default=None,
    help="File containing lldb commands to run before running rippled",
)
@click.option("--times", default=1, type=int, help="Number of times to run the command")
@click.option("--build/--no-build", default=True, is_flag=True, help="Build rippled")
@click.option(
    "--stop-on-fail/--no-stop-on-fail",
    is_flag=True,
    default=True,
    help="Stop on first failure (--no-stop-on-fail to continue on failures)",
)
@click.option(
    "--reconfigure-build/--no-reconfigure-build",
    is_flag=True,
    default=False,
    help="Force CMake reconfiguration even if build directory exists",
)
@click.option(
    "--coverage/--no-coverage",
    is_flag=True,
    default=False,
    help="Build with code coverage support",
)
@click.option(
    "--conan/--no-conan",
    is_flag=True,
    default=False,
    help="Use Conan package manager for dependencies",
)
@click.option(
    "--verbose/--no-verbose",
    is_flag=True,
    default=False,
    help="Enable verbose output during build",
)
@click.option(
    "--generate-coverage-report/--no-generate-coverage-report",
    is_flag=True,
    default=False,
    help="Generate coverage report after tests finish (only if --coverage was used)",
)
@click.option(
    "--coverage-file",
    default=None,
    help="Generate coverage report for a specific source file",
)
@click.option(
    "--ccache/--no-ccache",
    is_flag=True,
    default=False,
    help="Use ccache to speed up compilation",
)
@click.argument("rippled_args", nargs=-1, type=click.UNPROCESSED)
def main(
    log_level,
    build_jshooks_header,
    lldb,
    lldb_commands_file,
    times,
    stop_on_fail,
    rippled_args,
    build,
    reconfigure_build,
    coverage,
    conan,
    verbose,
    generate_coverage_report,
    coverage_file,
    ccache,
):
    """Build and run rippled tests with support for debugging and coverage analysis.

    Examples:
        # Run a basic unit test
        run_tests.py unit_test_hook

        # Build with coverage and generate report
        run_tests.py --coverage --generate-coverage-report unit_test_hook

        # Run with debugger
        run_tests.py --lldb unit_test_hook

        # Build with conan and ccache
        run_tests.py --conan --ccache --reconfigure-build unit_test_hook

        # Run multiple times
        run_tests.py --times 5 --no-stop-on-fail unit_test_hook
    """
    # Set up logging first
    setup_logging(log_level)

    logger.info("Starting run_tests.py")
    logger.debug(f"Command line arguments: {' '.join(sys.argv[1:])}")

    try:
        # Verify lldb_commands_file path if provided
        if lldb_commands_file:
            if not os.path.isabs(lldb_commands_file):
                # relative to the current directory before changing to xahaud root
                original_dir = os.getcwd()
                lldb_commands_file = os.path.join(
                    os.path.abspath(original_dir), lldb_commands_file
                )
                logger.debug(
                    f"Resolved relative lldb_commands_file path to: {lldb_commands_file}"
                )

            if not os.path.exists(lldb_commands_file):
                logger.error(f"LLDB commands file not found: {lldb_commands_file}")
                raise click.ClickException(f"File not found: {lldb_commands_file}")

            lldb = True  # automatically enable lldb if a commands file is provided
            logger.debug(
                "Automatically enabled lldb mode due to lldb_commands_file being specified"
            )

        # Change to xahaud root directory
        xahaud_root = get_xahaud_root()
        logger.info(f"Using xahaud root directory: {xahaud_root}")

        with change_directory(xahaud_root):
            # Build JS hooks header if needed
            if build_jshooks_header:
                logger.info("Building JS hooks header...")
                do_build_jshooks_header()

            # Build rippled
            if build:
                logger.info("Building rippled...")
                build_successful = build_rippled(
                    reconfigure_build=reconfigure_build,
                    coverage=coverage,
                    use_conan=conan,
                    verbose=verbose,
                    use_ccache=ccache,
                )

                if not build_successful:
                    logger.error("Build failed, cannot run tests")
                    sys.exit(1)
            else:
                logger.info("Skipping build as requested")

            # Run rippled with the appropriate arguments
            logger.info(f"Running rippled with args: {' '.join(rippled_args)}")
            env = os.environ.copy()
            if coverage:
                # Generate a random prefix for coverage files
                coverage_prefix = generate_random_prefix()
                logger.info(f"Using coverage file prefix: {coverage_prefix}")

                # Set LLVM_PROFILE_FILE environment variable to handle process forking
                env["LLVM_PROFILE_FILE"] = f"{coverage_prefix}.%p.profraw"
                logger.debug(f"Set LLVM_PROFILE_FILE={env['LLVM_PROFILE_FILE']}")

            exit_code = run_rippled(
                list(rippled_args),
                lldb,
                times,
                stop_on_fail,
                lldb_commands_file,
                env=env,
            )

            # Generate coverage report if requested
            if (generate_coverage_report or coverage_file) and coverage:
                logger.info("Generating coverage report...")
                do_generate_coverage_report(
                    specific_file=coverage_file, prefix=coverage_prefix
                )

            # Return the exit code from the last process
            logger.info(f"Exiting with code {exit_code}")
            sys.exit(exit_code)

    except click.ClickException as e:
        logger.error(f"Command line error: {e}")
        raise
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed with exit code {e.returncode}")
        sys.exit(e.returncode)
    except Exception as e:
        logger.critical(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
