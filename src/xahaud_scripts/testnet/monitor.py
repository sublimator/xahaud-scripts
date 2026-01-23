"""Network monitoring and display utilities.

This module provides:
- Rich table display for network status
- Amendment status checking
- Topology display
- Port status display
- Async network monitoring loop
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

from xahaud_scripts.testnet.websocket import PersistentWebSocketManager
from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import NetworkConfig, NodeInfo
    from xahaud_scripts.testnet.protocols import ProcessManager, RPCClient

logger = make_logger(__name__)
console = Console()


def format_uptime(seconds: float) -> str:
    """Format seconds as human-readable uptime."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}m {secs}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def display_network_status(
    node_data: dict[int, dict[str, Any]],
    node_count: int,
    tracked_amendment: str | None = None,
    ledger_events: dict[int, dict[str, Any]] | None = None,
    uptime_seconds: float | None = None,
) -> None:
    """Display network status as a rich table.

    Args:
        node_data: Dict mapping node_id -> node data from get_node_data()
        node_count: Number of nodes
        tracked_amendment: Optional amendment ID being tracked
        uptime_seconds: Optional uptime in seconds to display
    """
    title = f"Network Status ({node_count} nodes)"
    if tracked_amendment:
        title += f" - Amendment: {tracked_amendment[:16]}..."

    table = Table(title=title)
    table.add_column("Node", style="cyan", no_wrap=True)
    table.add_column("State", style="green")
    table.add_column("Ledger", justify="right", style="yellow")
    table.add_column("Hash", style="dim", no_wrap=True)
    table.add_column("Txns", justify="right", style="magenta")
    table.add_column("Peers", justify="right", style="blue")
    table.add_column("Props", justify="right", style="blue")
    table.add_column("Quorum", justify="right", style="blue")
    table.add_column("Conv", justify="right", style="white")
    table.add_column("Amend", style="white")
    table.add_column("Δt", justify="right", style="cyan")

    for node_id in range(node_count):
        data = node_data.get(node_id, {})

        if data.get("error"):
            table.add_row(
                str(node_id),
                f"ERROR: {data['error']}",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                f"{data.get('response_time', 0):.3f}s",
            )
            continue

        server_info = data.get("server_info")
        if not server_info:
            table.add_row(
                str(node_id),
                "NOT RESPONDING",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                f"{data.get('response_time', 0):.3f}s",
            )
            continue

        state = server_info.get("info", {})
        last_close = state.get("last_close", {})
        proposers = last_close.get("proposers", "N/A")
        converge_time = last_close.get("converge_time_s", "N/A")
        # Get txn_count from ledger_events (WebSocket) if available
        if ledger_events and node_id in ledger_events:
            txn_count = ledger_events[node_id].get("txn_count", "N/A")
        else:
            txn_count = "?"
        validation_quorum = state.get("validation_quorum", "N/A")
        validated_ledger = state.get("validated_ledger", {})
        ledger_seq = validated_ledger.get("seq", "N/A")
        ledger_hash = validated_ledger.get("hash", "N/A")

        # Truncate hash for display
        if ledger_hash != "N/A" and len(ledger_hash) > 12:
            ledger_hash_display = ledger_hash[:12] + "..."
        else:
            ledger_hash_display = ledger_hash

        # Amendment status display
        amend_status = data.get("amendment_status", {})
        if amend_status:
            status = amend_status.get("status")
            if status == "not_found":
                amend_display = "?"
            elif status == "not_synced":
                amend_display = "!"
            elif amend_status.get("enabled", False):
                amend_display = "ON"
            else:
                amend_display = "OFF"
        else:
            amend_display = "-"

        # Format converge time
        if isinstance(converge_time, (int, float)):
            converge_str = f"{converge_time:.2f}"
        else:
            converge_str = str(converge_time)

        table.add_row(
            str(node_id),
            state.get("server_state", "unknown"),
            str(ledger_seq),
            ledger_hash_display,
            str(txn_count),
            str(state.get("peers", 0)),
            str(proposers),
            str(validation_quorum),
            converge_str,
            amend_display,
            f"{data.get('response_time', 0):.3f}s",
        )

    console.print(table)

    # Debug: dump server_info for node 0 if DEBUG=1
    if os.environ.get("DEBUG") == "1":
        node0_data = node_data.get(0, {})
        server_info = node0_data.get("server_info", {})
        info = server_info.get("info", {})
        console.print("\n[dim]DEBUG: server_info.info (node 0):[/dim]")
        console.print(json.dumps(info, indent=2))


