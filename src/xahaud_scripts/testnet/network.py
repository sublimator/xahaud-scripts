"""TestNetwork orchestrator class.

This module provides the main TestNetwork class that coordinates
all testnet operations including generation, launching, monitoring,
and teardown.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
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
        process_manager: ProcessManager,
    ) -> None:
        """Initialize the TestNetwork.

        Args:
            base_dir: Directory for generated configs and data
            network_config: Network configuration (ports, node count, etc.)
            launcher: Launcher implementation for starting nodes
            rpc_client: RPC client for node queries
            process_manager: Process manager for teardown
        """
        self._base_dir = base_dir
        self._config = network_config
        self._launcher = launcher
        self._rpc = rpc_client
        self._process_mgr = process_manager
        self._nodes: list[NodeInfo] = []
        self._rc_specs: list[str] = []
        self._launch_state: dict[str, Any] = {}
        self._start_time: float | None = None

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

    @property
    def rc_specs(self) -> list[str]:
        """Get runtime config specs (from generate or network.json)."""
        return list(self._rc_specs)

    @property
    def start_time(self) -> float | None:
        """Get network launch timestamp (epoch seconds)."""
        return self._start_time

    def generate(
        self,
        log_levels: dict[str, str] | None = None,
        find_ports: bool = False,
        rc_specs: list[str] | None = None,
    ) -> None:
        """Generate all node configurations.

        This creates:
        - Validator keys for each node
        - xahaud.cfg for each node
        - validators.txt for each node
        - network.json with network metadata

        Args:
            log_levels: Optional log level overrides (partition -> severity)
            find_ports: If True, auto-find free ports if defaults are in use.
                        If False (default), error if any port is in use.
        """
        logger.info(f"Generating configs for {self._config.node_count} nodes")

        # Clean previous configs
        self.clean()

        # Generate all configs (may adjust ports to avoid conflicts if find_ports=True)
        self._nodes, self._config = generate_all_configs(
            base_dir=self._base_dir,
            network_config=self._config,
            key_generator=ValidatorKeysGenerator(),
            log_levels=log_levels,
            process_manager=self._process_mgr,
            find_ports=find_ports,
        )

        # Store runtime config specs
        self._rc_specs = rc_specs or []

        # Save network.json
        self._save_network_info()

        logger.info(f"Generated configs for {len(self._nodes)} nodes")
        logger.info(f"  Network ID: {self._config.network_id}")
        logger.info(
            f"  Ports: peer={self._config.base_port_peer}+, "
            f"rpc={self._config.base_port_rpc}+, "
            f"ws={self._config.base_port_ws}+"
        )

    def check_ports(self) -> dict[int, list[dict[str, str]]]:
        """Check if any required ports are in use.

        Returns:
            Dict mapping port -> list of connections for ports in use
        """
        # Collect all ports we'll need
        ports = []
        for i in range(self._config.node_count):
            ports.extend(
                [
                    self._config.port_peer(i),
                    self._config.port_rpc(i),
                    self._config.port_ws(i),
                ]
            )

        return self._process_mgr.check_ports_free(ports)

    def run(self, launch_config: LaunchConfig) -> None:
        """Launch all nodes and start monitoring.

        Args:
            launch_config: Launch configuration with paths and flags
        """
        # Load network info if not already loaded
        if not self._nodes:
            self._load_network_info()

        # Wait for ports to become free before launching
        max_wait = 30  # seconds
        wait_interval = 2  # seconds
        waited = 0
        killed_pids: set[int] = set()
        # Processes to ignore (monitoring tools, not actually using ports)
        ignored_processes = {"peermon"}

        def filter_ports(
            ports: dict[int, list[dict[str, str]]],
        ) -> dict[int, list[dict[str, str]]]:
            """Filter out ignored processes from port check results."""
            result: dict[int, list[dict[str, str]]] = {}
            for port, conns in ports.items():
                filtered = [c for c in conns if c["process"] not in ignored_processes]
                if filtered:
                    result[port] = filtered
            return result

        while waited < max_wait:
            ports_in_use = filter_ports(self.check_ports())
            if not ports_in_use:
                break

            if waited == 0:
                logger.warning("Waiting for ports to become free...")

            for port, connections in sorted(ports_in_use.items()):
                for conn in connections:
                    state = conn["state"]
                    pid = int(conn["pid"])
                    process_name = conn["process"]

                    logger.warning(
                        f"  Port {port}: {process_name} (PID {pid}, {state})"
                    )
                    # Kill processes that are actively using ports (not TIME_WAIT)
                    killable = ("LISTEN", "ESTABLISHED", "CLOSE_WAIT", "CLOSED")
                    if state in killable and pid not in killed_pids:
                        logger.warning(f"  Killing PID {pid}...")
                        self._process_mgr.kill(pid)
                        killed_pids.add(pid)

            time.sleep(wait_interval)
            waited += wait_interval

        if ports_in_use:
            # Check if remaining are just TIME_WAIT (can't do anything about those)
            only_time_wait = all(
                conn["state"] == "TIME_WAIT"
                for conns in ports_in_use.values()
                for conn in conns
            )
            if only_time_wait:
                logger.warning(
                    f"Ports still in TIME_WAIT after {max_wait}s. "
                    "Proceeding anyway (may fail to bind)."
                )
            else:
                logger.error(
                    f"Ports still in use after {max_wait}s. "
                    "Try 'x-testnet teardown' or wait longer."
                )
                return

        logger.info("Launching test network...")

        for i, node in enumerate(self._nodes):
            logger.info(f"  Launching Node {node.id}")

            success = self._launcher.launch(node, launch_config)
            if not success:
                logger.error(f"Failed to launch node {node.id}")
                continue

            # Delay between node launches
            if not launch_config.no_delays:
                if i == 0:
                    logger.info(
                        f"  Waiting {launch_config.slave_delay} seconds "
                        "for first node to initialize..."
                    )
                    time.sleep(launch_config.slave_delay)
                elif i < len(self._nodes) - 1:
                    logger.info(
                        f"  Waiting {launch_config.slave_delay} seconds "
                        "before launching next node..."
                    )
                    time.sleep(launch_config.slave_delay)

        # Finalize launcher (e.g., attach to tmux session)
        self._launcher.finalize()

        # Record launch time and persist launch state
        self._start_time = time.time()

        from xahaud_scripts.testnet.protocols import ControllableLauncher

        if isinstance(self._launcher, ControllableLauncher):
            self._launch_state = self._launcher.launch_state

        self._save_network_info()

        logger.info("Network launched!")
        logger.info(f"  Node 0 RPC: http://127.0.0.1:{self._config.base_port_rpc}")
        logger.info(f"  Node 0 WS:  ws://127.0.0.1:{self._config.base_port_ws}")

        # Dump effective env vars so it's obvious what flags are active
        self._dump_launch_env(launch_config)

    def _dump_launch_env(self, launch_config: LaunchConfig) -> None:
        """Log effective environment variables for visibility."""
        logger.info("  Environment:")

        # Global extra env vars (--env NAME=VALUE)
        for key, value in sorted(launch_config.extra_env.items()):
            logger.info(f"    {key}={value}")

        # Node-specific env vars (--env n0:NAME=VALUE)
        for node_id in sorted(launch_config.node_env):
            for key, value in sorted(launch_config.node_env[node_id].items()):
                logger.info(f"    n{node_id}: {key}={value}")

        # Startup flags
        if launch_config.quorum is not None:
            logger.info(f"    --quorum {launch_config.quorum}")
        if launch_config.extra_args:
            logger.info(f"    extra args: {' '.join(launch_config.extra_args)}")

    def monitor(
        self,
        tracked_features: list[str] | None = None,
        stop_after_first_ledger: bool = False,
        teardown_on_exit: bool = True,
    ) -> int:
        """Start the monitoring loop.

        Args:
            tracked_features: Optional list of feature names to track
            stop_after_first_ledger: If True, stop after first ledger closes
            teardown_on_exit: If True, kill nodes on Ctrl+C. If False, just
                detach (nodes keep running).

        Returns:
            Ledger index when stopped (0 if failed)
        """
        # Load network info if not already loaded
        if not self._nodes:
            self._load_network_info()

        monitor = NetworkMonitor(
            rpc_client=self._rpc,
            network_config=self._config,
            tracked_features=tracked_features,
            base_dir=self._base_dir,
            start_time=self._start_time,
        )

        if stop_after_first_ledger:
            logger.info("Waiting for first ledger...")
            return asyncio.run(monitor.monitor(stop_after_first_ledger=True))

        logger.info("Starting monitoring loop (Ctrl+C to stop)...")
        try:
            asyncio.run(monitor.monitor())
            return 0
        except KeyboardInterrupt:
            if teardown_on_exit:
                logger.info("Monitoring stopped by user")
                # Shutdown nodes via launcher (kills processes + closes window)
                killed = self._launcher.shutdown(self._base_dir, self._process_mgr)
                if killed:
                    logger.info(f"Killed {killed} rippled processes")
            else:
                logger.info("Monitor detached (network still running)")
            return 0

    def teardown(self, *, keep_dirs: bool = False) -> int:
        """Kill all running test network processes and optionally clean up.

        Args:
            keep_dirs: If True, kill processes but keep node directories/logs.

        Returns:
            Number of processes killed
        """
        logger.info(f"Tearing down test network (base_dir: {self._base_dir})...")

        # Delegate to launcher for process killing + window closing
        killed = self._launcher.shutdown(self._base_dir, self._process_mgr)

        if killed > 0:
            logger.info(f"Killed {killed} rippled processes")
        else:
            logger.info("No rippled processes found for this test network")

        if keep_dirs:
            return killed

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

    def ping(self, node_id: int) -> dict[str, Any] | None:
        """Send ping to a specific node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Returns:
            The ping result, or None if query failed
        """
        return self._rpc.ping(node_id)

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
            if self._rpc.log_level(nid, partition, severity):
                logger.info(f"  Node {nid}: Log level set successfully")
            else:
                logger.warning(f"  Node {nid}: Failed to set log level")

    def _save_network_info(self) -> None:
        """Save network.json with network metadata."""
        network_info: dict[str, Any] = {
            "network_id": self._config.network_id,
            "node_count": self._config.node_count,
            "validators": self._config.validators,
            "base_port_peer": self._config.base_port_peer,
            "base_port_rpc": self._config.base_port_rpc,
            "base_port_ws": self._config.base_port_ws,
            "nodes": [
                {
                    "id": node.id,
                    "public_key": node.public_key,
                    "token": node.token,
                    "config": str(node.config_path),
                    "port_peer": node.port_peer,
                    "port_rpc": node.port_rpc,
                    "port_ws": node.port_ws,
                }
                for node in self._nodes
            ],
        }

        # Persist runtime config specs if any
        rc_specs = getattr(self, "_rc_specs", [])
        if rc_specs:
            network_info["runtime_config"] = rc_specs

        # Persist launch time
        if self._start_time is not None:
            network_info["start_time"] = self._start_time

        # Persist launch state if any
        if self._launch_state:
            network_info["launch_state"] = self._launch_state

        network_file = self._base_dir / "network.json"
        self._base_dir.mkdir(parents=True, exist_ok=True)

        with open(network_file, "w") as f:
            json.dump(network_info, f, indent=2)

        logger.debug(f"Saved network.json to {network_file}")

    def _load_network_info(self) -> None:
        """Load network.json and populate nodes list."""
        from xahaud_scripts.testnet.config import NetworkConfig

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
            )
            for node in network_info["nodes"]
        ]

        # Update config with saved base ports (may differ from defaults if ports were adjusted)
        if "base_port_peer" in network_info:
            self._config = NetworkConfig(
                network_id=network_info.get("network_id", self._config.network_id),
                node_count=network_info.get("node_count", len(self._nodes)),
                validators=network_info.get("validators"),
                base_port_peer=network_info["base_port_peer"],
                base_port_rpc=network_info["base_port_rpc"],
                base_port_ws=network_info["base_port_ws"],
            )
            # Update RPC client to use the correct ports
            self._rpc.base_port_rpc = network_info["base_port_rpc"]

        # Load runtime config specs if present
        self._rc_specs = network_info.get("runtime_config", [])

        # Load launch time
        self._start_time = network_info.get("start_time")

        # Load and restore launch state
        self._launch_state = network_info.get("launch_state", {})
        if self._launch_state:
            from xahaud_scripts.testnet.protocols import ControllableLauncher

            saved_type = self._launch_state.get("launcher")
            if isinstance(self._launcher, ControllableLauncher):
                self._launcher.load_launch_state(self._launch_state)
            elif saved_type:
                logger.warning(
                    f"Network was launched with '{saved_type}' launcher but "
                    f"current launcher does not support per-node control"
                )

        logger.info(f"Loaded {len(self._nodes)} nodes from network.json")
        for node in self._nodes:
            logger.info(f"  Node {node.id}: config={node.config_path}")
        if self._rc_specs:
            logger.info(f"  Runtime config specs: {len(self._rc_specs)}")
            for spec in self._rc_specs:
                logger.info(f"    {spec}")

    def _get_node(self, node_id: int) -> NodeInfo | None:
        """Get a node by ID, or None if not found."""
        for node in self._nodes:
            if node.id == node_id:
                return node
        return None

    def _ensure_controllable(self) -> None:
        """Load network info and validate launcher supports lifecycle control."""
        from xahaud_scripts.testnet.protocols import ControllableLauncher

        if not self._nodes:
            self._load_network_info()
        if not isinstance(self._launcher, ControllableLauncher):
            raise RuntimeError("Launcher does not support per-node control")
        if not self._launcher.is_session_alive():
            raise RuntimeError("Launcher session not found. Is the network running?")

    def stop_nodes(self, node_ids: list[int]) -> dict[int, bool]:
        """Stop specific nodes by sending Ctrl+C to their panes.

        Args:
            node_ids: List of node IDs to stop

        Returns:
            Dict mapping node_id -> success
        """
        from xahaud_scripts.testnet.protocols import ControllableLauncher

        self._ensure_controllable()
        assert isinstance(self._launcher, ControllableLauncher)

        results = {}
        for nid in node_ids:
            node = self._get_node(nid)
            if node is None:
                logger.warning(f"Unknown node: n{nid}")
                results[nid] = False
                continue
            if not self._process_mgr.is_port_listening(node.port_rpc):
                logger.warning(
                    f"Node {nid} may already be stopped "
                    f"(port {node.port_rpc} not listening)"
                )
            results[nid] = self._launcher.stop_node(nid)
        return results

    def start_nodes(self, node_ids: list[int]) -> dict[int, bool]:
        """Start specific nodes by re-sending their launch commands.

        Args:
            node_ids: List of node IDs to start

        Returns:
            Dict mapping node_id -> success
        """
        from xahaud_scripts.testnet.protocols import ControllableLauncher

        self._ensure_controllable()
        assert isinstance(self._launcher, ControllableLauncher)

        if not self._launch_state.get("launch_commands"):
            raise RuntimeError("No launch commands found. Re-run 'x-testnet run'.")

        results = {}
        for nid in node_ids:
            node = self._get_node(nid)
            if node is None:
                logger.warning(f"Unknown node: n{nid}")
                results[nid] = False
                continue
            cmd = self._launch_state["launch_commands"].get(str(nid))
            if not cmd:
                logger.error(f"No saved command for node {nid}")
                results[nid] = False
                continue
            if self._process_mgr.is_port_listening(node.port_rpc):
                logger.warning(
                    f"Node {nid} may already be running "
                    f"(port {node.port_rpc} listening)"
                )
            results[nid] = self._launcher.start_node(nid, cmd)
        return results

    def restart_nodes(self, node_ids: list[int], delay: float = 0) -> dict[int, bool]:
        """Restart specific nodes (stop, optional delay, start).

        Args:
            node_ids: List of node IDs to restart
            delay: Seconds to wait between stop and start

        Returns:
            Dict mapping node_id -> success (True only if both stop and start succeeded)
        """
        stop_results = self.stop_nodes(node_ids)
        if delay > 0:
            time.sleep(delay)
        else:
            time.sleep(0.5)  # brief pause for process cleanup
        start_results = self.start_nodes(node_ids)
        return {
            nid: stop_results.get(nid, False) and start_results.get(nid, False)
            for nid in node_ids
        }

    def get_exit_status(self, node_id: int) -> int | None:
        """Get the exit status of a stopped node's process.

        Returns:
            Exit code, or None if node is still alive or not found.
        """
        from xahaud_scripts.testnet.protocols import ControllableLauncher

        self._ensure_controllable()
        assert isinstance(self._launcher, ControllableLauncher)
        return self._launcher.get_exit_status(node_id)

    def capture_output(self, node_id: int, lines: int = 1000) -> str | None:
        """Capture terminal output from a node.

        Args:
            node_id: Node ID to capture from
            lines: Number of lines of scrollback

        Returns:
            Captured text, or None if capture failed
        """
        from xahaud_scripts.testnet.protocols import ControllableLauncher

        self._ensure_controllable()
        assert isinstance(self._launcher, ControllableLauncher)
        return self._launcher.capture_output(node_id, lines)

    def snapshot(self, name: str | None = None, keep_db: bool = False) -> Path:
        """Copy the network directory into output/snapshots/YYYYMMDD-HHMMSS[-name]/.

        Copies the entire testnet dir verbatim (excluding output/) so that
        all existing tooling (logs-search, etc.) works against the snapshot
        with the same directory structure.

        Args:
            name: Optional label for the snapshot
            keep_db: If True, include db/ directories (large). Default: exclude.

        Returns:
            Path to the snapshot directory
        """
        if not self._nodes:
            self._load_network_info()

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        dir_name = f"{timestamp}-{name}" if name else timestamp
        snap_base = self._base_dir.parent / ".testnet" / "output" / "snapshots"
        snapshot_dir = snap_base / dir_name

        exclude: list[str] = []
        if not keep_db:
            exclude.append("db")

        logger.info(f"Snapshotting {self._base_dir} -> {snapshot_dir}")

        shutil.copytree(
            self._base_dir,
            snapshot_dir,
            ignore=shutil.ignore_patterns(*exclude),
        )

        # Write metadata.json at snapshot root
        metadata: dict[str, Any] = {
            "timestamp": timestamp,
            "source_dir": str(self._base_dir),
            "label": name,
            "node_count": len(self._nodes),
        }

        # Add git info if available
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            commit = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            metadata["git_branch"] = branch
            metadata["git_commit"] = commit
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        with open(snapshot_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        logger.info(f"Snapshot saved to {snapshot_dir}")
        return snapshot_dir
