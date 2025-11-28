"""ccache integration for xahaud builds."""

import os
import subprocess
import time
from pathlib import Path

from xahaud_scripts.utils.logging import make_logger
from xahaud_scripts.utils.shell_utils import check_tool_exists

logger = make_logger(__name__)

# ccache config path for xahaud builds
CCACHE_CONFIG_DIR = Path.home() / ".config" / "xahaud-scripts"
CCACHE_CONFIG_PATH = CCACHE_CONFIG_DIR / "ccache.conf"

AUTO_MAINTAIN_MARKER = "# @auto-maintain"

CCACHE_CONFIG_BODY = """\
# ccache configuration for xahaud builds
#
# To opt into automatic updates, add this line at the top of the file:
#   # @auto-maintain
# When present, x-run-tests will overwrite this file with new defaults.

# Use content hashing instead of mtime (more reliable across git operations)
compiler_check = content

# Don't include cwd in hash - use base_dir for path normalization instead
# This allows cache sharing between worktrees when using --ccache-basedir=.
hash_dir = false

# Max cache size
max_size = 10G

# Compression (saves space, slight CPU overhead)
compression = true
compression_level = 1

# Direct mode - hash source files directly, skip preprocessor (faster)
direct_mode = true
"""


def get_ccache_env(
    base_dir: str | None = None,
    sloppy: bool = False,
    debug_logfile: str | None = None,
) -> dict:
    """Get environment variables for ccache with custom config.

    Args:
        base_dir: Base directory for path normalization (enables cache sharing between worktrees)
        sloppy: If True, ignore locale and __DATE__, __TIME__ differences
        debug_logfile: If provided, enable debug logging to this file

    Returns:
        Environment dict with ccache settings
    """
    env = os.environ.copy()
    env["CCACHE_CONFIGPATH"] = str(CCACHE_CONFIG_PATH)

    if base_dir:
        # Resolve to absolute path
        abs_base_dir = os.path.abspath(base_dir)
        env["CCACHE_BASEDIR"] = abs_base_dir

    if sloppy:
        # locale: ignore LANG/LC_* env vars in hash
        # time_macros: ignore __DATE__, __TIME__, __TIMESTAMP__
        env["CCACHE_SLOPPINESS"] = "locale,time_macros"

    if debug_logfile:
        env["CCACHE_DEBUG"] = "1"
        env["CCACHE_LOGFILE"] = debug_logfile

    return env


def run_ccache(
    args: list[str], capture: bool = False
) -> subprocess.CompletedProcess | None:
    """Run ccache command with our custom config.

    Args:
        args: Arguments to pass to ccache (e.g., ["-s"] for stats, ["-z"] for zero)
        capture: If True, capture and return output

    Returns:
        CompletedProcess if capture=True, None otherwise
    """
    env = os.environ.copy()
    env["CCACHE_CONFIGPATH"] = str(CCACHE_CONFIG_PATH)

    cmd = ["ccache"] + args

    try:
        if capture:
            return subprocess.run(
                cmd, env=env, check=True, capture_output=True, text=True
            )
        else:
            subprocess.run(cmd, env=env, check=True)
            return None
    except Exception as e:
        logger.warning(f"ccache command failed: {e}")
        return None


def setup_ccache_config(dry_run: bool = False) -> Path:
    """Ensure ccache config exists and return its path.

    Creates ~/.config/xahaud-scripts/ccache.conf with sensible defaults
    if it doesn't exist. If the file exists and contains the @auto-maintain
    marker, it will be overwritten with the latest defaults (preserving the marker).

    Returns:
        Path to the ccache config file
    """
    if not CCACHE_CONFIG_PATH.exists():
        # New file - create without marker (user can opt in later)
        content = CCACHE_CONFIG_BODY
        if dry_run:
            print(f"\n[DRY RUN] Creating {CCACHE_CONFIG_PATH} with content:")
            print(content)
        else:
            CCACHE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CCACHE_CONFIG_PATH.write_text(content)
            logger.info(f"Created ccache config at {CCACHE_CONFIG_PATH}")
    elif CCACHE_CONFIG_PATH.read_text().startswith(AUTO_MAINTAIN_MARKER):
        # User opted in (marker at start of file) - regenerate with marker preserved
        content = AUTO_MAINTAIN_MARKER + "\n" + CCACHE_CONFIG_BODY
        if dry_run:
            print(f"\n[DRY RUN] Updating {CCACHE_CONFIG_PATH} with content:")
            print(content)
        else:
            CCACHE_CONFIG_PATH.write_text(content)
            logger.info(f"Updated ccache config at {CCACHE_CONFIG_PATH}")
    else:
        logger.debug(f"Using existing ccache config at {CCACHE_CONFIG_PATH}")

    return CCACHE_CONFIG_PATH


def get_ccache_debug_logfile() -> str:
    """Generate a timestamped ccache debug log path."""
    timestamp = int(time.time() * 1000)
    return str(CCACHE_CONFIG_DIR / f"ccache-{timestamp}.log")


def ccache_zero_stats() -> None:
    """Zero ccache statistics."""
    run_ccache(["-z"])
    logger.info("Zeroed ccache statistics")


def ccache_show_stats() -> None:
    """Show ccache statistics summary."""
    result = run_ccache(["-s"], capture=True)
    if result and result.stdout:
        print("\n" + "=" * 50)
        print("CCACHE STATISTICS")
        print("=" * 50)
        for line in result.stdout.strip().split("\n"):
            # Highlight hit/miss lines
            if "hit" in line.lower() or "miss" in line.lower():
                print(f"  ** {line}")
            elif line.strip():
                print(f"  {line}")
        print("=" * 50)

        # Warn about CCACHE_* env vars that might interfere
        ccache_env_vars = {
            k: v for k, v in os.environ.items() if k.startswith("CCACHE_")
        }
        if ccache_env_vars:
            print("\n  WARNING: CCACHE_* env vars detected (may interfere):")
            for k, v in ccache_env_vars.items():
                print(f"    {k}={v}")
        print()


def get_ccache_launcher(
    basedir: str | None = None,
    sloppy: bool = False,
    debug_logfile: str | None = None,
) -> str:
    """Get the ccache launcher command with env vars baked in.

    Returns a CMake-compatible launcher string that uses `env` to set
    ccache environment variables inline, ensuring they're used during
    compilation regardless of how the build is invoked.
    """
    parts = ["env"]
    parts.append(f"CCACHE_CONFIGPATH={CCACHE_CONFIG_PATH}")

    if basedir:
        parts.append(f"CCACHE_BASEDIR={basedir}")

    if sloppy:
        parts.append("CCACHE_SLOPPINESS=locale,time_macros")

    if debug_logfile:
        parts.append("CCACHE_DEBUG=1")
        parts.append(f"CCACHE_LOGFILE={debug_logfile}")

    parts.append("ccache")
    return " ".join(parts)


def is_ccache_available() -> bool:
    """Check if ccache is available."""
    return check_tool_exists("ccache")