def display_txn_histogram(
    rpc_client: RPCClient,
    ledger_index: int,
) -> dict[str, int]:
    """Display a transaction type histogram for the given ledger.

    Args:
        rpc_client: RPC client for queries
        ledger_index: Ledger index to query

    Returns:
        Dict mapping transaction type -> count for this ledger
    """
    # Retry a few times if ledger isn't validated yet
    result = None
    for _attempt in range(3):
        result = rpc_client.ledger(
            0, ledger_index=ledger_index, transactions=True, validated=True
        )
        if result and result.get("validated"):
            break
        time.sleep(0.1)

    if not result:
        return {}

    ledger = result.get("ledger", {})
    transactions = ledger.get("transactions", [])

    if not transactions:
        return {}

    # Count transaction types and collect Shuffle-specific fields
    type_counts: dict[str, int] = {}
    shuffle_ledger_seqs: set[int] = set()
    shuffle_parent_hashes: set[str] = set()

    for tx in transactions:
        tx_type = tx.get("TransactionType", "Unknown")
        type_counts[tx_type] = type_counts.get(tx_type, 0) + 1

        # Collect Shuffle-specific fields
        if tx_type == "Shuffle":
            if "LedgerSequence" in tx:
                shuffle_ledger_seqs.add(tx["LedgerSequence"])
            if "ParentHash" in tx:
                shuffle_parent_hashes.add(tx["ParentHash"])

    # Sort by count (descending), then by name
    sorted_types = sorted(type_counts.items(), key=lambda x: (-x[1], x[0]))

    # Build display parts
    parts = []
    for name, count in sorted_types:
        if name == "Shuffle" and (shuffle_ledger_seqs or shuffle_parent_hashes):
            # Build Shuffle-specific details
            details = []
            if shuffle_ledger_seqs:
                seqs = ",".join(str(s) for s in sorted(shuffle_ledger_seqs))
                details.append(f"Seq={seqs}")
            if shuffle_parent_hashes:
                hashes = ",".join(h[:12] + "..." for h in sorted(shuffle_parent_hashes))
                details.append(f"Parent={hashes}")
            parts.append(f"[cyan]{name}[/cyan]:{count}({' '.join(details)})")
        else:
            parts.append(f"[cyan]{name}[/cyan]:{count}")

    console.print(f"[dim]Txns:[/dim] {' '.join(parts)}")
    return type_counts


def display_amendment_status(
    rpc_client: RPCClient,
    nodes: list[NodeInfo],
    tracked_amendment: str,
) -> None:
    """Display detailed amendment status for all nodes.

    Args:
        rpc_client: RPC client for queries
        nodes: List of node configurations
        tracked_amendment: Amendment ID to check
    """
    tracked_upper = tracked_amendment.upper()

    console.print(f"\n[bold]TRACKED AMENDMENT: {tracked_upper[:16]}...[/bold]\n")

    table = Table(title="Amendment Status")
    table.add_column("Node", style="cyan", no_wrap=True)
    table.add_column("Role", style="white")
    table.add_column("Ledger", justify="right", style="yellow")
    table.add_column("Status", style="white")
    table.add_column("Name", style="green")
    table.add_column("Supp", justify="center", style="blue")
    table.add_column("Veto", justify="center", style="red")

    for node in nodes:
        # Get ledger index
        server_info = rpc_client.server_info(node.id)
        ledger_index = "N/A"
        if server_info and "info" in server_info:
            validated = server_info["info"].get("validated_ledger", {})
            ledger_index = validated.get("seq", "N/A")

        # Get server definitions
        defs = rpc_client.server_definitions(node.id)

        if defs is None:
            table.add_row(
                str(node.id),
                node.role,
                str(ledger_index),
                "Query failed",
                "-",
                "-",
                "-",
            )
            continue

        if "error" in defs:
            table.add_row(
                str(node.id),
                node.role,
                str(ledger_index),
                "Not synced",
                "-",
                "-",
                "-",
            )
            continue

        features = defs.get("features", {})
        if tracked_upper in features:
            feature = features[tracked_upper]
            name = feature.get("name", "UNKNOWN")
            enabled = feature.get("enabled", False)
            supported = feature.get("supported", False)
            vetoed = feature.get("vetoed", False)

            status = "ENABLED" if enabled else "DISABLED"

            table.add_row(
                str(node.id),
                node.role,
                str(ledger_index),
                status,
                name,
                "Y" if supported else "N",
                "Y" if vetoed else "N",
            )

            if enabled and node.is_injector:
                console.print(
                    f"[bold red]EXPLOIT SUCCESS on Node {node.id}![/bold red]"
                )
        else:
            table.add_row(
                str(node.id),
                node.role,
                str(ledger_index),
                "Not found",
                "-",
                "-",
                "-",
            )

    console.print(table)


