import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

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


def get_mise_tool_cmd(tool: str) -> list[str]:
    """Get the command prefix to run a tool via mise.

    Uses ``mise exec <tool> -- <tool>`` so the configured version is always
    used, even when the shell hasn't been activated.  Falls back to bare
    ``<tool>`` when mise is not installed.
    """
    if shutil.which("mise"):
        return ["mise", "exec", tool, "--", tool]
    return [tool]


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
    tee_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and return the result.

    When tee_file is set (and capture_output is False), stdout+stderr are
    streamed to the terminal and appended to tee_file simultaneously.
    """
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
            logger.info(f"Command completed with return code: {result.returncode}")
            return result

        if tee_file is not None:
            tee_file.parent.mkdir(parents=True, exist_ok=True)
            with open(tee_file, "a") as tf:
                with subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                ) as proc:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                        tf.write(line)
                        tf.flush()
                    proc.wait()
                rc = proc.returncode
            logger.info(f"Command completed with return code: {rc}")
            if check and rc != 0:
                raise subprocess.CalledProcessError(rc, cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=rc)

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
