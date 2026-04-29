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
)
from xahaud_scripts.build import (
    ccache_show_config as _ccache_show_config,
)
from xahaud_scripts.utils.lldb import create_lldb_script
from xahaud_scripts.utils.logging import make_logger, setup_logging
from xahaud_scripts.utils.paths import get_xahaud_root
from xahaud_scripts.utils.shell_utils import (
    change_directory,
    check_tool_exists,
    run_command,
)

OUTPUTS_DIR = Path.home() / ".config" / "xahaud-scripts" / "outputs"

# Set up logger
logger = make_logger(__name__)


def get_build_output_path(xahaud_root: str, build_type: str) -> Path:
    """Return the tee file path for this worktree + build type."""
    slug = f"{Path(xahaud_root).name}-{build_type.lower()}"
    return OUTPUTS_DIR / f"{slug}.txt"


def do_build_jshooks_header(tee_file: Path | None = None) -> None:
    """Build the JS hooks header."""
    logger.info("Building JS hooks header...")

    try:
        run_command(["build-jshooks-header", "--canonical"], tee_file=tee_file)
        logger.info("JS hooks header built successfully")
    except Exception as e:
        logger.error(f"Failed to build JS hooks header: {e}")
        raise


def build_rippled(
    reconfigure_build: bool = False,
    coverage: bool = False,
    use_conan: bool = True,
    verbose: bool = False,
    use_ccache: bool = False,
    ccache_basedir: str | None = None,
    ccache_sloppy: bool = False,
    ccache_debug: bool = False,
    target: str = "rippled",
    log_line_numbers: bool = True,
    build_type: str = "Release",
    dry_run: bool = False,
    unity: bool = False,
    build_dir: str | None = None,
    tee_file: Path | None = None,
    jobs: int | None = None,
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
        build_dir: Build directory (default: build-debug for Debug, build for Release)

    Returns:
        bool: True if build was successful, False otherwise
    """
    xahaud_root = get_xahaud_root()
    if build_dir is None:
        dir_name = "build-debug" if build_type.lower() == "debug" else "build"
        build_dir = os.path.join(xahaud_root, dir_name)
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
            verbose=verbose,
            ccache=use_ccache,
            ccache_basedir=ccache_basedir,
            ccache_sloppy=ccache_sloppy,
            ccache_debug=ccache_debug,
            log_line_numbers=log_line_numbers,
            use_conan=use_conan,
            unity=unity,
        )
        if not cmake_configure(build_dir, options, dry_run=dry_run, tee_file=tee_file):
            return False

    # Build the target
    return cmake_build(
        build_dir,
        target=target,
        verbose=verbose,
        parallel=jobs,
        dry_run=dry_run,
        ccache=use_ccache,
        ccache_basedir=ccache_basedir,
        ccache_sloppy=ccache_sloppy,
        tee_file=tee_file,
    )


def run_rippled(
    args: list[str],
    use_lldb: bool,
    times: int = 1,
    stop_on_fail: bool = True,
    lldb_commands_file: str | None = None,
    env: dict | None = None,
    lldb_all_threads: bool = False,
    build_dir: str | None = None,
    tee_file: Path | None = None,
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
        build_dir: Build directory containing the rippled executable

    Returns:
        int: the exit code of the last run
    """
    if build_dir is None:
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
                process = run_command(
                    cmd,
                    check=False,
                    env=env,
                    tee_file=tee_file if not use_lldb else None,
                )
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
    "--hooks-c-dir",
    "hooks_c_dir",
    multiple=True,
    help="Hook source dirs as domain=path (e.g. tipbot=/path/to/hooks). Repeatable.",
)
@click.option(
    "--hook-coverage/--no-hook-coverage",
    is_flag=True,
    default=False,
    help="Compile WASM hooks with SanitizerCoverage instrumentation.",
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
    "--dry-run/--no-dry-run",
    is_flag=True,
    default=False,
    help="Print commands without executing them (shows all flags that would be passed)",
)
@click.option(
    "--coverage/--no-coverage",
    is_flag=True,
    default=False,
    help="Build with code coverage support (gcov/gcovr).",
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
    default="Release",
    help="CMake build type: Debug, Release, or Coverage (Debug + coverage instrumentation + report).",
)
@click.option(
    "--build-dir",
    default=None,
    help="Build directory name (default: build-debug for Debug, build for Release).",
)
@click.option(
    "-j",
    "--jobs",
    type=int,
    default=None,
    help="Parallel build jobs (default: CPU count).",
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
    hooks_c_dir,
    hook_coverage,
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
    conan,
    verbose,
    unity,
    ccache,
    ccache_basedir,
    ccache_sloppy,
    ccache_debug,
    ccache_stats,
    ccache_show_config,
    target,
    log_line_numbers,
    build_type,
    build_dir,
    jobs,
    keep_gcda,
    diff_cover,
    diff_cover_since,
    diff_cover_context,
):
    """Build and run rippled tests with support for debugging and coverage analysis.

    The trailing positional arg(s) are passed straight to rippled as the
    --unittest filter. Multiple suites can be combined CSV-style — rippled
    parses one comma-separated string into multiple suites in a single run.

    The leading `--` is conventional but optional; anything after the known
    options is forwarded as rippled args.

    Suite-spec examples:
        # Single suite
        x-run-tests -- ripple.rpc.Catalogue

        # Multiple suites in one run (CSV — rippled native)
        x-run-tests -- ripple.core.Config,ripple.rdb.RelationalDatabase,ripple.rpc.Catalogue

        # Same, no leading `--`
        x-run-tests ripple.core.Config,ripple.rdb.RelationalDatabase

    Examples:
        # Run a basic unit test
        x-run-tests -- unit_test_hook

        # Build with coverage (auto-generates report)
        x-run-tests --build-type Coverage --reconfigure-build -- unit_test_hook

        # Coverage with diff-cover against origin/dev
        x-run-tests --build-type Coverage --diff-cover -- unit_test_hook

        # Explicit coverage flags (equivalent to --build-type Coverage)
        x-run-tests --coverage --reconfigure-build -- unit_test_hook

        # Run with debugger
        x-run-tests --lldb -- unit_test_hook

        # Build with ccache (cache sharing between worktrees enabled by default)
        x-run-tests --ccache --reconfigure-build -- unit_test_hook

        # Run multiple times
        x-run-tests --times 5 --no-stop-on-fail -- unit_test_hook

        # Build xrpld target instead of rippled
        x-run-tests --target xrpld -- unit_test_hook

        # Build with Release build type
        x-run-tests --build-type Release -- unit_test_hook

        # Dry run - show all commands without executing
        x-run-tests --dry-run --reconfigure-build -- unit_test_hook

        # Just build, no tests
        x-run-tests --times=0 --build-type Release --reconfigure-build
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

    # Coverage forces Debug in cmake (RippledSettings.cmake), and conan
    # generator expressions are config-specific ($<$<CONFIG:Release>:...>)
    # so conan and cmake must agree on build type or includes vanish.
    if coverage and build_type.lower() != "debug":
        raise click.UsageError(
            f"--coverage requires Debug build, but --build-type={build_type} was specified. "
            "Coverage instrumentation only works with --build-type=Debug."
        )

    # Check environment variable for ccache if not explicitly set
    if ccache is None:
        if os.environ.get("RUN_TESTS_CCACHE", "").lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            ccache = True
            logger.info("Enabled ccache from RUN_TESTS_CCACHE environment variable")
        elif reconfigure_build:
            ccache = True
            logger.info("Enabled ccache (default when --reconfigure-build)")
        else:
            ccache = False

    # Convert Path objects to strings for JSON serialization
    args_dict = {k: str(v) if isinstance(v, Path) else v for k, v in locals().items()}
    logger.info(
        f"Starting run_tests.py, running cmd with {json.dumps(args_dict, indent=2)}"
    )

    logger.debug(f"Command line arguments: {' '.join(sys.argv[1:])}")

    # Set up run recorder
    from xahaud_scripts.utils.runs_db import RunRecorder

    recorder = RunRecorder(
        worktree=get_xahaud_root(),
        target=target,
        build_type=build_type,
        test_suite=" ".join(rippled_args) if rippled_args else None,
        times=times,
        coverage=coverage,
        ccache=ccache,
        unity=unity,
        dry_run=dry_run,
        cli_args=" ".join(sys.argv[1:]),
    )

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
        if build_dir is None:
            dir_name = "build-debug" if build_type.lower() == "debug" else "build"
            build_dir = os.path.join(xahaud_root, dir_name)
        else:
            build_dir = os.path.join(xahaud_root, build_dir)
        logger.info(f"Using xahaud root directory: {xahaud_root}")
        logger.info(f"Build directory: {build_dir}")
        tee_file = get_build_output_path(xahaud_root, build_type)
        tee_file.parent.mkdir(parents=True, exist_ok=True)
        tee_file.write_text("")  # truncate at session start
        logger.info(f"Output tee: {tee_file}")

        with change_directory(xahaud_root):
            # Build JS hooks header if needed
            if build_jshooks_header:
                logger.info("Building JS hooks header...")
                do_build_jshooks_header(tee_file=tee_file)

            # Compile WASM hooks from test file if requested
            if compile_hooks:
                logger.info(f"Compiling WASM hooks from {compile_hooks}...")
                try:
                    cmd = ["hookz", "build-test-hooks", str(compile_hooks)]
                    for entry in hooks_c_dir:
                        cmd.extend(["--hooks-c-dir", entry])
                    if hook_coverage:
                        cmd.append("--hook-coverage")
                    run_command(cmd, tee_file=tee_file)
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

                recorder.build_started()
                build_successful = build_rippled(
                    reconfigure_build=reconfigure_build or dry_run,
                    coverage=coverage,
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
                    build_dir=build_dir,
                    tee_file=tee_file,
                    jobs=jobs,
                )
                recorder.build_finished(build_successful)

                # Show ccache stats after build if requested
                if ccache_stats and ccache and not dry_run:
                    ccache_show_stats()
                    if ccache_show_config:
                        _ccache_show_config()

                if not build_successful:
                    logger.error("Build failed, cannot run tests")
                    recorder.save()
                    sys.exit(1)

                if dry_run:
                    logger.info("Dry run complete - no commands were executed")
                    recorder.save()
                    sys.exit(0)
            else:
                logger.info("Skipping build as requested")

            # Clear stale .gcda files before test runs for clean coverage
            if coverage and not keep_gcda:
                from pathlib import Path as _Path

                gcda_files = list(_Path(build_dir).rglob("*.gcda"))
                if gcda_files:
                    logger.info(
                        f"Clearing {len(gcda_files)} .gcda files from previous runs..."
                    )
                    for f in gcda_files:
                        f.unlink()

            # Strip accidental --unittest / -u from rippled args (already added by run_rippled)
            rippled_args = list(rippled_args)
            if rippled_args and rippled_args[0] in ("--unittest", "-u"):
                logger.warning(
                    f"Stripping redundant '{rippled_args[0]}' from args (-u is added automatically)"
                )
                rippled_args = rippled_args[1:]
            elif rippled_args and rippled_args[0].startswith("--unittest="):
                # --unittest=SuiteName → just keep SuiteName
                suite = rippled_args[0].split("=", 1)[1]
                logger.warning(
                    "Stripping redundant '--unittest=' from args (-u is added automatically)"
                )
                rippled_args = [suite] + rippled_args[1:]

            # Run rippled with the appropriate arguments
            logger.info(f"Running rippled with args: {' '.join(rippled_args)}")
            env = os.environ.copy()

            # Determine which lldb mode to use
            use_lldb = lldb or lldb_all_threads

            if times > 0:
                recorder.test_started()
            exit_code = run_rippled(
                list(rippled_args),
                use_lldb,
                times,
                stop_on_fail,
                lldb_commands_file,
                env=env,
                lldb_all_threads=lldb_all_threads,
                build_dir=build_dir,
                tee_file=tee_file,
            )
            if times > 0:
                recorder.test_finished(exit_code)

            # Generate coverage report automatically when coverage is enabled
            # (skip if no tests ran — nothing new to report on).
            if coverage and times > 0:
                from xahaud_scripts.utils.coverage_diff import (
                    do_generate_coverage_report_v2,
                )

                do_generate_coverage_report_v2(build_dir=build_dir)

            # Generate diff coverage report if requested
            if diff_cover and coverage and times > 0:
                from xahaud_scripts.utils.coverage_diff import (
                    do_diff_coverage_report_v2,
                )

                logger.info(
                    f"Generating diff coverage report via gcovr "
                    f"(since {diff_cover_since})..."
                )
                do_diff_coverage_report_v2(
                    build_dir=build_dir,
                    commitish=diff_cover_since,
                    context_lines=diff_cover_context,
                )
            elif coverage and times == 0:
                logger.info(
                    "Skipping coverage report (--times=0, no tests run). "
                    "Use x-coverage-report / x-coverage-diff against existing "
                    ".gcda data when ready."
                )

            # Return the exit code from the last process
            logger.info(f"Exiting with code {exit_code}")
            recorder.save()
            sys.exit(exit_code)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        recorder.interrupted()
        recorder.save()
        sys.exit(130)
    except click.ClickException as e:
        logger.error(f"Command line error: {e}")
        recorder.save()
        raise
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed with exit code {e.returncode}")
        recorder.save()
        sys.exit(e.returncode)
    except Exception as e:
        logger.critical(f"Unexpected error: {e}", exc_info=True)
        recorder.save()
        sys.exit(1)


@click.command()
@click.option(
    "--build-type",
    type=click.Choice(["Debug", "Release", "Coverage"], case_sensitive=False),
    default="Release",
    help="Build type to follow (default: Release). Coverage maps to Debug.",
)
def tail_build(build_type: str) -> None:
    """Follow the build output log for this worktree.

    Runs tail -F on ~/.config/xahaud-scripts/outputs/<worktree>-<build-type>.txt.
    Waits for the file if it doesn't exist yet. Run from inside a xahaud worktree.

    Example:
        x-run-tests-tail
        x-run-tests-tail --build-type Debug
    """
    if build_type.lower() == "coverage":
        build_type = "Debug"
    try:
        xahaud_root = get_xahaud_root()
    except Exception as e:
        raise click.ClickException(f"Could not find xahaud root: {e}") from e

    tee_file = get_build_output_path(xahaud_root, build_type)
    click.echo(f"Following {tee_file}  (Ctrl+C to stop)", err=True)
    os.execvp("tail", ["tail", "-F", str(tee_file)])


def _resolve_build_dir(xahaud_root: str, build_type: str, build_dir: str | None) -> str:
    """Resolve the build dir (default: build-debug for Debug, build for Release)."""
    if build_dir is None:
        dir_name = "build-debug" if build_type.lower() == "debug" else "build"
        return os.path.join(xahaud_root, dir_name)
    return os.path.join(xahaud_root, build_dir)


def _has_gcda(build_dir: str) -> bool:
    """True if any .gcda exists under build_dir (short-circuits via find -quit)."""
    if not os.path.isdir(build_dir):
        return False
    result = subprocess.run(
        ["find", build_dir, "-name", "*.gcda", "-print", "-quit"],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


@click.command("coverage-diff")
@click.option(
    "--since",
    default="origin/dev",
    help="Commitish to diff against (default: origin/dev).",
)
@click.option(
    "--build-type",
    type=click.Choice(["Debug", "Release", "Coverage"], case_sensitive=False),
    default="Debug",
    help="Build type used (default: Debug). Coverage maps to Debug.",
)
@click.option(
    "--build-dir",
    default=None,
    help="Build directory (default: build-debug for Debug, build for Release).",
)
@click.option(
    "--context-lines",
    type=int,
    default=3,
    help="Source context lines around uncovered ranges (default: 3).",
)
@click.option(
    "--from-json",
    "from_json",
    default=None,
    help=(
        "Reuse an existing gcovr coverage.json instead of re-running gcovr "
        "(fast). Defaults to <build-dir>/coverage/coverage.json when present."
    ),
)
@click.option(
    "--regenerate",
    is_flag=True,
    help="Force regeneration (ignore any cached coverage.json).",
)
def coverage_diff(
    since: str,
    build_type: str,
    build_dir: str | None,
    context_lines: int,
    from_json: str | None,
    regenerate: bool,
) -> None:
    """Show diff coverage from accumulated .gcda data — no build, no test run.

    Uses gcovr against rippled's native coverage build (-Dcoverage=ON).
    """
    if build_type.lower() == "coverage":
        build_type = "Debug"
    try:
        xahaud_root = get_xahaud_root()
    except Exception as e:
        raise click.ClickException(f"Could not find xahaud root: {e}") from e

    build_dir = _resolve_build_dir(xahaud_root, build_type, build_dir)

    # Fast path: reuse an existing coverage.json instead of re-running gcovr
    # (gcovr on a rippled debug tree invokes gcov on thousands of .gcda files
    # and can take minutes — reusing a prior JSON is near-instant).
    cached_json = (
        Path(from_json) if from_json else Path(build_dir) / "coverage" / "coverage.json"
    )
    if not regenerate and cached_json.exists():
        from xahaud_scripts.utils.coverage_diff import (
            _parse_gcovr_line_coverage,
            compute_diff_coverage,
            display_diff_coverage,
            parse_diff_hunks,
        )

        logger.info(f"Reusing cached coverage data: {cached_json}")
        logger.info("(use --regenerate to force a fresh gcovr run)")
        diff_hunks = parse_diff_hunks(since, xahaud_root)
        if not diff_hunks:
            logger.info(f"No changes since {since}")
            return
        line_coverage = _parse_gcovr_line_coverage(cached_json)
        summary = compute_diff_coverage(diff_hunks, line_coverage, xahaud_root)
        display_diff_coverage(summary, xahaud_root, context_lines)
        return

    if not _has_gcda(build_dir):
        raise click.ClickException(
            f"No .gcda files under {build_dir}. "
            "Run an instrumented test run first (--coverage)."
        )

    from xahaud_scripts.utils.coverage_diff import do_diff_coverage_report_v2

    logger.info(f"Diff coverage via gcovr since {since} on {build_dir}")
    logger.info(
        "Note: gcovr will invoke gcov on every .gcda in the tree — "
        "this can take minutes on a debug build."
    )
    do_diff_coverage_report_v2(
        build_dir=build_dir,
        commitish=since,
        context_lines=context_lines,
    )


@click.command("coverage-report")
@click.option(
    "--build-type",
    type=click.Choice(["Debug", "Release", "Coverage"], case_sensitive=False),
    default="Debug",
    help="Build type used (default: Debug). Coverage maps to Debug.",
)
@click.option(
    "--build-dir",
    default=None,
    help="Build directory (default: build-debug for Debug, build for Release).",
)
@click.option(
    "--regenerate",
    is_flag=True,
    help=(
        "Force a fresh run even if <build-dir>/coverage/coverage.json already exists."
    ),
)
def coverage_report(
    build_type: str,
    build_dir: str | None,
    regenerate: bool,
) -> None:
    """Generate full coverage report from existing .gcda data — no build, no run.

    Runs gcovr and writes <build-dir>/coverage/coverage.json (+HTML).
    """
    if build_type.lower() == "coverage":
        build_type = "Debug"
    try:
        xahaud_root = get_xahaud_root()
    except Exception as e:
        raise click.ClickException(f"Could not find xahaud root: {e}") from e

    build_dir = _resolve_build_dir(xahaud_root, build_type, build_dir)

    existing_json = Path(build_dir) / "coverage" / "coverage.json"
    if existing_json.exists() and not regenerate:
        logger.info(f"Coverage report already exists: {existing_json}")
        logger.info("(use --regenerate to force a fresh gcovr run)")
        return

    if not _has_gcda(build_dir):
        raise click.ClickException(
            f"No .gcda files under {build_dir}. "
            "Run an instrumented test run first (--coverage)."
        )

    from xahaud_scripts.utils.coverage_diff import do_generate_coverage_report_v2

    logger.info(f"Generating coverage report (gcovr) for {build_dir}")
    do_generate_coverage_report_v2(build_dir=build_dir)


if __name__ == "__main__":
    main()
