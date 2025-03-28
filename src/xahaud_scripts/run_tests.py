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

import os
import subprocess
import sys
import time
from typing import List, Optional

import click

from xahaud_scripts.utils.coverage import do_generate_coverage_report
from xahaud_scripts.utils.logging import setup_logging, make_logger
from xahaud_scripts.utils.paths import get_xahaud_root
from xahaud_scripts.utils.shell_utils import (
    check_tool_exists,
    run_command,
    get_logical_cpu_count,
    change_directory,
    create_lldb_script,
)

# Set up logger
logger = make_logger(__name__)


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
    except Exception as e:
        logger.warning(f"Could not analyze previous build configuration: {e}")

    return config


def generate_coverage_prefix() -> str:
    """Generate a prefix for coverage files to identify specific test runs."""
    timestamp_ms = int(time.time() * 1000)
    return "coverage_run_" + str(timestamp_ms)


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
            # TODO: prompt user to confirm ?
            # click.prompt("Need to reconfigure")

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
    setup_logging(log_level, logger)

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
        build_dir = os.path.join(xahaud_root, "build")
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
                coverage_prefix = generate_coverage_prefix()
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
                    build_dir=build_dir,
                    specific_file=coverage_file,
                    prefix=coverage_prefix,
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
