"""iTerm2 pane-based launcher for xahaud nodes on macOS.

This module provides a launcher that opens all nodes in a single
iTerm2 window with split panes. Closing the window kills all nodes.
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


class ITermPanesLauncher:
    """Launch xahaud nodes in iTerm2 panes within a single window.

    Uses AppleScript to control iTerm2, creating one window and splitting
    it into panes for each node. Closing the window kills all nodes.
    """

    def __init__(self) -> None:
        self._window_created = False
        self._pane_count = 0

    def is_available(self) -> bool:
        """Check if iTerm launcher is available on this system."""
        return sys.platform == "darwin" and shutil.which("osascript") is not None

    def launch(self, node: NodeInfo, config: LaunchConfig) -> bool:
        """Launch a node in an iTerm pane.

        Args:
            node: Node configuration and identity information
            config: Launch configuration including paths and flags

        Returns:
            True if launch succeeded, False otherwise
        """
        env_vars = self._build_env_vars(node, config)
        startup_flags = self._build_startup_flags(node, config)

        role = "[EXPLOIT]" if node.is_injector else "[CLEAN]"
        pane_title = f"N{node.id} {role}"

        # Build the command to run
        cmd = f"{config.rippled_path} --conf {node.config_path} {startup_flags}"
        full_cmd = f"{env_vars} && {cmd}"

        try:
            if not self._window_created:
                self._create_window(node, pane_title, full_cmd)
            else:
                self._create_pane(pane_title, full_cmd)

            self._pane_count += 1
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to launch node {node.id}: {e}")
            if e.stderr:
                logger.error(f"  AppleScript error: {e.stderr.decode()}")
            return False

    def _create_window(self, node: NodeInfo, title: str, cmd: str) -> None:
        """Create the iTerm window with the first node."""
        applescript = f'''
tell application "iTerm"
    activate
    set newWindow to (create window with default profile)
    tell current session of newWindow
        set name to "{title}"
        write text "cd {node.node_dir}"
        write text "{cmd}"
    end tell
end tell
'''
        subprocess.run(
            ["osascript", "-e", applescript],
            check=True,
            capture_output=True,
        )
        self._window_created = True
        logger.info(f"Created iTerm window with node {node.id}")

    def _create_pane(self, title: str, cmd: str) -> None:
        """Split and create a new pane for a node."""
        # Alternate between vertical and horizontal splits for a grid layout
        split_direction = "vertically" if self._pane_count % 2 == 0 else "horizontally"

        applescript = f'''
tell application "iTerm"
    tell current session of current window
        set newSession to (split {split_direction} with default profile)
        tell newSession
            set name to "{title}"
            write text "{cmd}"
        end tell
    end tell
end tell
'''
        subprocess.run(
            ["osascript", "-e", applescript],
            check=True,
            capture_output=True,
        )
        logger.info(f"Created pane: {title}")

    def finalize(self) -> None:
        """No-op - window is already visible."""
        pass

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
