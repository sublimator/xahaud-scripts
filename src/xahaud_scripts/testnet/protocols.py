"""Protocol definitions for testnet components.

These protocols define the interfaces for pluggable components like
launchers, RPC clients, and process managers. This enables dependency
injection and makes the system testable and extensible.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import LaunchConfig, NodeInfo


@runtime_checkable
class Launcher(Protocol):
    """Protocol for launching xahaud nodes in terminal windows.

    Implementations:
        - ITermLauncher: Launch in iTerm2 on macOS
        - (Future) TerminalLauncher: Launch in Terminal.app on macOS
        - (Future) TmuxLauncher: Launch in tmux sessions
    """

    def launch(self, node: NodeInfo, config: LaunchConfig) -> bool:
        """Launch a single node in a new terminal window/tab.

        Args:
            node: Node configuration and identity information
            config: Launch configuration including paths and flags

        Returns:
            True if launch succeeded, False otherwise
        """
        ...

    def is_available(self) -> bool:
        """Check if this launcher is available on the current platform.

        Returns:
            True if the launcher can be used on this system
        """
        ...

    def finalize(self) -> None:
        """Called after all nodes have been launched.

        Use this for any cleanup or final actions like attaching to
        a tmux session. Default implementation does nothing.
        """
        ...

    def shutdown(self, base_dir: Path, process_manager: ProcessManager) -> int:
        """Shutdown all nodes launched by this launcher.

        Kills running rippled processes and closes any launcher-specific
        windows/sessions. Does NOT delete generated files.

        Args:
            base_dir: Base directory containing network.json and launcher state
            process_manager: Process manager for killing processes

        Returns:
            Number of processes killed
        """
        ...


@runtime_checkable
class ControllableLauncher(Launcher, Protocol):
    """Launcher that supports per-node lifecycle control."""

    def stop_node(self, node_id: int) -> bool:
        """Stop a specific node."""
        ...

    def start_node(self, node_id: int, command: str) -> bool:
        """Start a specific node with the given command."""
        ...

    def is_session_alive(self) -> bool:
        """Check if the launcher session is alive."""
        ...

    def load_launch_state(self, state: dict[str, Any]) -> None:
        """Restore launcher state from persisted launch_state."""
        ...

    def get_exit_status(self, node_id: int) -> int | None:
        """Get the exit status of a stopped node's process.

        Returns:
            Exit code, or None if node is still alive or not found.
        """
        ...

    def capture_output(self, node_id: int, lines: int = 1000) -> str | None:
        """Capture terminal output from a node.

        Args:
            node_id: The node ID to capture from
            lines: Number of lines of scrollback to capture

        Returns:
            Captured text, or None if capture failed
        """
        ...

    @property
    def launch_state(self) -> dict[str, Any]:
        """Get launch state for persistence.

        Returns dict with at minimum:
          - "launcher": str (launcher type identifier)
          - "pane_ids": dict[str, str] (node_id -> pane_id)
          - "launch_commands": dict[str, str] (node_id -> command)
        """
        ...


@runtime_checkable
class RPCClient(Protocol):
    """Protocol for RPC communication with xahaud nodes.

    Implementations:
        - RequestsRPCClient: HTTP client using requests library
    """

    base_port_rpc: int

    def request(
        self,
        node_id: int,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Make a raw RPC call to a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            method: RPC method name
            params: Optional parameters dict

        Returns:
            The result dict, or None if the call failed
        """
        ...

    def server_info(self, node_id: int) -> dict[str, Any] | None:
        """Get server_info from a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Returns:
            The server_info result dict, or None if query failed
        """
        ...

    def server_definitions(self, node_id: int) -> dict[str, Any] | None:
        """Get server_definitions from a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Returns:
            The server_definitions result dict, or None if query failed
        """
        ...

    def peers(self, node_id: int) -> list[dict[str, Any]] | None:
        """Get peer list from a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Returns:
            List of peer dicts, or None if query failed
        """
        ...

    def ledger(
        self,
        node_id: int,
        ledger_index: str | int = "validated",
        expand: bool = True,
        transactions: bool = False,
        validated: bool = False,
    ) -> dict[str, Any] | None:
        """Get ledger data from a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            ledger_index: Ledger index or "validated", "current", "closed"
            expand: If True, expand transaction details
            transactions: If True, include transactions
            validated: If True, only return if ledger is validated

        Returns:
            The ledger result dict, or None if query failed
        """
        ...

    def log_level(self, node_id: int, partition: str, severity: str) -> bool:
        """Set log level for a partition on a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            partition: Log partition name (e.g., "Validations")
            severity: Log severity (e.g., "trace", "debug", "info")

        Returns:
            True if successful, False otherwise
        """
        ...

    def ping(self, node_id: int) -> dict[str, Any] | None:
        """Send a ping command to a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Returns:
            The ping result dict, or None if query failed
        """
        ...

    def runtime_config(
        self,
        node_id: int,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Send a runtime_config RPC to a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            params: Optional params (set/clear/clear_all).
                    Empty params returns current config.

        Returns:
            The runtime_config result dict, or None if query failed
        """
        ...

    def connect(
        self,
        node_id: int,
        ip: str,
        port: int,
    ) -> dict[str, Any] | None:
        """Tell a node to connect to a peer.

        Args:
            node_id: The node ID to send the connect RPC to.
            ip: IP address of the peer to connect to.
            port: Peer port of the target node.

        Returns:
            The connect result dict, or None if query failed.
        """
        ...

    def disconnect(
        self,
        node_id: int,
        ip: str,
        port: int,
    ) -> dict[str, Any] | None:
        """Tell a node to disconnect from a peer.

        Args:
            node_id: The node ID to send the disconnect RPC to.
            ip: IP address of the peer to disconnect from.
            port: Peer port of the target node.

        Returns:
            The disconnect result dict, or None if query failed.
        """
        ...

    def feature(
        self,
        node_id: int,
        feature_name: str | None = None,
        vetoed: bool | None = None,
    ) -> dict[str, Any] | None:
        """Query or vote on an amendment feature."""
        ...

    def get_node_data(
        self,
        node_id: int,
        tracked_features: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get comprehensive data from a node for monitoring.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            tracked_features: Optional list of feature names to track

        Returns:
            Dict with node_id, server_info, feature_statuses, response_time, error
        """
        ...


@runtime_checkable
class ProcessManager(Protocol):
    """Protocol for managing OS processes.

    Implementations:
        - UnixProcessManager: Process management on Unix-like systems
    """

    def find_by_pattern(self, pattern: str) -> list[int]:
        """Find process IDs matching a pattern.

        Args:
            pattern: Pattern to match (used with pgrep -f)

        Returns:
            List of matching PIDs
        """
        ...

    def kill(self, pid: int, signal: int = 9) -> bool:
        """Kill a process by PID.

        Args:
            pid: Process ID to kill
            signal: Signal to send (default: 9 = SIGKILL)

        Returns:
            True if kill succeeded, False otherwise
        """
        ...

    def is_port_listening(self, port: int) -> bool:
        """Check if a port is currently listening.

        Args:
            port: Port number to check

        Returns:
            True if port is listening, False otherwise
        """
        ...

    def get_process_info(self, port: int) -> dict[str, str] | None:
        """Get information about the process listening on a port.

        Args:
            port: Port number to check

        Returns:
            Dict with 'pid' and 'process' keys, or None if not listening
        """
        ...

    def get_port_state(self, port: int) -> list[dict[str, str]]:
        """Get all TCP connections using a port (any state).

        Catches LISTEN, TIME_WAIT, CLOSE_WAIT, ESTABLISHED, etc.

        Args:
            port: Port number to check

        Returns:
            List of dicts with 'process', 'pid', 'state' keys
        """
        ...

    def check_ports_free(self, ports: list[int]) -> dict[int, list[dict[str, str]]]:
        """Check if ports are free, returning any that are in use.

        Args:
            ports: List of port numbers to check

        Returns:
            Dict mapping port -> list of connections (empty dict if all free)
        """
        ...


@runtime_checkable
class KeyGenerator(Protocol):
    """Protocol for generating validator keys.

    Implementations:
        - ValidatorKeysGenerator: Uses validator-keys CLI tool
    """

    def generate(self, node_id: int, output_dir: Path) -> dict[str, str]:
        """Generate validator keys for a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            output_dir: Directory to write key files

        Returns:
            Dict with 'public_key', 'token', and 'keyfile' keys
        """
        ...
