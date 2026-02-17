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
import os
import subprocess
import sys
from pathlib import Path

import click

from xahaud_scripts.build import (
    CMakeOptions,
    ccache_show_stats,
    ccache_zero_stats,
    check_config_mismatch,
    cmake_build,
    cmake_configure,
    conan_install,
    generate_coverage_prefix,
)
from xahaud_scripts.build import (
    ccache_show_config as _ccache_show_config,
)
from xahaud_scripts.utils.coverage import do_generate_coverage_report
from xahaud_scripts.utils.lldb import create_lldb_script
from xahaud_scripts.utils.logging import make_logger, setup_logging
from xahaud_scripts.utils.paths import get_xahaud_root
from xahaud_scripts.utils.shell_utils import (
    change_directory,
    check_tool_exists,
    run_command,
)

# Set up logger
logger = make_logger(__name__)


def detect_coverage_version(xahaud_root: str) -> str:
    """Auto-detect coverage version from CMake files in the worktree.

    Returns 'v2' if CodeCoverage.cmake exists (PR #661 gcovr-based),
    otherwise 'v1' (llvm-cov based, origin/dev).
    """
    code_coverage_cmake = os.path.join(
        xahaud_root, "Builds", "CMake", "CodeCoverage.cmake"
    )
    if os.path.exists(code_coverage_cmake):
        logger.info("Auto-detected coverage v2 (CodeCoverage.cmake found)")
        return "v2"
    logger.info("Auto-detected coverage v1 (no CodeCoverage.cmake)")
    return "v1"


def do_build_jshooks_header() -> None:
    """Build the JS hooks header."""
    logger.info("Building JS hooks header...")

    try:
        run_command(["build-jshooks-header", "--canonical"])
        logger.info("JS hooks header built successfully")
    except Exception as e:
        logger.error(f"Failed to build JS hooks header: {e}")
        raise


def build_rippled(
    reconfigure_build: bool = False,
    coverage: bool = False,
    coverage_version: str = "v1",
    use_conan: bool = True,
    verbose: bool = False,
    use_ccache: bool = False,
    ccache_basedir: str | None = None,
    ccache_sloppy: bool = False,
    ccache_debug: bool = False,
    target: str = "rippled",
    log_line_numbers: bool = True,
    build_type: str = "Debug",
    dry_run: bool = False,
    unity: bool = False,
) -> bool:
    """Build the rippled executable.

    Args:
        reconfigure_build: If True, force CMake reconfiguration even if build directory exists
        coverage: If True, enable code coverage
        use_conan: If True, use Conan package manager for dependencies
        verbose: If True, enable verbose output during build
        use_ccache: If True, use ccache to speed up compilation
        ccache_basedir: Base directory for ccache path normalization (cache sharing)
        ccache_sloppy: If True, ignore locale, __DATE__, __TIME__ differences
        ccache_debug: If True, enable ccache debug logging
        target: Build target (e.g., rippled, xrpld)
        log_line_numbers: If True, enable BEAST_ENHANCED_LOGGING
        build_type: CMake build type (Debug or Release)
        dry_run: If True, print commands without executing
        unity: If True, enable unity builds (faster clean builds, slower incremental)

    Returns:
        bool: True if build was successful, False otherwise
    """
    xahaud_root = get_xahaud_root()
    build_dir = os.path.join(xahaud_root, "build")
    logger.info(f"Building {target} in {build_dir}")

    # Check if build directory exists
    build_dir_exists = os.path.exists(build_dir)

    # Determine if we need to configure
    need_configure = not build_dir_exists or reconfigure_build

    # Check for configuration mismatch if not reconfiguring
    if build_dir_exists and not need_configure:
        check_config_mismatch(
            build_dir=build_dir,
            coverage=coverage,
            use_conan=use_conan,
            verbose=verbose,
            ccache=use_ccache,
            build_type=build_type,
        )

    # Create build directory if needed
    if not dry_run:
        os.makedirs(build_dir, exist_ok=True)

    # Run conan install if requested
    if use_conan and need_configure:
        success = conan_install(
            xahaud_root=xahaud_root,
            build_type=build_type,
            dry_run=dry_run,
        )
        if not success:
            return False

    # Configure cmake if needed
    if need_configure:
        options = CMakeOptions(
            build_type=build_type,
            coverage=coverage,
            coverage_version=coverage_version,
            verbose=verbose,
            ccache=use_ccache,
            ccache_basedir=ccache_basedir,
            ccache_sloppy=ccache_sloppy,
            ccache_debug=ccache_debug,
            log_line_numbers=log_line_numbers,
            use_conan=use_conan,
            unity=unity,
        )
        if not cmake_configure(build_dir, options, dry_run=dry_run):
            return False

    # Build the target
    return cmake_build(
        build_dir,
        target=target,
        verbose=verbose,
        dry_run=dry_run,
        ccache=use_ccache,
        ccache_basedir=ccache_basedir,
        ccache_sloppy=ccache_sloppy,
    )


