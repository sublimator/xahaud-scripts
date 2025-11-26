import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager

from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)


def check_tool_exists(tool_name: str) -> bool:
    """Check if a command-line tool exists."""
    exists = shutil.which(tool_name) is not None
    if exists:
        logger.debug(f"Tool '{tool_name}' is available")
    else:
        logger.warning(f"Tool '{tool_name}' not found in PATH")
    return exists


def get_llvm_tool_command(tool_name: str) -> list[str]:
    """Get the command to run an LLVM tool, using xcrun on macOS if available."""
    if sys.platform == "darwin" and check_tool_exists("xcrun"):
        logger.debug(f"Using xcrun to invoke {tool_name} on macOS")
        return ["xcrun", tool_name]
    else:
        return [tool_name]


def run_command(
    cmd: list[str],
    check: bool = True,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
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
            result = subprocess.run(cmd, check=check, env=env, text=True)

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
    except FileNotFoundError:
        logger.error(f"Command not found: {cmd[0]}")
        raise


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
