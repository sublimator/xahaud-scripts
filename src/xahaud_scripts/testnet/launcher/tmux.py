"""Tmux launcher for xahaud nodes.

This module provides a launcher that creates a tmux session with
panes for each node. Processes can be killed with Ctrl+C and
restarted manually since the shell stays alive.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import LaunchConfig, NodeInfo

logger = make_logger(__name__)

TMUX_SESSION_NAME = "xahaud-testnet"


class TmuxLauncher:
    """Launch xahaud nodes in tmux panes.

    Creates a single tmux session with a pane for each node.
    Uses send-keys so Ctrl+C kills the process but keeps the shell.
    """

    def __init__(self) -> None:
        self._session_created = False
        self._pane_count = 0

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

        # Extra environment variables from CLI
        for key, value in config.extra_env.items():
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
        if not self._session_created:
            return

        # Open iTerm and attach to the session if on macOS
        if sys.platform == "darwin" and shutil.which("osascript"):
            applescript = f"""
tell application "iTerm"
    create window with default profile
    tell current session of current window
        write text "tmux attach -t {TMUX_SESSION_NAME}"
    end tell
end tell
"""
            subprocess.run(
                ["osascript", "-e", applescript],
                check=True,
                capture_output=True,
            )
            logger.info("Opened iTerm and attached to tmux session")
        else:
            # Just print instructions
            logger.info(
                f"Attach to tmux session with: tmux attach -t {TMUX_SESSION_NAME}"
            )