def display_topology(
    rpc_client: RPCClient,
    nodes: list[NodeInfo],
) -> None:
    """Display peer connection topology for all nodes.

    Args:
        rpc_client: RPC client for queries
        nodes: List of node configurations
    """
    console.print("\n[bold]Network Peer Topology[/bold]\n")

    for node in nodes:
        peers = rpc_client.peers(node.id)

        if peers is None:
            console.print(f"Node {node.id} [{node.role}]: [red]Query failed[/red]\n")
            continue

        console.print(f"Node {node.id} [{node.role}] - {len(peers)} peer(s):")
        for peer in peers:
            address = peer.get("address", "unknown")
            peer_type = peer.get("type", "null")
            uptime = peer.get("uptime", 0)
            console.print(f"  -> {address} (type: {peer_type}, uptime: {uptime}s)")
        console.print()


def display_port_status(
    process_manager: ProcessManager,
    nodes: list[NodeInfo],
) -> None:
    """Display which peer and RPC ports are listening.

    Args:
        process_manager: Process manager for port checks
        nodes: List of node configurations from network.json
    """
    console.print(f"\n[bold]Port Status ({len(nodes)} nodes)[/bold]\n")

    for node in nodes:
        role = node.role

        # Check peer port
        peer_status = (
            "[green]UP[/green]"
            if process_manager.is_port_listening(node.port_peer)
            else "[red]DOWN[/red]"
        )

        # Check RPC port
        rpc_status = (
            "[green]UP[/green]"
            if process_manager.is_port_listening(node.port_rpc)
            else "[red]DOWN[/red]"
        )

        # Get process info if peer port is listening
        pid_info = ""
        if process_manager.is_port_listening(node.port_peer):
            info = process_manager.get_process_info(node.port_peer)
            if info:
                pid_info = f" (PID: {info['pid']})"

        console.print(
            f"Node {node.id} [{role}]: "
            f"Peer {node.port_peer} {peer_status}, "
            f"RPC {node.port_rpc} {rpc_status}{pid_info}"
        )


def dump_configs(nodes: list[NodeInfo]) -> None:
    """Dump all node configurations.

    Args:
        nodes: List of node configurations
    """
    console.print("\n[bold]Node Configurations[/bold]\n")
    console.print("=" * 80)

    for node in nodes:
        config_file = node.config_path
        validators_file = node.node_dir / "validators.txt"

        console.print(f"\n{'=' * 80}")
        console.print(f"Node {node.id} [{node.role}]: {config_file}")
        console.print("=" * 80)

        if config_file.exists():
            console.print(config_file.read_text(), markup=False)

        if validators_file.exists():
            console.print(f"\n{'=' * 80}")
            console.print(f"Node {node.id} [{node.role}]: {validators_file}")
            console.print("=" * 80)
            console.print(validators_file.read_text(), markup=False)

    console.print("=" * 80)


