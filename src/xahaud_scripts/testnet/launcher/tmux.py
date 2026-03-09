"""Tmux launcher for xahaud nodes.

This module provides a launcher that creates a tmux session with
panes for each node. Processes can be killed with Ctrl+C and
restarted manually since the shell stays alive.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import LaunchConfig, NodeInfo
    from xahaud_scripts.testnet.protocols import ProcessManager

logger = make_logger(__name__)

TMUX_SESSION_NAME = "xahaud-testnet"

# Shell function injected into each pane before launching a node.
# Saves PID and exit status to the node's working directory.
# Compatible with bash and zsh. Process runs in foreground (output
# visible, Ctrl+C works). Leading space avoids zsh history.
_XRUN_FUNC = (
    ' _xrun() { "$@" & local p=$!; echo $p > .pid;'
    " trap 'kill $p 2>/dev/null' INT TERM;"
    " wait $p; echo $? > .exit_status; }"
)
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
        self._pane_ids: dict[int, str] = {}
        self._launch_commands: dict[int, str] = {}

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
            cmd = self._build_full_command(node, config)
            self._launch_commands[node.id] = cmd
            self._desktop = config.desktop

            if not self._session_created:
                pane_id = self._create_session(node, cmd)
            else:
                pane_id = self._create_pane(node, cmd)

            self._pane_ids[node.id] = pane_id
            self._pane_count += 1
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to launch node {node.id}: {e}")
            if e.stderr:
                logger.error(f"  Error: {e.stderr.decode()}")
            return False

    def _create_session(self, node: NodeInfo, cmd: str) -> str:
        """Create the tmux session with the first node.

        Returns:
            The tmux pane ID (e.g. "%0") for the created pane.
        """
        # Track base_dir for finalize()
        self._base_dir = node.node_dir.parent

        # Kill any existing session
        subprocess.run(
            ["tmux", "kill-session", "-t", TMUX_SESSION_NAME],
            capture_output=True,
        )

        window_name = f"n{node.id}"

        # Create new detached session and capture pane ID
        result = subprocess.run(
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
                "-P",
                "-F",
                "#{pane_id}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        pane_id = result.stdout.strip()

        # Inject _xrun helper, then send the startup command
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, _XRUN_FUNC, "Enter"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, cmd, "Enter"],
            check=True,
            capture_output=True,
        )

        self._session_created = True
        logger.info(f"Created tmux session '{TMUX_SESSION_NAME}' with node {node.id}")
        return pane_id

    def _create_pane(self, node: NodeInfo, cmd: str) -> str:
        """Create a new pane for a node.

        Returns:
            The tmux pane ID (e.g. "%3") for the created pane.
        """
        # Split the window, create new pane, and capture pane ID
        result = subprocess.run(
            [
                "tmux",
                "split-window",
                "-t",
                TMUX_SESSION_NAME,
                "-c",
                str(node.node_dir),
                "-P",
                "-F",
                "#{pane_id}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        pane_id = result.stdout.strip()

        # Rebalance panes to tiled layout
        subprocess.run(
            ["tmux", "select-layout", "-t", TMUX_SESSION_NAME, "tiled"],
            check=True,
            capture_output=True,
        )

        # Inject _xrun helper, then send the startup command
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, _XRUN_FUNC, "Enter"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, cmd, "Enter"],
            check=True,
            capture_output=True,
        )

        logger.info(f"Created pane for node {node.id}")
        return pane_id

    def _build_full_command(self, node: NodeInfo, config: LaunchConfig) -> str:
        """Build the full command with env vars and startup flags."""
        env_vars = self._build_env_vars(node, config)
        startup_flags = self._build_startup_flags(node, config)
        binary = str(config.get_rippled_path(node.id))
        args = f"--conf {node.config_path} {startup_flags}"

        if node.id in config.lldb_nodes:
            from xahaud_scripts.utils.lldb import create_lldb_script

            script = create_lldb_script(all_threads=False)
            cmd = f"lldb -s {script} -- {binary} {args}"
            logger.info(f"Node {node.id} running under lldb (script: {script})")
        else:
            cmd = f"{binary} {args}"

        # Leading space prevents zsh history logging (HIST_IGNORE_SPACE)
        # _xrun saves PID to .pid and exit status to .exit_status
        return f" {env_vars} && _xrun {cmd}"

    def _build_env_vars(self, node: NodeInfo, config: LaunchConfig) -> str:
        """Build environment variable exports for the node."""
        parts = []

        # Log date format for easier identification
        parts.append(f"export LOG_DATE_FORMAT='N{node.id} %T %Z'")
        parts.append("export LOG_DATE_LOCAL=1")
        parts.append("export NO_COLOR=1")

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

        # Extra arguments
        if config.extra_args:
            parts.extend(config.extra_args)

        return " ".join(parts)

    @property
    def launch_state(self) -> dict[str, Any]:
        """Get launch state for persistence."""
        return {
            "launcher": "tmux",
            "pane_ids": {str(k): v for k, v in self._pane_ids.items()},
            "launch_commands": {str(k): v for k, v in self._launch_commands.items()},
        }

    def load_launch_state(self, state: dict[str, Any]) -> None:
        """Restore state from persisted launch_state."""
        self._pane_ids = {int(k): v for k, v in state.get("pane_ids", {}).items()}
        self._launch_commands = {
            int(k): v for k, v in state.get("launch_commands", {}).items()
        }

    def is_session_alive(self) -> bool:
        """Check if the tmux session is alive."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", TMUX_SESSION_NAME],
            capture_output=True,
        )
        return result.returncode == 0

    def _list_live_pane_ids(self) -> set[str]:
        """Query tmux for currently existing pane IDs in the session."""
        try:
            result = subprocess.run(
                [
                    "tmux",
                    "list-panes",
                    "-t",
                    TMUX_SESSION_NAME,
                    "-F",
                    "#{pane_id}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return set(result.stdout.strip().splitlines())
        except subprocess.CalledProcessError:
            return set()

    def _validate_pane(self, node_id: int) -> str | None:
        """Get pane ID for node, validating it still exists.

        Returns pane ID if valid, None with error log if stale/missing.
        """
        pane_id = self._pane_ids.get(node_id)
        if not pane_id:
            logger.error(f"No pane ID recorded for node {node_id}")
            return None
        live = self._list_live_pane_ids()
        if pane_id not in live:
            logger.error(
                f"Pane {pane_id} for node {node_id} no longer exists "
                f"(was it manually closed?). Live panes: {live}"
            )
            return None
        return pane_id

    def capture_output(self, node_id: int, lines: int = 1000) -> str | None:
        """Capture terminal output from a node's tmux pane.

        Args:
            node_id: The node ID to capture from
            lines: Number of lines of scrollback to capture

        Returns:
            Captured text, or None if capture failed
        """
        pane_id = self._validate_pane(node_id)
        if not pane_id:
            return None
        try:
            result = subprocess.run(
                [
                    "tmux",
                    "capture-pane",
                    "-t",
                    pane_id,
                    "-p",  # output to stdout
                    "-S",
                    f"-{lines}",  # start N lines back
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to capture pane for node {node_id}: {e}")
            return None

    def get_exit_status(self, node_id: int) -> int | None:
        """Get the exit status of a node's process.

        Reads the .exit_status file written by the _xrun shell helper.

        Returns:
            Exit code, or None if process is still running or file not found.
        """
        if not self._base_dir:
            return None
        status_file = self._base_dir / f"n{node_id}" / ".exit_status"
        try:
            return int(status_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def stop_node(self, node_id: int) -> bool:
        """Stop a node by sending SIGTERM to its process (via .pid file).

        Falls back to sending Ctrl+C to the tmux pane if no PID file found.
        """
        # Try killing via PID file first (reliable with _xrun)
        if self._base_dir:
            pid_file = self._base_dir / f"n{node_id}" / ".pid"
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                return True
            except (FileNotFoundError, ValueError, ProcessLookupError):
                pass

        # Fallback: Ctrl+C to pane
        pane_id = self._validate_pane(node_id)
        if not pane_id:
            return False
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, "C-c", ""],
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to stop node {node_id}: {e}")
            return False

    def start_node(self, node_id: int, command: str) -> bool:
        """Send launch command to node's tmux pane.

        Clears prompt first (C-c C-u) to avoid appending to junk.
        """
        pane_id = self._validate_pane(node_id)
        if not pane_id:
            return False
        try:
            # Clear any partial input
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, "C-c", ""],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, "C-u", ""],
                check=True,
                capture_output=True,
            )
            # Clean up old status files
            if self._base_dir:
                for f in (".pid", ".exit_status"):
                    p = self._base_dir / f"n{node_id}" / f
                    p.unlink(missing_ok=True)
            # Re-inject _xrun and send command
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, _XRUN_FUNC, "Enter"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, command, "Enter"],
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to start node {node_id}: {e}")
            return False

    def finalize(self) -> None:
        """Attach to the tmux session after all nodes are launched."""
        import os

        if not self._session_created:
            return

        # Headless by default — set TMUX_MODE=attach to open iTerm window
        if os.environ.get("TMUX_MODE", "").lower() != "attach":
            logger.info(
                f"tmux session '{TMUX_SESSION_NAME}' running in background. "
                f"Attach with: tmux attach -t {TMUX_SESSION_NAME}"
            )
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
