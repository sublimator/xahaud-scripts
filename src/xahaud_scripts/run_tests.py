#!/usr/bin/env python3
"""
Script to build and run rippled tests with support for debugging with lldb.
"""

import argparse
import os
import subprocess
import sys
from typing import List, Tuple

from xahaud_scripts.utils.paths import get_xahaud_root


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


def parse_args() -> Tuple[List[str], bool, bool]:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Build and run rippled tests")
    parser.add_argument("--build-jshooks-header", action="store_true", help="Build JS hooks header")
    parser.add_argument("--lldb", action="store_true", help="Run with lldb debugger")

    # Allow pass-through of other arguments to rippled
    args, rippled_args = parser.parse_known_args()

    return rippled_args, args.build_jshooks_header, args.lldb


def build_jshooks_header() -> None:
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
            "-DCMAKE_BUILD_TYPE=Debug",
            "-DCMAKE_VERBOSE_MAKEFILE=ON"
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
    run_command([
        "cmake", "--build", ".",
        "--target", "rippled",
        "--parallel", f"-j{cpu_count}"
    ])


def run_rippled(args: List[str], use_lldb: bool) -> None:
    """Run the rippled executable, optionally with lldb."""
    test_args = ["-u"] + args

    if use_lldb:
        cmd = ["lldb", "--", "./rippled"] + test_args
    else:
        cmd = ["./rippled"] + test_args

    # Don't use check=True here to allow lldb to exit naturally
    process = run_command(cmd, check=False)

    # Return the exit code from the process
    sys.exit(process.returncode)

def main() -> None:
    """Main function."""
    # Parse arguments
    os.chdir(get_xahaud_root())

    rippled_args, build_header, use_lldb = parse_args()

    # Set strict error handling
    # (similar to set -e in bash)
    try:
        # Build JS hooks header if needed
        if build_header:
            build_jshooks_header()

        # Build rippled
        build_rippled()

        # Run rippled with the appropriate arguments
        run_rippled(rippled_args, use_lldb)

    except subprocess.CalledProcessError as e:
        print(f"Command failed with exit code {e.returncode}")
        sys.exit(e.returncode)


if __name__ == "__main__":
    main()
