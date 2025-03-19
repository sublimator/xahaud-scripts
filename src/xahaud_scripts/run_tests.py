#!/usr/bin/env python3
"""
Script to build and run rippled tests with support for debugging with lldb.
"""

import click
import os
import subprocess
import sys
from typing import List

from xahaud_scripts.utils.paths import get_xahaud_root

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


def run_command(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    print(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)


def get_logical_cpu_count() -> int:
    """Get the number of logical CPUs."""
    if sys.platform == "darwin":
        return int(subprocess.check_output(["sysctl", "-n", "hw.logicalcpu"]).strip())
    elif sys.platform == "linux":
        return os.cpu_count() or 4  # Default to 4 if we can't determine
    else:
        return os.cpu_count() or 4  # Default to 4 if we can't determine


def do_build_jshooks_header() -> None:
    """Build the JS hooks header."""
    print("Building JS hooks header...")
    run_command(["python", "./.scripts/src/xahaud_scripts/build_jshooks_header.py"])


def build_rippled() -> None:
    """Build the rippled executable."""
    # Create build directory if it doesn't exist
    if not os.path.exists("build"):
        os.makedirs("build", exist_ok=True)
        os.chdir("build")
        # Get environment variables
        llvm_dir = os.environ.get("LLVM_DIR", "")
        llvm_library_dir = os.environ.get("LLVM_LIBRARY_DIR", "")

        # Run cmake configuration
        cmake_cmd = [
            "cmake",
            "-Dsan",
            "-DCMAKE_BUILD_TYPE=Debug",
            "-DCMAKE_VERBOSE_MAKEFILE=ON",
        ]

        if llvm_dir:
            cmake_cmd.append(f"-DLLVM_DIR={llvm_dir}")

        if llvm_library_dir:
            cmake_cmd.append(f"-DLLVM_LIBRARY_DIR={llvm_library_dir}")

        cmake_cmd.append("..")
        run_command(cmake_cmd)
    else:
        os.chdir("build")

    # Build rippled
    cpu_count = get_logical_cpu_count()
    run_command(
        ["cmake", "--build", ".", "--target", "rippled", "--parallel", f"-j{cpu_count}"]
    )


def run_rippled(
    args: List[str],
    use_lldb: bool,
    times: int = 1,
    stop_on_fail: bool = True,
    lldb_commands_file: str = None,
) -> None:
    """Run the rippled executable, optionally with lldb, multiple times."""
    os.chdir(os.path.join(get_xahaud_root(), "build"))

    test_args = ["-u"] + args
    exit_code = 0

    for i in range(times):
        if times > 1:
            print(f"\nRun {i + 1}/{times}")

        if use_lldb:
            cmd = ["lldb", "--", "./rippled"] + test_args
            if lldb_commands_file:
                cmd = cmd[0:1] + ["-s", lldb_commands_file] + cmd[1:]
        else:
            cmd = ["./rippled"] + test_args

        # Don't use check=True here to allow lldb to exit naturally
        process = run_command(cmd, check=False)
        exit_code = process.returncode

        # If a run fails and we're not at the last iteration
        if exit_code != 0 and i < times - 1:
            print(f"Run {i + 1} failed with exit code {exit_code}")

            if stop_on_fail:
                print(
                    "Stopping due to failure (use --no-stop-on-fail to continue on failures)"
                )
                break
            else:
                print("Continuing to next run...")

    # Return the exit code from the last process
    sys.exit(exit_code)


@click.command(context_settings=dict(ignore_unknown_options=True))
@click.option("--build-jshooks-header", is_flag=True, help="Build JS hooks header")
@click.option("--lldb", is_flag=True, help="Run with lldb debugger")
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
@click.argument(
    "rippled_args", nargs=-1, type=click.UNPROCESSED
)  # TODO: may be too lax here
def main(
    build_jshooks_header,
    lldb,
    lldb_commands_file,
    times,
    stop_on_fail,
    rippled_args,
    build,
):
    """Build and run rippled tests with support for debugging."""
    # Set strict error handling
    try:
        if lldb_commands_file:
            if not os.path.isabs(lldb_commands_file):
                # relative to the current directory before changing to xahaud root
                lldb_commands_file = os.path.join(
                    os.path.abspath(os.getcwd()), lldb_commands_file
                )
            if not os.path.exists(lldb_commands_file):
                raise click.ClickException(f"File not found: {lldb_commands_file}")
            lldb = True  # automatically enable lldb if a commands file is provided

        # Change to xahaud root directory
        os.chdir(get_xahaud_root())

        # Build JS hooks header if needed
        if build_jshooks_header:
            do_build_jshooks_header()

        # Build rippled
        if build:
            build_rippled()

        # Run rippled with the appropriate arguments

        run_rippled(list(rippled_args), lldb, times, stop_on_fail, lldb_commands_file)

    except subprocess.CalledProcessError as e:
        print(f"Command failed with exit code {e.returncode}")
        sys.exit(e.returncode)


if __name__ == "__main__":
    main()
