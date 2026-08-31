"""Tmux launcher for xahaud nodes.

This module provides a launcher that creates a tmux session with
panes for each node. Processes can be killed with Ctrl+C and
restarted manually since the shell stays alive.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xahaud_scripts.utils.logging import make_logger
from xahaud_scripts.utils.quoting import shell_export

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import LaunchConfig, NodeInfo
    from xahaud_scripts.testnet.protocols import ProcessManager

logger = make_logger(__name__)

TMUX_SESSION_NAME = "xahaud-testnet"


def _process_output(value: str | bytes | None) -> str:
    """Normalize captured subprocess output without assuming byte mode."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return value.strip() if value else ""


# Shell function injected into each pane before launching a node.
# Saves PID and exit status to the node's working directory.
# Compatible with bash and zsh. Process runs in foreground (output
# visible, Ctrl+C works). Leading space avoids zsh history.
_XRUN_FUNC = (
    ' _xrun() { rm -f .pid .exit_status; "$@" & local p=$!;'
    ' local r="$$:$p:$RANDOM:$RANDOM"; printf "%s %s\\n" "$r" "$p" > .pid;'
    " trap 'kill $p 2>/dev/null' INT TERM;"
    " wait $p; local s=$?; trap - INT TERM;"
    ' printf "%s %s %s\\n" "$r" "$p" "$s" > .exit_status; }'
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
        self._pane_owner_tokens: dict[int, str] = {}
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
            if stderr := _process_output(e.stderr):
                logger.error(f"  tmux stderr: {stderr}")
            if stdout := _process_output(e.stdout):
                logger.error(f"  tmux stdout: {stdout}")
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
        self._set_pane_owner(node.id, pane_id)

        self._clear_node_markers(node.id)

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
        self._set_pane_owner(node.id, pane_id)

        # Rebalance panes to tiled layout
        subprocess.run(
            ["tmux", "select-layout", "-t", TMUX_SESSION_NAME, "tiled"],
            check=True,
            capture_output=True,
        )

        self._clear_node_markers(node.id)

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

    def build_launch_command(self, node: NodeInfo, config: LaunchConfig) -> str:
        """Build a node's full launch command (public wrapper over the builder).

        Reused when swapping a node's binary: rebuilding through the same
        builder reproduces env vars, startup flags and lldb wrapping exactly,
        so only the rippled path (via ``config.get_rippled_path``) changes.
        """
        return self._build_full_command(node, config)

    def _build_full_command(self, node: NodeInfo, config: LaunchConfig) -> str:
        """Build the full command with env vars and startup flags."""
        env_vars = self._build_env_vars(node, config)
        startup_flags = self._build_startup_flags(node, config)
        binary = shlex.quote(str(config.get_rippled_path(node.id)))
        args = f"--conf {shlex.quote(str(node.config_path))} {startup_flags}"

        if node.id in config.lldb_nodes:
            from xahaud_scripts.utils.lldb import create_lldb_script

            script = create_lldb_script(all_threads=False)
            cmd = f"lldb -s {shlex.quote(str(script))} -- {binary} {args}"
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
            parts.append(shell_export(key, value))

        # Node-specific environment variables (override global)
        if node.id in config.node_env:
            for key, value in config.node_env[node.id].items():
                parts.append(shell_export(key, value))

        return " && ".join(parts)

    def _build_startup_flags(self, node: NodeInfo, config: LaunchConfig) -> str:
        """Build startup flags for the rippled command."""
        parts = []

        # Genesis ledger file
        parts.append(f"--ledgerfile {shlex.quote(str(config.genesis_file))}")

        # Quorum setting
        if config.quorum is not None:
            parts.append(f"--quorum {config.quorum}")

        # Extra arguments. Quote each one: these strings are joined into a
        # command that is typed into a shell, so an unquoted value with a space
        # would split into two argv entries and a $(...) would be executed.
        if config.extra_args:
            parts.extend(shlex.quote(arg) for arg in config.extra_args)

        return " ".join(parts)

    @property
    def launch_state(self) -> dict[str, Any]:
        """Get launch state for persistence."""
        state: dict[str, Any] = {
            "launcher": "tmux",
            "pane_ids": {str(k): v for k, v in self._pane_ids.items()},
            "pane_owner_tokens": {
                str(k): v for k, v in self._pane_owner_tokens.items()
            },
            "launch_commands": {str(k): v for k, v in self._launch_commands.items()},
        }
        if self._base_dir is not None:
            state["base_dir"] = str(self._base_dir)
        return state

    def load_launch_state(self, state: dict[str, Any]) -> None:
        """Restore state from persisted launch_state."""
        self._pane_ids = {int(k): v for k, v in state.get("pane_ids", {}).items()}
        self._pane_owner_tokens = {
            int(k): v for k, v in state.get("pane_owner_tokens", {}).items()
        }
        self._launch_commands = {
            int(k): v for k, v in state.get("launch_commands", {}).items()
        }
        base_dir = state.get("base_dir")
        if isinstance(base_dir, str) and base_dir:
            self._base_dir = Path(base_dir)

    def _clear_node_markers(self, node_id: int) -> None:
        """Remove PID/status markers before dispatching a new process generation."""
        if self._base_dir is None:
            return
        for name in (".pid", ".exit_status"):
            (self._base_dir / f"n{node_id}" / name).unlink(missing_ok=True)

    def _read_pid_record(self, node_id: int) -> tuple[str, int] | None:
        """Read the generation-tagged PID marker written by ``_xrun``."""
        if self._base_dir is None:
            return None
        try:
            generation, pid_text = (
                (self._base_dir / f"n{node_id}" / ".pid").read_text().split()
            )
            pid = int(pid_text)
        except (FileNotFoundError, ValueError):
            return None
        if pid <= 1:
            return None
        return generation, pid

    def _pid_belongs_to_node(self, pid: int, node_id: int) -> bool:
        """Confirm a PID still runs this node's exact generated config."""
        if self._base_dir is None:
            return False
        expected_config = str(self._base_dir / f"n{node_id}" / "xahaud.cfg")
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        # The config path must be the complete --conf value.  A raw substring
        # check would accept an unrelated command using e.g. xahaud.cfg.backup
        # and could signal that process after PID reuse.
        pattern = re.compile(
            rf"(?:^|\s)--conf\s+{re.escape(expected_config)}(?=\s+--|\s*$)"
        )
        return pattern.search(result.stdout) is not None

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

    def _pane_current_path(self, pane_id: str) -> Path | None:
        """Return the working directory of the process currently in a pane."""
        try:
            result = subprocess.run(
                [
                    "tmux",
                    "display-message",
                    "-p",
                    "-t",
                    pane_id,
                    "#{pane_current_path}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip()
        return Path(value) if value else None

    def _set_pane_owner(self, node_id: int, pane_id: str) -> None:
        """Tag a new pane with an unguessable owner token for reuse detection."""
        token = secrets.token_hex(16)
        subprocess.run(
            [
                "tmux",
                "set-option",
                "-p",
                "-t",
                pane_id,
                "@xahaud_owner",
                token,
            ],
            check=True,
            capture_output=True,
        )
        self._pane_owner_tokens[node_id] = token

    def _pane_owner(self, pane_id: str) -> str | None:
        """Return the owner token attached to a live pane, if any."""
        try:
            result = subprocess.run(
                [
                    "tmux",
                    "show-options",
                    "-p",
                    "-q",
                    "-v",
                    "-t",
                    pane_id,
                    "@xahaud_owner",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip()
        return value if value else None

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
        expected_owner = self._pane_owner_tokens.get(node_id)
        if expected_owner is None:
            logger.error(
                f"Refusing untagged pane {pane_id} for node {node_id}: persisted "
                "launch state predates pane ownership tokens; relaunch the network"
            )
            return None
        current_owner = self._pane_owner(pane_id)
        if current_owner != expected_owner:
            logger.error(
                f"Refusing pane {pane_id} for node {node_id}: owner token "
                "does not match persisted launch state"
            )
            return None
        if self._base_dir is not None:
            expected_path = (self._base_dir / f"n{node_id}").resolve()
            current_path = self._pane_current_path(pane_id)
            if current_path is None or current_path.resolve() != expected_path:
                logger.error(
                    f"Refusing pane {pane_id} for node {node_id}: current path "
                    f"{current_path!s} does not match {expected_path}"
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
        pid_record = self._read_pid_record(node_id)
        if pid_record is None:
            return None
        generation, pid = pid_record
        status_file = self._base_dir / f"n{node_id}" / ".exit_status"
        try:
            status_generation, status_pid_text, status_text = (
                status_file.read_text().split()
            )
            status_pid = int(status_pid_text)
            status = int(status_text)
        except (FileNotFoundError, ValueError):
            return None
        if status_generation != generation or status_pid != pid:
            return None
        return status

    def stop_node(self, node_id: int) -> bool:
        """Stop a node by sending SIGTERM to its process (via .pid file).

        Falls back to sending Ctrl+C to the tmux pane if no PID file found.
        """
        # Only signal a marker PID after verifying that its current command line
        # still belongs to this exact node. PID reuse must never target an
        # unrelated host process.
        if self.get_exit_status(node_id) is not None:
            logger.warning(f"Node {node_id} is already stopped")
            return False
        pid_record = self._read_pid_record(node_id)
        if pid_record is not None:
            _generation, pid = pid_record
            if self._pid_belongs_to_node(pid, node_id):
                try:
                    os.kill(pid, signal.SIGTERM)
                    return True
                except ProcessLookupError:
                    pass
            else:
                logger.warning(
                    f"Refusing to signal unverified PID {pid} for node {node_id}; "
                    "falling back to its tmux pane"
                )

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
            self._clear_node_markers(node_id)
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