class NetworkMonitor:
    """Async network monitoring with persistent WebSocket connections.

    Uses long-lived WebSocket connections to each node with automatic
    reconnection and keepalive for reliable event streaming.

    Attributes:
        rpc_client: RPC client for queries
        network_config: Network configuration
        tracked_amendment: Optional amendment ID to track
    """

    def __init__(
        self,
        rpc_client: RPCClient,
        network_config: NetworkConfig,
        tracked_amendment: str | None = None,
    ) -> None:
        """Initialize the network monitor.

        Args:
            rpc_client: RPC client for queries
            network_config: Network configuration
            tracked_amendment: Optional amendment ID to track
        """
        self.rpc_client = rpc_client
        self.network_config = network_config
        self.tracked_amendment = tracked_amendment
        self._start_time: float | None = None

        # Convergence tracking
        self._total_conv_sum: float = 0.0  # Sum of all convergence times
        self._total_conv_count: int = 0  # Number of data points
        self._recent_conv_times: deque[list[float]] = deque(
            maxlen=10
        )  # Last 10 ledgers

        # Transaction count tracking (per type)
        self._txn_totals: dict[str, int] = {}  # Total count per type
        self._txn_ledger_count: int = 0  # Number of ledgers tracked
        self._recent_txn_counts: deque[dict[str, int]] = deque(
            maxlen=10
        )  # Last 10 ledgers
        # Count distribution: tx_type -> {count: num_ledgers}
        self._txn_count_dist: dict[str, dict[int, int]] = {}

        # Stall tracking (10+ seconds without ledger close)
        self._stall_count: int = 0
        self._in_stall: bool = False
        self._stall_start: float | None = None
        self._longest_stall: float = 0.0

        # Create persistent WebSocket manager (started in monitor())
        self._ws_manager = PersistentWebSocketManager(
            base_port_ws=network_config.base_port_ws,
            node_count=network_config.node_count,
        )

    def _get_uptime(self) -> float | None:
        """Get seconds since monitoring started."""
        if self._start_time is None:
            return None
        return time.time() - self._start_time

    def _update_convergence_stats(self, node_data: dict[int, dict[str, Any]]) -> None:
        """Extract convergence times from node data and update tracking stats.

        Args:
            node_data: Dict mapping node_id -> node data from _fetch_all_node_data()
        """
        ledger_conv_times: list[float] = []

        for data in node_data.values():
            if data.get("error"):
                continue
            server_info = data.get("server_info")
            if not server_info:
                continue
            info = server_info.get("info", {})
            last_close = info.get("last_close", {})
            converge_time = last_close.get("converge_time_s")

            if isinstance(converge_time, (int, float)) and converge_time > 0:
                ledger_conv_times.append(float(converge_time))
                self._total_conv_sum += float(converge_time)
                self._total_conv_count += 1

        # Add this ledger's times to the rolling window
        if ledger_conv_times:
            self._recent_conv_times.append(ledger_conv_times)

    def _display_convergence_averages(self) -> None:
        """Display convergence averages below the status table."""
        # Calculate last 10 ledgers average
        if self._recent_conv_times:
            all_recent = [t for ledger in self._recent_conv_times for t in ledger]
            recent_avg = sum(all_recent) / len(all_recent)
            recent_avg_str = f"{recent_avg:.3f}s"
            ledger_count = len(self._recent_conv_times)
        else:
            recent_avg_str = "N/A"
            ledger_count = 0

        # Calculate cumulative average
        if self._total_conv_count > 0:
            cumul_avg = self._total_conv_sum / self._total_conv_count
            cumul_avg_str = f"{cumul_avg:.3f}s"
        else:
            cumul_avg_str = "N/A"

        # Store for combined display
        self._last_conv_display = (
            f"last{ledger_count}={recent_avg_str} "
            f"cumul={cumul_avg_str} ({self._total_conv_count} samples)"
        )

    def _display_averages_and_histogram(self) -> None:
        """Display combined averages and convergence histogram."""
        # Get conv display (set by _display_convergence_averages)
        conv_line = getattr(self, "_last_conv_display", "N/A")

        # Get txn averages
        if self._txn_ledger_count:
            recent_totals: dict[str, int] = {}
            for ledger_counts in self._recent_txn_counts:
                for tx_type, count in ledger_counts.items():
                    recent_totals[tx_type] = recent_totals.get(tx_type, 0) + count

            ledger_count = len(self._recent_txn_counts)
            recent_parts = []
            for tx_type in sorted(recent_totals.keys()):
                avg = recent_totals[tx_type] / ledger_count
                recent_parts.append(f"{tx_type}:{avg:.1f}")

            cumul_parts = []
            for tx_type in sorted(self._txn_totals.keys()):
                avg = self._txn_totals[tx_type] / self._txn_ledger_count
                cumul_parts.append(f"{tx_type}:{avg:.1f}")

            txn_line = (
                f"last{ledger_count}=[{' '.join(recent_parts)}] "
                f"cumul=[{' '.join(cumul_parts)}]"
            )
        else:
            txn_line = "N/A"

        # Display combined output
        console.print(f"[dim]Avg Conv:[/dim] {conv_line}")
        console.print(f"[dim]Avg Txns:[/dim] {txn_line}")

        # Display txn count distributions per type
        self._display_txn_distributions()

    def _update_txn_stats(self, type_counts: dict[str, int]) -> None:
        """Update transaction count tracking stats.

        Args:
            type_counts: Dict mapping transaction type -> count for this ledger
        """
        if not type_counts:
            return

        # Update totals and count distribution
        for tx_type, count in type_counts.items():
            self._txn_totals[tx_type] = self._txn_totals.get(tx_type, 0) + count
            # Track distribution: how many ledgers had this count
            if tx_type not in self._txn_count_dist:
                self._txn_count_dist[tx_type] = {}
            dist = self._txn_count_dist[tx_type]
            dist[count] = dist.get(count, 0) + 1

        self._txn_ledger_count += 1

        # Add to rolling window
        self._recent_txn_counts.append(type_counts)

    def _display_txn_distributions(self) -> None:
        """Display transaction count distributions per type."""
        if not self._txn_count_dist:
            return

        for tx_type in sorted(self._txn_count_dist.keys()):
            dist = self._txn_count_dist[tx_type]
            # Sort by count descending, show {count: num_ledgers}
            sorted_dist = sorted(dist.items(), key=lambda x: -x[1])
            dist_str = ", ".join(f"{count}:{num}" for count, num in sorted_dist)
            console.print(f"[dim]{tx_type}:[/dim] {{{dist_str}}}")

        # Show stall stats if any
        if self._stall_count > 0:
            console.print(
                f"[yellow]Stalls:[/yellow] {self._stall_count} "
                f"(longest: {self._longest_stall:.1f}s)"
            )

    def _enter_stall(self) -> None:
        """Mark that we've entered a stall (no ledger for 10+ seconds)."""
        if not self._in_stall:
            self._in_stall = True
            self._stall_start = time.time()
            self._stall_count += 1

    def _exit_stall(self) -> None:
        """Mark that we've exited a stall (received a ledger)."""
        if self._in_stall and self._stall_start is not None:
            stall_duration = time.time() - self._stall_start
            self._longest_stall = max(self._longest_stall, stall_duration)
            self._in_stall = False
            self._stall_start = None

    async def monitor(self, stop_after_first_ledger: bool = False) -> int:
        """Run the monitoring loop with persistent WebSocket connections.

        Uses async context manager for proper connection lifecycle.
        Connections are maintained in background tasks with auto-reconnect.

        Args:
            stop_after_first_ledger: If True, return after first ledger closes
                                     instead of continuous monitoring.

        Returns:
            The ledger index when stopped (0 if failed to start).
        """
        node_count = self.network_config.node_count
        last_ledger_index = 0
        self._start_time = time.time()

        try:
            async with self._ws_manager:
                # Wait for at least one node to connect
                console.print("[yellow]Connecting to nodes...[/yellow]")

                if not await self._ws_manager.wait_until_ready(timeout=30.0):
                    console.print("[red]Failed to connect to any nodes[/red]")
                    return 0

                # Show connection status
                status = self._ws_manager.get_connection_status()
                connected_count = sum(1 for c in status.values() if c)
                console.print(
                    f"[green]Connected to {connected_count}/{node_count} nodes, "
                    "monitoring ledger closes...[/green]\n"
                )

                # Initial polling phase
                console.print(
                    "[yellow]Waiting for first ledger close (polling every 3s)...[/yellow]"
                )
                first_ledger_received = False

                while not first_ledger_received:
                    uptime = self._get_uptime()
                    uptime_str = f" (up {format_uptime(uptime)})" if uptime else ""
                    console.print(
                        f"\n[bold]Network Status - {time.strftime('%H:%M:%S')}"
                        f"{uptime_str}[/bold]\n"
                    )

                    # Fetch node data in parallel
                    node_data = self._fetch_all_node_data()
                    self._update_convergence_stats(node_data)
                    display_network_status(
                        node_data,
                        node_count,
                        self.tracked_amendment,
                        uptime_seconds=uptime,
                    )
                    self._display_convergence_averages()

                    # Check buffered events from WebSocket
                    events = self._ws_manager.get_latest_events()
                    for event in events.values():
                        ledger_seq = event.get("ledger_index", 0)
                        if isinstance(ledger_seq, int) and ledger_seq > 1:
                            first_ledger_received = True
                            last_ledger_index = ledger_seq
                            break

                    if not first_ledger_received:
                        await asyncio.sleep(3)
                    else:
                        if stop_after_first_ledger:
                            console.print(
                                f"[green]First ledger close detected "
                                f"(index {last_ledger_index})[/green]\n"
                            )
                            return last_ledger_index
                        console.print(
                            f"[green]First ledger close detected (index {last_ledger_index}), "
                            "event-driven monitoring active[/green]\n"
                        )

                # Event-driven monitoring phase
                last_ledger_events: dict[int, dict[str, Any]] | None = None
                missed_events_count = 0

                while True:
                    next_ledger_index = last_ledger_index + 1

                    # Wait for new ledger from buffered events
                    ledger_events = await self._ws_manager.wait_for_new_ledger(
                        min_ledger_index=next_ledger_index,
                        timeout=10.0,
                    )

                    if ledger_events:
                        missed_events_count = 0  # Reset counter on success
                        self._exit_stall()  # End any active stall

                        # Debug: dump ledgerClosed event
                        if os.environ.get("DEBUG") == "1":
                            first_event = next(iter(ledger_events.values()), {})
                            console.print("\n[dim]DEBUG: ledgerClosed event:[/dim]")
                            console.print(json.dumps(first_event, indent=2))

                        max_index = max(
                            event.get("ledger_index", 0)
                            for event in ledger_events.values()
                        )
                        last_ledger_index = max_index
                        last_ledger_events = ledger_events
                    else:
                        missed_events_count += 1
                        self._enter_stall()  # Mark stall start (only counts once per stall)

                        # Show detailed diagnostics
                        diag = self._ws_manager.get_diagnostics()
                        connected_count = sum(
                            1 for n in diag["nodes"].values() if n["connected"]
                        )

                        # Build diagnostic line showing buffered ledger indices
                        buffered = []
                        for nid, info in sorted(diag["nodes"].items()):
                            if info["latest_index"]:
                                age = info["time_since_event"]
                                age_str = f"{age:.0f}s" if age else "?"
                                buffered.append(
                                    f"n{nid}={info['latest_index']}({age_str})"
                                )

                        if connected_count > 0:
                            msg = (
                                f"[yellow]No ledger close events received "
                                f"(waiting for ledger {next_ledger_index}), "
                                f"connected to {connected_count}/{node_count} nodes"
                            )
                            if buffered:
                                msg += f", buffered: {' '.join(buffered)}"
                            msg += "[/yellow]"
                            console.print(msg)
                        else:
                            console.print(
                                "[red]All WebSocket connections lost, reconnecting...[/red]"
                            )

                        # After 3 missed events, resync ledger index from RPC
                        if missed_events_count >= 3:
                            server_info = self.rpc_client.server_info(0)
                            if server_info and "info" in server_info:
                                validated = server_info["info"].get(
                                    "validated_ledger", {}
                                )
                                current_seq = validated.get("seq", 0)
                                if (
                                    isinstance(current_seq, int)
                                    and current_seq > 0
                                    and current_seq != last_ledger_index
                                ):
                                    console.print(
                                        f"[cyan]Resyncing: ledger moved from "
                                        f"{last_ledger_index} to {current_seq}[/cyan]"
                                    )
                                    last_ledger_index = current_seq
                                    missed_events_count = 0

                        await asyncio.sleep(5)
                        continue

                    # Display status after receiving events
                    uptime = self._get_uptime()
                    uptime_str = f" (up {format_uptime(uptime)})" if uptime else ""
                    console.print(
                        f"\n[bold]Network Status - {time.strftime('%H:%M:%S')}"
                        f"{uptime_str}[/bold]\n"
                    )

                    node_data = self._fetch_all_node_data()
                    self._update_convergence_stats(node_data)
                    display_network_status(
                        node_data,
                        node_count,
                        self.tracked_amendment,
                        last_ledger_events,
                        uptime_seconds=uptime,
                    )

                    # Update stats
                    self._display_convergence_averages()  # Stores result for combined display
                    txn_counts = display_txn_histogram(
                        self.rpc_client, last_ledger_index
                    )
                    self._update_txn_stats(txn_counts)

                    # Display combined averages and histogram
                    console.print("[dim]───[/dim]")
                    self._display_averages_and_histogram()

        except KeyboardInterrupt:
            console.print("\n\n[bold yellow]Monitoring stopped by user[/bold yellow]")

        return 0

    def _fetch_all_node_data(self) -> dict[int, dict[str, Any]]:
        """Fetch data from all nodes in parallel.

        If some nodes are slightly behind the majority, re-fetches them
        after a short delay to reduce race condition noise in the display.

        Returns:
            Dict mapping node_id -> node data
        """
        node_data: dict[int, dict[str, Any]] = {}

        with ThreadPoolExecutor(max_workers=self.network_config.node_count) as executor:
            futures = {
                executor.submit(
                    self.rpc_client.get_node_data, node_id, self.tracked_amendment
                ): node_id
                for node_id in range(self.network_config.node_count)
            }

            for future in as_completed(futures):
                data = future.result()
                node_data[data["node_id"]] = data

            # Find majority ledger and re-fetch lagging nodes (inside with block)
            node_data = self._refetch_lagging_nodes(node_data, executor)

        return node_data

    def _refetch_lagging_nodes(
        self,
        node_data: dict[int, dict[str, Any]],
        executor: ThreadPoolExecutor,
    ) -> dict[int, dict[str, Any]]:
        """Re-fetch nodes that are slightly behind the majority ledger.

        Only re-fetches if:
        - There's a clear majority (>50% of nodes on same ledger)
        - Lagging nodes are exactly 1 ledger behind
        - At least some nodes responded successfully

        Args:
            node_data: Initial node data from parallel fetch
            executor: Thread pool executor to reuse

        Returns:
            Updated node_data with re-fetched lagging nodes
        """
        # Extract ledger indices from successful responses
        ledger_indices: dict[int, int] = {}
        for node_id, data in node_data.items():
            if data.get("error"):
                continue
            server_info = data.get("server_info")
            if not server_info:
                continue
            info = server_info.get("info", {})
            validated = info.get("validated_ledger", {})
            seq = validated.get("seq")
            if isinstance(seq, int) and seq > 0:
                ledger_indices[node_id] = seq

        if len(ledger_indices) < 2:
            return node_data

        # Find the majority ledger
        from collections import Counter

        counts = Counter(ledger_indices.values())
        majority_seq, majority_count = counts.most_common(1)[0]

        # Only proceed if there's a clear majority (>50%)
        if majority_count <= len(ledger_indices) // 2:
            return node_data

        # Find nodes that are exactly 1 behind
        lagging_nodes = [
            node_id
            for node_id, seq in ledger_indices.items()
            if seq == majority_seq - 1
        ]

        if not lagging_nodes:
            return node_data

        # Retry up to 3 times with increasing delays
        delays = [0.1, 0.15, 0.25]
        for delay in delays:
            if not lagging_nodes:
                break

            time.sleep(delay)

            futures = {
                executor.submit(
                    self.rpc_client.get_node_data, node_id, self.tracked_amendment
                ): node_id
                for node_id in lagging_nodes
            }

            still_lagging = []
            for future in as_completed(futures):
                data = future.result()
                node_id = data["node_id"]
                # Only update if the node caught up
                server_info = data.get("server_info")
                if not server_info:
                    still_lagging.append(node_id)
                    continue
                info = server_info.get("info", {})
                validated = info.get("validated_ledger", {})
                new_seq = validated.get("seq")
                if isinstance(new_seq, int) and new_seq >= majority_seq:
                    node_data[node_id] = data
                else:
                    still_lagging.append(node_id)

            lagging_nodes = still_lagging

        return node_data
