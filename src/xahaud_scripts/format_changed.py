#!/usr/bin/env python3

import argparse
import os
import subprocess
import sys
from pathlib import Path

from xahaud_scripts.utils.logging import make_logger, setup_logging
from xahaud_scripts.utils.paths import get_xahaud_root

logger = make_logger(__name__)

SEARCH_DIRS = ["Builds/CMake", "src", "."]


def get_all_files_by_type(root_dir: Path) -> dict[str, list[Path]]:
    """Get all files by type under configured search directories."""
    files: dict[str, list[Path]] = {"cpp": [], "shell": [], "python": [], "cmake": []}

    # File patterns by type
    patterns = {
        "cpp": [".h", ".cpp"],
        "shell": [".sh"],
        "python": [".py"],
        "cmake": ["CMakeLists.txt", ".cmake"],
    }

    for subdir in SEARCH_DIRS:
        dir_path = root_dir / subdir
        if not dir_path.exists():
            continue

        # Find files of each type
        for file_type, extensions in patterns.items():
            for ext in extensions:
                if subdir == ".":
                    # Special case for root directory - don't recurse
                    files[file_type].extend(dir_path.glob(f"*{ext}"))
                else:
                    files[file_type].extend(dir_path.rglob(f"*{ext}"))

    return files


def get_git_dirty_files(
    root_dir: Path, since_base: str | None = None
) -> dict[str, list[Path]]:
    """Get all dirty files (staged, unstaged, and untracked) in git by file type.

    Args:
        root_dir: The root directory of the git repository
        since_base: If provided, get files changed since this base ref (e.g., origin/dev)
    """
    os.chdir(root_dir)

    files: set[str] = set()

    if since_base:
        # Get files changed since the base ref
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{since_base}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout.strip():
            files.update(result.stdout.strip().split("\n"))
        logger.debug(f"Found {len(files)} files changed since {since_base}")
    else:
        # Get staged files
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout.strip():
            files.update(result.stdout.strip().split("\n"))

        # Get unstaged files
        result = subprocess.run(
            ["git", "diff", "--name-only"], capture_output=True, text=True, check=True
        )
        if result.stdout.strip():
            files.update(result.stdout.strip().split("\n"))

        # Get untracked files
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout.strip():
            files.update(result.stdout.strip().split("\n"))

    # Filter files by type
    filtered_files: dict[str, list[Path]] = {
        "cpp": [],
        "shell": [],
        "python": [],
        "cmake": [],
    }

    search_dirs = []
    for d in SEARCH_DIRS:
        if d == ".":
            search_dirs.append("")  # Root files have no prefix
        else:
            search_dirs.append(f"{d}/")

    for filename in files:
        file_path = root_dir / filename
        if not file_path.exists():
            continue

        # Check if file is in one of our target directories
        is_in_target_dir = False
        for search_dir in search_dirs:
            if search_dir == "":  # Root directory case
                # File is in root if it doesn't contain any path separators
                if "/" not in filename:
                    is_in_target_dir = True
                    break
            else:
                if filename.startswith(search_dir):
                    is_in_target_dir = True
                    break

        if not is_in_target_dir:
            continue

        # Categorize by file type
        if filename.endswith(".h") or filename.endswith(".cpp"):
            filtered_files["cpp"].append(file_path)
        elif filename.endswith(".sh"):
            filtered_files["shell"].append(file_path)
        elif filename.endswith(".py"):
            filtered_files["python"].append(file_path)
        elif filename.endswith("CMakeLists.txt") or filename.endswith(".cmake"):
            filtered_files["cmake"].append(file_path)

    return filtered_files


