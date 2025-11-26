"""LLDB debugging utilities."""

import os
import tempfile

from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)

LLDB_SCRIPT = """
# Set breakpoints for common crash conditions
breakpoint set --name malloc_error_break
breakpoint set --name abort
breakpoint set --name __assert_rtn
breakpoint set --name __stack_chk_fail

# Run the program
run

# When stopped, get backtrace for current/crashing thread and quit
thread backtrace
quit 1
"""

LLDB_SCRIPT_ALL_THREADS = """
# Set breakpoints for common crash conditions
breakpoint set --name malloc_error_break
breakpoint set --name abort
breakpoint set --name __assert_rtn
breakpoint set --name __stack_chk_fail

# Run the program
run

# When stopped, get backtrace for all threads and quit
thread backtrace all
quit 1
"""


def create_lldb_script(all_threads: bool = False) -> str:
    """Create a temporary file with LLDB commands.

    Args:
        all_threads: If True, show backtrace for all threads. If False, only current thread.

    Returns:
        Path to the temporary LLDB script file.
    """
    fd, path = tempfile.mkstemp(suffix=".lldb")
    logger.debug(
        f"Creating temporary LLDB script at {path} (all_threads={all_threads})"
    )

    with os.fdopen(fd, "w") as f:
        if all_threads:
            f.write(LLDB_SCRIPT_ALL_THREADS)
        else:
            f.write(LLDB_SCRIPT)

    return path
