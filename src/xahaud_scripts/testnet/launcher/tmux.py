"""Tmux launcher for xahaud nodes.

This module provides a launcher that creates a tmux session with
panes for each node. Processes can be killed with Ctrl+C and
restarted manually since the shell stays alive.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import LaunchConfig, NodeInfo
    from xahaud_scripts.testnet.protocols import ProcessManager

logger = make_logger(__name__)

TMUX_SESSION_NAME = "xahaud-testnet"
ITERM_WINDOW_FILE = ".tmux_iterm_window"

# macOS key codes for numbers 1-9 (used for Ctrl+N desktop switching)
DESKTOP_KEY_CODES = {
    1: 18,
    2: 19,
    3: 20,
    4: 21,
    5: 23,
    6: 22,
    7: 26,
    8: 28,
    9: 25,
}


def switch_to_desktop(desktop: int) -> bool:
    """Switch to a specific macOS desktop using Ctrl+number.

    Args:
        desktop: Desktop number (1-9)

    Returns:
        True if switch succeeded, False otherwise
    """
    if desktop not in DESKTOP_KEY_CODES:
        logger.warning(f"Invalid desktop number: {desktop}")
        return False

    key_code = DESKTOP_KEY_CODES[desktop]
    applescript = f"""
tell application "System Events"
    key code {key_code} using control down
end tell
delay 0.5
"""
    try:
        subprocess.run(
            ["osascript", "-e", applescript],
            check=True,
            capture_output=True,
        )
        logger.debug(f"Switched to desktop {desktop}")
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to switch to desktop {desktop}: {e}")
        return False


class TmuxLauncher:
    """Launch xahaud nodes in tmux panes.

    Creates a single tmux session with a pane for each node.
    Uses send-keys so Ctrl+C kills the process but keeps the shell.
    """

    def __init__(self) -> None:
        self._session_created = False
        self._pane_count = 0
        self._base_dir: Path | None = None
        self._desktop: int | None = None

    def is_available(self) -> bool:
        """Check if tmux is available on this system."""
        return shutil.which("tmux") is not None

    def launch(self, node: NodeInfo, config: LaunchConfig) -> bool:
        """Launch a node in a tmux pane.

        Args:
            node: Node configuration and identity information
            config: Launch configuration including paths and flags

        Returns:
            True if launch succeeded, False otherwise
        """
        try:
            if not self._session_created:
                self._create_session(node, config)
            else:
                self._create_pane(node, config)

            self._pane_count += 1
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to launch node {node.id}: {e}")
            if e.stderr:
                logger.error(f"  Error: {e.stderr.decode()}")
            return False

    def _create_session(self, node: NodeInfo, config: LaunchConfig) -> None:
        """Create the tmux session with the first node."""
        # Track base_dir and desktop for finalize()
        self._base_dir = node.node_dir.parent
        self._desktop = config.desktop

        # Kill any existing session
        subprocess.run(
            ["tmux", "kill-session", "-t", TMUX_SESSION_NAME],
            capture_output=True,
        )

        role = "[EXPLOIT]" if node.is_injector else "[CLEAN]"
        window_name = f"n{node.id}{role}"

        # Create new detached session
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                TMUX_SESSION_NAME,
                "-n",
                window_name,
                "-c",
                str(node.node_dir),
            ],
            check=True,
            capture_output=True,
        )

        # Send the startup command
        cmd = self._build_full_command(node, config)
        subprocess.run(
            ["tmux", "send-keys", "-t", TMUX_SESSION_NAME, cmd, "Enter"],
            check=True,
            capture_output=True,
        )

        self._session_created = True
        logger.info(f"Created tmux session '{TMUX_SESSION_NAME}' with node {node.id}")

    def _create_pane(self, node: NodeInfo, config: LaunchConfig) -> None:
        """Create a new pane for a node."""
        role = "[EXPLOIT]" if node.is_injector else "[CLEAN]"

        # Split the window and create new pane
        subprocess.run(
            [
                "tmux",
                "split-window",
                "-t",
                TMUX_SESSION_NAME,
                "-c",
                str(node.node_dir),
            ],
            check=True,
            capture_output=True,
        )

        # Rebalance panes to tiled layout
        subprocess.run(
            ["tmux", "select-layout", "-t", TMUX_SESSION_NAME, "tiled"],
            check=True,
            capture_output=True,
        )

        # Send the startup command to the new pane
        cmd = self._build_full_command(node, config)
        subprocess.run(
            ["tmux", "send-keys", "-t", TMUX_SESSION_NAME, cmd, "Enter"],
            check=True,
            capture_output=True,
        )

        logger.info(f"Created pane for node {node.id} {role}")

    def _build_full_command(self, node: NodeInfo, config: LaunchConfig) -> str:
        """Build the full command with env vars and startup flags."""
        env_vars = self._build_env_vars(node, config)
        startup_flags = self._build_startup_flags(node, config)
        cmd = f"{config.rippled_path} --conf {node.config_path} {startup_flags}"

        # Combine env vars and command
        return f"{env_vars} && {cmd}"

    def _build_env_vars(self, node: NodeInfo, config: LaunchConfig) -> str:
        """Build environment variable exports for the node."""
        parts = []

        # Log date format for easier identification
        parts.append(f"export LOG_DATE_FORMAT='N{node.id} %T %Z'")
        parts.append("export LOG_DATE_LOCAL=1")
        parts.append("export NO_COLOR=1")

        # Amendment ID for injection
        amendment_id = (
            config.amendment_id
            or "56B241D7A43D40354D02A9DC4C8DF5C7A1F930D92A9035C4E12291B3CA3E1C2B"
        )
        parts.append(f"export AMENDMENT_ID={amendment_id}")

        # Injection type
        parts.append(f"export INJECT_TYPE={config.inject_type}")

        # Optional flood setting
        if config.flood is not None:
            parts.append(f"export FLOOD={config.flood}")

        # Optional n_txns setting
        if config.n_txns is not None:
            parts.append(f"export N_TXNS={config.n_txns}")

        # Disable local pseudo-transaction checking if requested
        if config.no_check_local:
            parts.append("export CHECK_LOCAL_PSEUDO=0")

        # Disable pseudo-transaction validity checking if requested
        if config.no_check_pseudo_valid:
            parts.append("export CHECK_PSEUDO_VALIDITY=0")

        # Extra environment variables from CLI (global)
        for key, value in config.extra_env.items():
            parts.append(f"export {key}={value}")

        # Node-specific environment variables (override global)
        if node.id in config.node_env:
            for key, value in config.node_env[node.id].items():
                parts.append(f"export {key}={value}")

        return " && ".join(parts)

    def _build_startup_flags(self, node: NodeInfo, config: LaunchConfig) -> str:
        """Build startup flags for the rippled command."""
        parts = []

        # Genesis ledger file
        parts.append(f"--ledgerfile {config.genesis_file}")

        # Quorum setting
        if config.quorum is not None:
            parts.append(f"--quorum {config.quorum}")

        # Slave-net mode: add --net flag to non-master nodes
        if config.slave_net and not node.is_injector:
            parts.append("--net")

        # Extra arguments
        if config.extra_args:
            parts.extend(config.extra_args)

        return " ".join(parts)

    def finalize(self) -> None:
        """Attach to the tmux session after all nodes are launched."""
        import os

        if not self._session_created:
            return

        # Check for headless mode (no iTerm window)
        if os.environ.get("TMUX_MODE", "").lower() == "headless":
            logger.info(
                f"Headless mode: tmux session '{TMUX_SESSION_NAME}' running in background"
            )
            logger.info(f"  Attach with: tmux attach -t {TMUX_SESSION_NAME}")
            return

        # Switch to target desktop if specified
        if self._desktop is not None:
            switch_to_desktop(self._desktop)

        # Open iTerm and attach to the session if on macOS
        if sys.platform == "darwin" and shutil.which("osascript"):
            applescript = f"""
