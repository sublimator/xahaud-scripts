"""TestNetwork orchestrator class.

This module provides the main TestNetwork class that coordinates
all testnet operations including generation, launching, monitoring,
and teardown.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xahaud_scripts.testnet.config import NodeInfo
from xahaud_scripts.testnet.generator import (
    ValidatorKeysGenerator,
    generate_all_configs,
)
from xahaud_scripts.testnet.monitor import NetworkMonitor
from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import LaunchConfig, NetworkConfig
    from xahaud_scripts.testnet.protocols import (
        Launcher,
        ProcessManager,
        RPCClient,
    )
    from xahaud_scripts.testnet.websocket import WebSocketClient

logger = make_logger(__name__)


class TestNetwork:
    """Orchestrates test network operations with dependency injection.

    This class coordinates:
    - Configuration generation (validator keys, node configs)
    - Node launching (via pluggable launcher)
    - Network monitoring (via RPC and WebSocket)
    - Process management (teardown, port checking)

    All external dependencies are injected, making this class
    fully testable without launching actual nodes.
    """

    def __init__(
        self,
        base_dir: Path,
        network_config: NetworkConfig,
        launcher: Launcher,
        rpc_client: RPCClient,
        ws_client: WebSocketClient,
        process_manager: ProcessManager,
    ) -> None:
        """Initialize the TestNetwork.

        Args:
            base_dir: Directory for generated configs and data
            network_config: Network configuration (ports, node count, etc.)
            launcher: Launcher implementation for starting nodes
            rpc_client: RPC client for node queries
            ws_client: WebSocket client for ledger streaming
            process_manager: Process manager for teardown
        """
        self._base_dir = base_dir
        self._config = network_config
        self._launcher = launcher
        self._rpc = rpc_client
        self._ws = ws_client
        self._process_mgr = process_manager
        self._nodes: list[NodeInfo] = []

    @property
    def nodes(self) -> list[NodeInfo]:
        """Get list of configured nodes."""
        return self._nodes.copy()

    @property
    def config(self) -> NetworkConfig:
        """Get network configuration."""
        return self._config

    @property
    def rpc_client(self) -> RPCClient:
        """Get RPC client for direct queries."""
        return self._rpc

    @property
    def base_dir(self) -> Path:
        """Get base directory."""
        return self._base_dir

    def generate(self) -> None:
        """Generate all node configurations.

        This creates:
        - Validator keys for each node
        - xahaud.cfg for each node
        - validators.txt for each node
        - network.json with network metadata
        """
        logger.info(f"Generating configs for {self._config.node_count} nodes")

        # Clean previous configs
        self.clean()

        # Generate all configs
        self._nodes = generate_all_configs(
            base_dir=self._base_dir,
            network_config=self._config,
            key_generator=ValidatorKeysGenerator(),
        )

        # Save network.json
        self._save_network_info()

        logger.info(f"Generated configs for {len(self._nodes)} nodes")
        logger.info(f"  Network ID: {self._config.network_id}")
        logger.info("  Node 0: EXPLOIT INJECTOR")
        logger.info(f"  Nodes 1-{self._config.node_count - 1}: Clean validators")

    def run(self, launch_config: LaunchConfig) -> None:
        """Launch all nodes and start monitoring.

        Args:
            launch_config: Launch configuration with paths and flags
        """
        # Load network info if not already loaded
        if not self._nodes:
            self._load_network_info()

        logger.info("Launching test network...")

        for i, node in enumerate(self._nodes):
            role = "EXPLOIT" if node.is_injector else "CLEAN"
            logger.info(f"  Launching Node {node.id} [{role}]")

            success = self._launcher.launch(node, launch_config)
            if not success:
                logger.error(f"Failed to launch node {node.id}")
                continue

            # Delay between node launches
            if not launch_config.no_delays:
                if i == 0:
                    logger.info("  Waiting 10 seconds for first node to initialize...")
                    time.sleep(10)
                elif i < len(self._nodes) - 1:
                    logger.info(
                        f"  Waiting {launch_config.slave_delay} seconds "
                        "before launching next node..."
                    )
                    time.sleep(launch_config.slave_delay)

        # Finalize launcher (e.g., attach to tmux session)
        self._launcher.finalize()

        logger.info("Network launched!")
        logger.info(f"  Node 0 RPC: http://127.0.0.1:{self._config.base_port_rpc}")
        logger.info(f"  Node 0 WS:  ws://127.0.0.1:{self._config.base_port_ws}")

    def monitor(self, tracked_amendment: str | None = None) -> None:
        """Start the monitoring loop.

        Args:
            tracked_amendment: Optional amendment ID to track
        """
        # Load network info if not already loaded
        if not self._nodes:
            self._load_network_info()

        monitor = NetworkMonitor(
            rpc_client=self._rpc,
            ws_client=self._ws,
            network_config=self._config,
            tracked_amendment=tracked_amendment,
        )

        logger.info("Starting monitoring loop (Ctrl+C to stop)...")
        asyncio.run(monitor.monitor())

    def teardown(self) -> int:
        """Kill all running test network processes.

        Returns:
            Number of processes killed
        """
        # Load network info if not already loaded
        if not self._nodes:
            try:
                self._load_network_info()
            except FileNotFoundError:
                logger.warning("No network.json found, using default node count")
                # Create minimal node list for pattern matching
                for i in range(self._config.node_count):
                    config_path = self._base_dir / f"n{i}" / "xahaud.cfg"
                    self._nodes.append(
                        NodeInfo(
                            id=i,
                            public_key="",
                            token="",
                            config_path=config_path,
                            port_peer=self._config.port_peer(i),
                            port_rpc=self._config.port_rpc(i),
                            port_ws=self._config.port_ws(i),
                            is_injector=(i == 0),
                        )
                    )

        logger.info(
            f"Tearing down test network processes (base_dir: {self._base_dir})..."
        )
        logger.info(f"Looking for {len(self._nodes)} nodes")

        killed = 0
        for node in self._nodes:
            pattern = f"rippled.*--conf {node.config_path}"
            logger.info(f"  Node {node.id}: searching for pattern: {pattern}")
            pids = self._process_mgr.find_by_pattern(pattern)

            if not pids:
                logger.info(f"  Node {node.id}: no processes found")
            else:
                logger.info(f"  Node {node.id}: found {len(pids)} process(es): {pids}")

            for pid in pids:
                if self._process_mgr.kill(pid):
                    logger.info(f"  Node {node.id}: killed PID {pid}")
                    killed += 1
                else:
                    logger.warning(f"  Node {node.id}: failed to kill PID {pid}")

        if killed > 0:
            logger.info(f"Killed {killed} rippled processes")
        else:
            logger.info("No rippled processes found for this test network")

        # Clean up generated files
        logger.info("Cleaning up generated files...")
        removed_dirs = 0
        for i in range(self._config.node_count):
            node_dir = self._base_dir / f"n{i}"
            if node_dir.exists():
                shutil.rmtree(node_dir)
                logger.info(f"  Removed {node_dir}")
                removed_dirs += 1

        network_file = self._base_dir / "network.json"
        if network_file.exists():
            network_file.unlink()
            logger.info(f"  Removed {network_file}")

        if removed_dirs > 0:
            logger.info(f"Cleaned up {removed_dirs} node directories")
        else:
            logger.info("No node directories to clean up")

        self._nodes = []

        return killed

    def clean(self) -> None:
        """Remove all generated files."""
        logger.info("Cleaning up...")

        for i in range(self._config.node_count):
            node_dir = self._base_dir / f"n{i}"
            if node_dir.exists():
                shutil.rmtree(node_dir)
                logger.debug(f"  Removed {node_dir}")

        network_file = self._base_dir / "network.json"
        if network_file.exists():
            network_file.unlink()
            logger.debug(f"  Removed {network_file}")

        self._nodes = []
        logger.info("Cleanup complete")

    def server_info(self, node_id: int) -> dict[str, Any] | None:
        """Get server_info from a specific node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Returns:
            The server_info result, or None if query failed
        """
        return self._rpc.server_info(node_id)

    def ping(self, node_id: int, inject: bool = False) -> dict[str, Any] | None:
        """Send ping to a specific node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            inject: If True, include inject flag

        Returns:
            The ping result, or None if query failed
        """
        return self._rpc.ping(node_id, inject=inject)

    def inject_amendment(
        self,
        node_id: int,
        amendment_id: str,
        ledger_seq: int,
    ) -> dict[str, Any]:
        """Inject an EnableAmendment pseudo-transaction.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            amendment_id: Amendment ID to enable
            ledger_seq: Ledger sequence for the transaction

        Returns:
            The inject result dict
        """
        from xrpl.core import binarycodec

        # Build EnableAmendment pseudo-transaction
        pseudo_tx = {
            "TransactionType": "EnableAmendment",
            "Account": "rrrrrrrrrrrrrrrrrrrrrhoLvTp",  # All zeros account
            "Sequence": 0,
            "Fee": "0",
            "SigningPubKey": "",
            "LedgerSequence": ledger_seq,
            "Amendment": amendment_id.upper(),
        }

        # Encode to binary
        tx_blob = binarycodec.encode(pseudo_tx)
        if isinstance(tx_blob, bytes):
            tx_blob = tx_blob.hex().upper()
        else:
            tx_blob = tx_blob.upper()

        logger.info(f"Injecting EnableAmendment on node {node_id}")
        logger.debug(f"  Amendment: {amendment_id}")
        logger.debug(f"  Ledger seq: {ledger_seq}")
        logger.debug(f"  tx_blob: {tx_blob}")

        return self._rpc.inject(node_id, tx_blob)

    def set_log_level(
        self,
        partition: str,
        severity: str,
        node_id: int | None = None,
    ) -> None:
        """Set log level for a partition on one or all nodes.

        Args:
            partition: Log partition name (e.g., "Validations")
            severity: Log severity (e.g., "trace", "debug", "info")
            node_id: Specific node ID, or None for all nodes
        """
        node_ids = (
            [node_id] if node_id is not None else list(range(self._config.node_count))
        )

        logger.info(f"Setting log level: partition={partition}, severity={severity}")

        for nid in node_ids:
            role = "EXPLOIT" if nid == 0 else "CLEAN"
            if self._rpc.log_level(nid, partition, severity):
                logger.info(f"  Node {nid} [{role}]: Log level set successfully")
            else:
                logger.warning(f"  Node {nid} [{role}]: Failed to set log level")

    def _save_network_info(self) -> None:
        """Save network.json with network metadata."""
        network_info = {
            "network_id": self._config.network_id,
            "node_count": self._config.node_count,
            "nodes": [
                {
                    "id": node.id,
                    "public_key": node.public_key,
                    "token": node.token,
                    "config": str(node.config_path),
                    "port_peer": node.port_peer,
                    "port_rpc": node.port_rpc,
                    "port_ws": node.port_ws,
                    "is_injector": node.is_injector,
                }
                for node in self._nodes
            ],
        }

        network_file = self._base_dir / "network.json"
        self._base_dir.mkdir(parents=True, exist_ok=True)

        with open(network_file, "w") as f:
            json.dump(network_info, f, indent=2)

        logger.debug(f"Saved network.json to {network_file}")

    def _load_network_info(self) -> None:
        """Load network.json and populate nodes list."""
        network_file = self._base_dir / "network.json"
        logger.info(f"Loading network info from: {network_file}")

        if not network_file.exists():
            raise FileNotFoundError(
                f"Network not generated. Run 'testnet generate' first.\n"
                f"Expected: {network_file}"
            )

        with open(network_file) as f:
            network_info = json.load(f)

        self._nodes = [
            NodeInfo(
                id=node["id"],
                public_key=node["public_key"],
                token=node["token"],
                config_path=Path(node["config"]),
                port_peer=node["port_peer"],
                port_rpc=node["port_rpc"],
                port_ws=node["port_ws"],
                is_injector=node.get("is_injector", node["id"] == 0),
            )
            for node in network_info["nodes"]
        ]

        logger.info(f"Loaded {len(self._nodes)} nodes from network.json")
        for node in self._nodes:
            logger.info(f"  Node {node.id}: config={node.config_path}")
