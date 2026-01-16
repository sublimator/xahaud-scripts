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


@runtime_checkable
class RPCClient(Protocol):
    """Protocol for RPC communication with xahaud nodes.

    Implementations:
        - RequestsRPCClient: HTTP client using requests library
    """

    base_port_rpc: int

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

    def inject(self, node_id: int, tx_blob: str) -> dict[str, Any]:
        """Inject a transaction blob via RPC.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            tx_blob: Hex-encoded transaction blob

        Returns:
            The inject result dict
        """
        ...

    def ping(self, node_id: int, inject: bool = False) -> dict[str, Any] | None:
        """Send a ping command to a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            inject: If True, include inject flag in ping

        Returns:
            The ping result dict, or None if query failed
        """
        ...

    def get_node_data(
        self,
        node_id: int,
        tracked_amendment: str | None = None,
    ) -> dict[str, Any]:
        """Get comprehensive data from a node for monitoring.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            tracked_amendment: Optional amendment ID to track

        Returns:
            Dict with node_id, server_info, amendment_status, response_time, error
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