def format_cpp_file(file_path: Path, root_dir: Path) -> bool:
    """Format a C++ file using clang-format."""
    logger.info(f"Formatting C++ file: {file_path.relative_to(root_dir)}")
    try:
        subprocess.run(["clang-format", "-i", str(file_path)], check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Error formatting {file_path}: {e}")
        return False
    except FileNotFoundError:
        logger.error("clang-format not found. Please install it first.")
        sys.exit(1)


def format_shell_file(file_path: Path, root_dir: Path) -> bool:
    """Format a shell file using shfmt."""
    logger.info(f"Formatting shell file: {file_path.relative_to(root_dir)}")
    try:
        # Use shfmt with sensible defaults
        # -i 2: indent with 2 spaces
        # -w: write to file instead of stdout
        subprocess.run(["shfmt", "-i", "2", "-w", str(file_path)], check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Error formatting {file_path}: {e}")
        return False
    except FileNotFoundError:
        logger.error("shfmt not found. Please install it first.")
        logger.error(
            "Install with: brew install shfmt (macOS) or apt-get install shfmt (Linux)"
        )
        sys.exit(1)


def format_python_file(file_path: Path, root_dir: Path) -> bool:
    """Format a Python file using black."""
    logger.info(f"Formatting Python file: {file_path.relative_to(root_dir)}")

    # Use black from the same environment as the current Python executable
    black_path = Path(sys.executable).parent / "black"

    # Check if black exists next to the Python executable
    if not black_path.exists():
        logger.error(f"black not found at {black_path}")
        logger.error("Make sure black is installed in the current Python environment")
        sys.exit(1)

    try:
        # Use black with default settings
        subprocess.run([str(black_path), str(file_path)], check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Error formatting {file_path}: {e}")
        return False


def format_cmake_file(file_path: Path, root_dir: Path) -> bool:
    """Format a CMake file using cmake-format from the virtual environment."""
    logger.info(f"Formatting CMake file: {file_path.relative_to(root_dir)}")

    # Use cmake-format from the same environment as the current Python executable
    cmake_format_path = Path(sys.executable).parent / "cmake-format"

    # Check if cmake-format exists next to the Python executable
    if not cmake_format_path.exists():
        logger.error(f"cmake-format not found at {cmake_format_path}")
        logger.error(
            "Make sure cmake-format is installed in the current Python environment"
        )
        sys.exit(1)

    try:
        # Use cmake-format with default settings
        subprocess.run([str(cmake_format_path), "-i", str(file_path)], check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Error formatting {file_path}: {e}")
        return False


def main() -> None:
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(description="Format C++, shell, and Python files")
    parser.add_argument("--all", action="store_true", help="Format all files")
    parser.add_argument(
        "--since",
        metavar="REF",
        help="Format files changed since ref (e.g., origin/dev)",
    )
    parser.add_argument("--cpp-only", action="store_true", help="Format only C++ files")
    parser.add_argument(
        "--shell-only", action="store_true", help="Format only shell files"
    )
    parser.add_argument(
        "--python-only", action="store_true", help="Format only Python files"
    )
    parser.add_argument(
        "--cmake-only", action="store_true", help="Format only CMake files"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Set the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    parser.add_argument(
        "--stage",
        action="store_true",
        help="Stage all formatted files with git add",
    )
    args = parser.parse_args()

    setup_logging(args.log_level, logger)

    root_dir: Path = Path(get_xahaud_root()).absolute()

    # Determine which file types to format
    format_only_one_type = (
        args.cpp_only or args.shell_only or args.python_only or args.cmake_only
    )
    format_cpp = args.cpp_only or not format_only_one_type
    format_shell = args.shell_only or not format_only_one_type
    format_python = args.python_only or not format_only_one_type
    format_cmake = args.cmake_only or not format_only_one_type

    if args.all:
        # Explicit request for all files
        files_by_type = get_all_files_by_type(root_dir)
        logger.info("Formatting mode: all files")
    else:
        # Format dirty files or files changed since base
        files_by_type = get_git_dirty_files(root_dir, since_base=args.since)
        cpp_count = len(files_by_type["cpp"]) if format_cpp else 0
        shell_count = len(files_by_type["shell"]) if format_shell else 0
        python_count = len(files_by_type["python"]) if format_python else 0
        cmake_count = len(files_by_type["cmake"]) if format_cmake else 0
        total_count = cpp_count + shell_count + python_count + cmake_count

        if total_count > 0:
            if args.since:
                logger.info(
                    f"Formatting mode: files changed since {args.since} ({total_count} files found)"
                )
            else:
                logger.info(
                    f"Formatting mode: dirty files only ({total_count} files found)"
                )
        else:
            if args.since:
                logger.info(f"No files changed since {args.since}")
            else:
                logger.info("No dirty files to format")
            return

    # Set up formatters by file type
    # All formatters now need the root_dir for relative path logging
    def cpp_formatter(path: Path) -> bool:
        return format_cpp_file(path, root_dir)

    def shell_formatter(path: Path) -> bool:
        return format_shell_file(path, root_dir)

    def python_formatter(path: Path) -> bool:
        return format_python_file(path, root_dir)

    def cmake_formatter(path: Path) -> bool:
        return format_cmake_file(path, root_dir)

    formatters = {
        "cpp": cpp_formatter,
        "shell": shell_formatter,
        "python": python_formatter,
        "cmake": cmake_formatter,
    }

    # Track overall success
    success: bool = True
    files_formatted = 0
    formatted_paths: list[Path] = []  # Track paths for --stage

    # Format all requested file types
    for file_type, formatter in formatters.items():
        # Skip if this file type is not requested
        if (
            (file_type == "cpp" and not format_cpp)
            or (file_type == "shell" and not format_shell)
            or (file_type == "python" and not format_python)
            or (file_type == "cmake" and not format_cmake)
        ):
            continue

        for file_path in files_by_type[file_type]:
            if not formatter(file_path):
                success = False
            else:
                files_formatted += 1
                formatted_paths.append(file_path)

    if files_formatted > 0:
        logger.info(f"Successfully formatted {files_formatted} files")
    else:
        logger.info("No files were formatted")

    # Stage formatted files if requested
    if args.stage and formatted_paths:
        try:
            subprocess.run(
                ["git", "add", "--"] + [str(p) for p in formatted_paths],
                cwd=root_dir,
                check=True,
            )
            logger.info(f"Staged {len(formatted_paths)} formatted files")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to stage files: {e}")
            success = False

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