tell application "iTerm"
    activate
    set newWindow to (create window with default profile)
    set windowId to id of newWindow
    tell current session of newWindow
        delay 0.3
        write text "tmux attach -t {TMUX_SESSION_NAME}"
    end tell
    return windowId
end tell
"""
            result = subprocess.run(
                ["osascript", "-e", applescript],
                check=True,
                capture_output=True,
                text=True,
            )
            window_id = result.stdout.strip()
            logger.info(
                f"Opened iTerm window (id={window_id}) attached to tmux session"
            )

            # Save window ID for shutdown
            if window_id and self._base_dir:
                window_file = self._base_dir / ITERM_WINDOW_FILE
                window_file.write_text(window_id)
                logger.debug(f"Saved iTerm window ID to {window_file}")
        else:
            # Just print instructions
            logger.info(
                f"Attach to tmux session with: tmux attach -t {TMUX_SESSION_NAME}"
            )

    def shutdown(self, base_dir: Path, process_manager: ProcessManager) -> int:
        """Shutdown the tmux session, killing all processes.

        Args:
            base_dir: Base directory containing network.json
            process_manager: Process manager (unused - tmux handles killing)

        Returns:
            Number of processes killed (estimated from network.json)
        """
        # Count nodes for return value
        killed = 0
        network_file = base_dir / "network.json"
        if network_file.exists():
            import json

            with open(network_file) as f:
                info = json.load(f)
            killed = len(info.get("nodes", []))

        # Kill the entire tmux session - this terminates all panes and processes
        result = subprocess.run(
            ["tmux", "kill-session", "-t", TMUX_SESSION_NAME],
            capture_output=True,
        )

        if result.returncode == 0:
            logger.info(f"Killed tmux session '{TMUX_SESSION_NAME}'")
        else:
            # Session might not exist (already killed or never created)
            logger.debug(
                f"tmux session '{TMUX_SESSION_NAME}' not found or already killed"
            )
            killed = 0

        # Close the iTerm window if one was created
        self._close_iterm_window(base_dir)

        return killed

    def _close_iterm_window(self, base_dir: Path) -> bool:
        """Close the iTerm window that was created for this tmux session.

        Args:
            base_dir: Base directory containing the .tmux_iterm_window file

        Returns:
            True if window was closed, False if not found or failed
        """
        window_file = base_dir / ITERM_WINDOW_FILE
        if not window_file.exists():
            return False

        window_id = window_file.read_text().strip()
        if not window_id:
            window_file.unlink()
            return False

        # Close the specific window by ID
        applescript = f"""
tell application "iTerm"
    repeat with w in windows
        if id of w is {window_id} then
            close w
            return true
        end if
    end repeat
    return false
end tell
"""
        try:
            result = subprocess.run(
                ["osascript", "-e", applescript],
                check=True,
                capture_output=True,
                text=True,
            )
            closed = result.stdout.strip() == "true"

            if closed:
                logger.info(f"Closed iTerm window (id={window_id})")
            else:
                logger.debug(f"iTerm window (id={window_id}) not found")

            # Clean up the file
            window_file.unlink()
            return closed

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to close iTerm window: {e}")
            return False