def run_rippled(
    args: list[str],
    use_lldb: bool,
    times: int = 1,
    stop_on_fail: bool = True,
    lldb_commands_file: str | None = None,
    env: dict | None = None,
    lldb_all_threads: bool = False,
) -> int:
    """Run the rippled executable, optionally with lldb, multiple times.

    Args:
        args: Arguments to pass to rippled
        use_lldb: Whether to run with lldb
        times: Number of times to run the command
        stop_on_fail: Whether to stop on first failure
        lldb_commands_file: Path to LLDB commands file
        env: Environment variables to set for the process
        lldb_all_threads: Whether to show all threads in LLDB backtrace

    Returns:
        int: the exit code of the last run
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
        temp_lldb_script = create_lldb_script(all_threads=lldb_all_threads)
        lldb_commands_file = temp_lldb_script
        logger.info(
            f"Created default LLDB script at {lldb_commands_file} (all_threads={lldb_all_threads})"
        )

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


@click.command()
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
    "--compile-hooks",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Compile WASM hooks from test file before building (e.g., Export_test.cpp)",
)
@click.option(
    "--lldb/--no-lldb",
    is_flag=True,
    default=False,
    help="Run with lldb debugger (shows crashing thread backtrace)",
)
@click.option(
    "--lldb-all-threads/--no-lldb-all-threads",
    is_flag=True,
    default=False,
    help="Run with lldb debugger (shows all threads backtrace)",
)
@click.option(
    "--lldb-commands-file",
    default=None,
    help="File containing lldb commands to run before running rippled",
)
@click.option("--times", default=2, type=int, help="Number of times to run the command")
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
    "--dry-run/--no-dry-run",
    is_flag=True,
    default=False,
    help="Print commands without executing them (shows all flags that would be passed)",
)
@click.option(
    "--coverage/--no-coverage",
    is_flag=True,
    default=False,
    help="Build with code coverage support",
)
@click.option(
    "--coverage-version",
    type=click.Choice(["v1", "v2", "auto"], case_sensitive=False),
    default="auto",
    help="Coverage system: 'v1' for llvm-cov (origin/dev), 'v2' for gcovr (PR #661), 'auto' to detect from worktree. Default: auto.",
)
@click.option(
    "--conan/--no-conan",
    is_flag=True,
    default=True,
    help="Use Conan package manager for dependencies (default: enabled)",
)
@click.option(
    "--verbose/--no-verbose",
    is_flag=True,
    default=False,
    help="Enable verbose output during build",
)
@click.option(
    "--unity/--no-unity",
    is_flag=True,
    default=False,
    help="Enable unity builds (faster clean builds, slower incremental; default: off)",
)
@click.option(
    "--generate-coverage-report/--no-generate-coverage-report",
    is_flag=True,
    default=False,
    help="Deprecated: reports are now generated automatically when coverage is enabled.",
)
@click.option(
    "--coverage-file",
    default=None,
    help="Generate coverage report for a specific source file",
)
@click.option(
    "--ccache/--no-ccache",
    is_flag=True,
    default=None,
    help="Use ccache to speed up compilation",
)
@click.option(
    "--ccache-basedir",
    default=".",
    help="Base directory for ccache path normalization (default: . for worktree sharing)",
)
@click.option(
    "--ccache-sloppy/--no-ccache-sloppy",
    is_flag=True,
    default=True,
    help="Ignore locale and __DATE__/__TIME__ differences in ccache (default: on)",
)
@click.option(
    "--ccache-debug/--no-ccache-debug",
    is_flag=True,
    default=False,
    help="Enable ccache debug logging to ~/.config/xahaud-scripts/ccache-<timestamp>.log",
)
@click.option(
    "--ccache-stats/--no-ccache-stats",
    is_flag=True,
    default=True,
    help="Show ccache stats after build (default: on when using --ccache)",
)
@click.option(
    "--ccache-show-config",
    is_flag=True,
    default=False,
    help="Show ccache config after build stats",
)
@click.option(
    "--target",
    default="rippled",
    help="Build target (e.g., rippled, xrpld)",
)
@click.option(
    "--log-line-numbers/--no-log-line-numbers",
    is_flag=True,
    default=True,
    help="Enable/disable log line numbers (default: enabled)",
)
@click.option(
    "--build-type",
    type=click.Choice(["Debug", "Release", "Coverage"], case_sensitive=False),
    default="Debug",
    help="CMake build type: Debug, Release, or Coverage (Debug + coverage instrumentation + report).",
)
@click.option(
    "--keep-gcda/--no-keep-gcda",
    is_flag=True,
    default=False,
    help="Keep .gcda files from previous runs (default: clear before tests for clean coverage)",
)
@click.option(
    "--diff-cover/--no-diff-cover",
    is_flag=True,
    default=False,
    help="Show coverage for lines changed since --diff-cover-since",
)
@click.option(
    "--diff-cover-since",
    default="origin/dev",
    help="Commitish for --diff-cover comparison (default: origin/dev)",
)
@click.option(
    "--diff-cover-context",
    type=int,
    default=3,
    help="Context lines around uncovered regions (default: 3)",
)
@click.argument("rippled_args", nargs=-1, type=click.UNPROCESSED)
def main(
    log_level,
    build_jshooks_header,
    compile_hooks,
    lldb,
    lldb_all_threads,
    lldb_commands_file,
    times,
    stop_on_fail,
    rippled_args,
    build,
    reconfigure_build,
    dry_run,
    coverage,
    coverage_version,
    conan,
    verbose,
    unity,
    generate_coverage_report,
    coverage_file,
    ccache,
    ccache_basedir,
    ccache_sloppy,
    ccache_debug,
    ccache_stats,
    ccache_show_config,
    target,
    log_line_numbers,
    build_type,
    keep_gcda,
    diff_cover,
    diff_cover_since,
    diff_cover_context,
):
    """Build and run rippled tests with support for debugging and coverage analysis.

    Use -- to separate run-tests options from rippled arguments.

    Examples:
        # Run a basic unit test
        run-tests -- unit_test_hook

        # Build with coverage (auto-generates report)
        run-tests --build-type Coverage --reconfigure-build -- unit_test_hook

        # Coverage with diff-cover against origin/dev
        run-tests --build-type Coverage --diff-cover -- unit_test_hook

        # Explicit coverage flags (equivalent to --build-type Coverage)
        run-tests --coverage --reconfigure-build -- unit_test_hook

        # Run with debugger
        run-tests --lldb -- unit_test_hook

        # Build with ccache (cache sharing between worktrees enabled by default)
        run-tests --ccache --reconfigure-build -- unit_test_hook

        # Run multiple times
        run-tests --times 5 --no-stop-on-fail -- unit_test_hook

        # Build xrpld target instead of rippled
        run-tests --target xrpld -- unit_test_hook

        # Build with Release build type
        run-tests --build-type Release -- unit_test_hook

        # Dry run - show all commands without executing
        run-tests --dry-run --reconfigure-build -- unit_test_hook

        # Just build, no tests
        run-tests --times=0 --build-type Release --reconfigure-build
    """
    # Set up logging first
    setup_logging(log_level, logger)

    # Resolve --build-type=Coverage → coverage + Debug
    if build_type.lower() == "coverage":
        coverage = True
        build_type = "Debug"
        logger.info(
            "--build-type=Coverage: enabling coverage instrumentation with Debug build"
        )

    # Auto-enable coverage when diff-cover is requested
    if diff_cover and not coverage:
        logger.info(
            "--diff-cover implies --coverage, enabling coverage instrumentation"
        )
        coverage = True

    # Resolve coverage version (auto-detect if needed)
    if coverage and coverage_version == "auto":
        try:
            xahaud_root = get_xahaud_root()
            coverage_version = detect_coverage_version(xahaud_root)
        except Exception:
            logger.warning("Could not auto-detect coverage version, falling back to v1")
            coverage_version = "v1"

    # Coverage forces Debug in cmake (RippledSettings.cmake), and conan
    # generator expressions are config-specific ($<$<CONFIG:Release>:...>)
    # so conan and cmake must agree on build type or includes vanish.
    if coverage and build_type.lower() != "debug":
        raise click.UsageError(
            f"--coverage requires Debug build, but --build-type={build_type} was specified. "
            "Coverage instrumentation only works with --build-type=Debug (the default)."
        )

    # Check environment variable for ccache if not explicitly set
    if ccache is None:
        ccache = os.environ.get("RUN_TESTS_CCACHE", "").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if ccache:
            logger.info("Enabled ccache from RUN_TESTS_CCACHE environment variable")

    # Convert Path objects to strings for JSON serialization
    args_dict = {k: str(v) if isinstance(v, Path) else v for k, v in locals().items()}
    logger.info(
        f"Starting run_tests.py, running cmd with {json.dumps(args_dict, indent=2)}"
    )

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

            if not lldb and not lldb_all_threads:
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

            # Compile WASM hooks from test file if requested
            if compile_hooks:
                logger.info(f"Compiling WASM hooks from {compile_hooks}...")
                try:
                    run_command(["x-build-test-hooks", str(compile_hooks)])
                    logger.info("WASM hooks compiled successfully")
                except Exception as e:
                    logger.error(f"Failed to compile WASM hooks: {e}")
                    raise

            # Build rippled
            if build or dry_run:
                logger.info("Building rippled...")

                # Resolve ccache_basedir to absolute path if provided
                resolved_ccache_basedir = None
                if ccache_basedir:
                    resolved_ccache_basedir = os.path.abspath(ccache_basedir)
                    logger.debug(
                        f"Resolved ccache_basedir to: {resolved_ccache_basedir}"
                    )

                # Zero ccache stats before build if requested
                if ccache_stats and ccache and not dry_run:
                    ccache_zero_stats()

                build_successful = build_rippled(
                    reconfigure_build=reconfigure_build or dry_run,
                    coverage=coverage,
                    coverage_version=coverage_version,
                    use_conan=conan,
                    verbose=verbose,
                    use_ccache=ccache,
                    ccache_basedir=resolved_ccache_basedir,
                    ccache_sloppy=ccache_sloppy,
                    ccache_debug=ccache_debug,
                    target=target,
                    log_line_numbers=log_line_numbers,
                    build_type=build_type,
                    dry_run=dry_run,
                    unity=unity,
                )

                # Show ccache stats after build if requested
                if ccache_stats and ccache and not dry_run:
                    ccache_show_stats()
                    if ccache_show_config:
                        _ccache_show_config()

                if not build_successful:
                    logger.error("Build failed, cannot run tests")
                    sys.exit(1)

                if dry_run:
                    logger.info("Dry run complete - no commands were executed")
                    sys.exit(0)
            else:
                logger.info("Skipping build as requested")

            # Clear stale .gcda files before test runs for clean coverage
            if coverage and coverage_version == "v2" and not keep_gcda:
                from pathlib import Path as _Path

                gcda_files = list(_Path(build_dir).rglob("*.gcda"))
                if gcda_files:
                    logger.info(
                        f"Clearing {len(gcda_files)} .gcda files from previous runs..."
                    )
                    for f in gcda_files:
                        f.unlink()

            # Run rippled with the appropriate arguments
            logger.info(f"Running rippled with args: {' '.join(rippled_args)}")
            env = os.environ.copy()
            coverage_prefix = None
            if coverage and coverage_version == "v1":
                # v1: Generate a random prefix for llvm-cov profraw files
                coverage_prefix = generate_coverage_prefix()
                logger.info(f"Using coverage file prefix: {coverage_prefix}")

                # Set LLVM_PROFILE_FILE environment variable to handle process forking
                env["LLVM_PROFILE_FILE"] = f"{coverage_prefix}.%p.profraw"
                logger.debug(f"Set LLVM_PROFILE_FILE={env['LLVM_PROFILE_FILE']}")

            # Determine which lldb mode to use
            use_lldb = lldb or lldb_all_threads

            exit_code = run_rippled(
                list(rippled_args),
                use_lldb,
                times,
                stop_on_fail,
                lldb_commands_file,
                env=env,
                lldb_all_threads=lldb_all_threads,
            )

            # Generate coverage report automatically when coverage is enabled
            if coverage:
                if coverage_version == "v2":
                    # v2: run gcovr directly on .gcda files
                    from xahaud_scripts.utils.coverage_diff import (
                        do_generate_coverage_report_v2,
                    )

                    do_generate_coverage_report_v2(
                        build_dir=build_dir,
                    )
                else:
                    # v1: use llvm-cov
                    logger.info("Generating coverage report (v1/llvm-cov)...")
                    do_generate_coverage_report(
                        build_dir=build_dir,
                        specific_file=coverage_file,
                        prefix=coverage_prefix,
                    )

            # Generate diff coverage report if requested
            if diff_cover and coverage:
                if coverage_version == "v2":
                    from xahaud_scripts.utils.coverage_diff import (
                        do_diff_coverage_report_v2,
                    )

                    logger.info(
                        f"Generating diff coverage report via gcovr (since {diff_cover_since})..."
                    )
                    do_diff_coverage_report_v2(
                        build_dir=build_dir,
                        commitish=diff_cover_since,
                        context_lines=diff_cover_context,
                    )
                else:
                    from xahaud_scripts.utils.coverage_diff import (
                        do_diff_coverage_report,
                    )

                    logger.info(
                        f"Generating diff coverage report (since {diff_cover_since})..."
                    )
                    do_diff_coverage_report(
                        build_dir=build_dir,
                        commitish=diff_cover_since,
                        prefix=coverage_prefix,
                        context_lines=diff_cover_context,
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
