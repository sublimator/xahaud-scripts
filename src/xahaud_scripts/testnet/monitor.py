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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import NetworkConfig, NodeInfo
    from xahaud_scripts.testnet.protocols import ProcessManager, RPCClient
    from xahaud_scripts.testnet.websocket import WebSocketClient

logger = make_logger(__name__)
console = Console()


def display_network_status(
    node_data: dict[int, dict[str, Any]],
    node_count: int,
    tracked_amendment: str | None = None,
    ledger_events: dict[int, dict[str, Any]] | None = None,
) -> None:
    """Display network status as a rich table.

    Args:
        node_data: Dict mapping node_id -> node data from get_node_data()
        node_count: Number of nodes
        tracked_amendment: Optional amendment ID being tracked
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
) -> None:
    """Display a transaction type histogram for the given ledger.

    Args:
        rpc_client: RPC client for queries
        ledger_index: Ledger index to query
    """
    result = rpc_client.ledger(0, ledger_index=ledger_index, transactions=True)
    if not result:
        return

    ledger = result.get("ledger", {})
    transactions = ledger.get("transactions", [])

    if not transactions:
        return

    # Count transaction types
    type_counts: dict[str, int] = {}
    for tx in transactions:
        tx_type = tx.get("TransactionType", "Unknown")
        type_counts[tx_type] = type_counts.get(tx_type, 0) + 1

    # Sort by count (descending), then by name
    sorted_types = sorted(type_counts.items(), key=lambda x: (-x[1], x[0]))

    # Display as compact histogram
    parts = [f"[cyan]{name}[/cyan]:{count}" for name, count in sorted_types]
    console.print(f"[dim]Txns:[/dim] {' '.join(parts)}")


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
    """Async network monitoring with event-driven updates.

    Attributes:
        rpc_client: RPC client for queries
        ws_client: WebSocket client for ledger streaming
        network_config: Network configuration
        tracked_amendment: Optional amendment ID to track
    """

    def __init__(
        self,
        rpc_client: RPCClient,
        ws_client: WebSocketClient,
        network_config: NetworkConfig,
        tracked_amendment: str | None = None,
    ) -> None:
        """Initialize the network monitor.

        Args:
            rpc_client: RPC client for queries
            ws_client: WebSocket client for ledger streaming
            network_config: Network configuration
            tracked_amendment: Optional amendment ID to track
        """
        self.rpc_client = rpc_client
        self.ws_client = ws_client
        self.network_config = network_config
        self.tracked_amendment = tracked_amendment

    async def monitor(self) -> None:
        """Run the monitoring loop.

        Polls initially, then switches to event-driven updates
        once the first ledger close is detected.
        """
        node_count = self.network_config.node_count
        last_ledger_index = 0

        try:
            # Wait for nodes to start
            connected = False
            while not connected:
                try:
                    connected = await self.ws_client.check_connection(0)
                    if not connected:
                        console.print("[yellow]Waiting for nodes to start...[/yellow]")
                        await asyncio.sleep(5)
                except Exception:
                    await asyncio.sleep(5)

            console.print(
                "[green]Connected to network, monitoring ledger closes...[/green]\n"
            )

            # Initial polling phase
            console.print(
                "[yellow]Waiting for first ledger close (polling every 3s)...[/yellow]"
            )
            first_ledger_received = False

            while not first_ledger_received:
                console.print(
                    f"\n[bold]Network Status - {time.strftime('%H:%M:%S')}[/bold]\n"
                )

                # Fetch node data in parallel
                node_data = self._fetch_all_node_data()
                display_network_status(node_data, node_count, self.tracked_amendment)

                # Check if we've gotten past genesis
                for data in node_data.values():
                    server_info = data.get("server_info")
                    if server_info and "info" in server_info:
                        validated = server_info["info"].get("validated_ledger", {})
                        ledger_seq = validated.get("seq", 0)
                        if isinstance(ledger_seq, int) and ledger_seq > 1:
                            first_ledger_received = True
                            last_ledger_index = ledger_seq
                            break

                if not first_ledger_received:
                    await asyncio.sleep(3)
                else:
                    console.print(
                        f"[green]First ledger close detected (index {last_ledger_index}), "
                        "switching to event-driven updates[/green]\n"
                    )

            # Event-driven monitoring phase
            last_ledger_events: dict[int, dict[str, Any]] | None = None

            while True:
                # Wait for next ledger close first
                next_ledger_index = last_ledger_index + 1
                ledger_events = await self.ws_client.wait_for_all_nodes_ledger_close(
                    node_count, next_ledger_index, timeout=10.0
                )

                if ledger_events:
                    # Debug: dump ledgerClosed event
                    if os.environ.get("DEBUG") == "1":
                        first_event = next(iter(ledger_events.values()), {})
                        console.print("\n[dim]DEBUG: ledgerClosed event:[/dim]")
                        console.print(json.dumps(first_event, indent=2))

                    max_index = max(
                        event.get("ledger_index", 0) for event in ledger_events.values()
                    )
                    last_ledger_index = max_index
                    last_ledger_events = ledger_events
                else:
                    console.print(
                        "[yellow]No ledger close events received, polling...[/yellow]"
                    )
                    await asyncio.sleep(5)
                    continue

                # Display status after receiving events
                console.print(
                    f"\n[bold]Network Status - {time.strftime('%H:%M:%S')}[/bold]\n"
                )

                node_data = self._fetch_all_node_data()
                display_network_status(
                    node_data, node_count, self.tracked_amendment, last_ledger_events
                )
                display_txn_histogram(self.rpc_client, last_ledger_index)

        except KeyboardInterrupt:
            console.print("\n\n[bold yellow]Monitoring stopped by user[/bold yellow]")

    def _fetch_all_node_data(self) -> dict[int, dict[str, Any]]:
        """Fetch data from all nodes in parallel.

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

        return node_data
