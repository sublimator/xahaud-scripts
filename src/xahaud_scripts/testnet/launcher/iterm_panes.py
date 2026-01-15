"""iTerm2 pane-based launcher for xahaud nodes on macOS.

This module provides a launcher that opens all nodes in a single
iTerm2 window with split panes. Closing the window kills all nodes.
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

logger = make_logger(__name__)

# File to store the iTerm window ID for teardown
ITERM_WINDOW_FILE = ".iterm_window"


class ITermPanesLauncher:
    """Launch xahaud nodes in iTerm2 panes within a single window.

    Uses AppleScript to control iTerm2, creating one window and splitting
    it into panes for each node. Closing the window kills all nodes.

    The window ID is saved to {base_dir}/.iterm_window for precise teardown.
    """

    def __init__(self) -> None:
        self._window_created = False
        self._pane_count = 0
        self._base_dir: Path | None = None
        self._window_id: str | None = None

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
        # Derive base_dir from node directory (node_dir is {base_dir}/n0, etc.)
        self._base_dir = node.node_dir.parent

        # Create window and return its ID for later teardown
        applescript = f'''
tell application "iTerm"
    activate
    set newWindow to (create window with default profile)
    set windowId to id of newWindow
    tell current session of newWindow
        set name to "{title}"
        write text "cd {node.node_dir}"
        write text "{cmd}"
    end tell
    return windowId
end tell
'''
        result = subprocess.run(
            ["osascript", "-e", applescript],
            check=True,
            capture_output=True,
            text=True,
        )
        self._window_id = result.stdout.strip()
        self._window_created = True

        # Save window ID to file for teardown
        if self._window_id:
            window_file = self._base_dir / ITERM_WINDOW_FILE
            window_file.write_text(self._window_id)
            logger.debug(f"Saved iTerm window ID {self._window_id} to {window_file}")

        logger.info(f"Created iTerm window (id={self._window_id}) with node {node.id}")

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

    @staticmethod
    def close_window(base_dir: Path) -> bool:
        """Close the iTerm window that was created for this testnet.

        Args:
            base_dir: Base directory containing the .iterm_window file

        Returns:
            True if window was closed, False if not found or failed
        """
        window_file = base_dir / ITERM_WINDOW_FILE
        if not window_file.exists():
            logger.debug(f"No iTerm window file found at {window_file}")
            return False

        window_id = window_file.read_text().strip()
        if not window_id:
            logger.warning(f"Empty window ID in {window_file}")
            window_file.unlink()
            return False

        # Close the specific window by ID and confirm the dialog
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
        # Separate script to confirm the "Close Window" dialog by pressing Return
        # (OK is the default button, highlighted in blue)
        confirm_script = """
delay 0.5
tell application "iTerm"
    activate
end tell
delay 0.1
tell application "System Events"
    key code 36
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
                # Try to click OK on the confirmation dialog
                subprocess.run(
                    ["osascript", "-e", confirm_script],
                    capture_output=True,
                )
            else:
                logger.warning(
                    f"iTerm window (id={window_id}) not found - may already be closed"
                )

            # Clean up the file
            window_file.unlink()
            return closed

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to close iTerm window: {e}")
            if e.stderr:
                logger.error(f"  AppleScript error: {e.stderr}")
            return False

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
