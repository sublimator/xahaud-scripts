"""Configuration dataclasses and builder for testnet.

This module provides:
- NetworkConfig: Network-wide settings (ports, network ID, node count)
- LaunchConfig: Settings for launching nodes (paths, flags)
- NodeInfo: Information about a single node
- ConfigBuilder: Fluent builder for creating configurations
"""

from __future__ import annotations

import importlib.resources
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Default port bases
DEFAULT_BASE_PORT_PEER = 51235
DEFAULT_BASE_PORT_RPC = 5005
DEFAULT_BASE_PORT_WS = 6005
DEFAULT_NETWORK_ID = 99999
DEFAULT_NODE_COUNT = 5


def get_bundled_genesis_file() -> Path:
    """Get the path to the bundled genesis.json file."""
    return Path(
        str(
            importlib.resources.files("xahaud_scripts.testnet.data").joinpath(
                "genesis.json"
            )
        )
    )


def prepare_genesis_file(
    base_genesis: Path,
    features: list[str],
) -> Path:
    """Create a modified genesis.json with custom amendments.

    Args:
        base_genesis: Path to the base genesis.json file
        features: List of amendment hashes. Prefix with '-' to remove.

    Returns:
        Path to the (possibly modified) genesis file.
        If no features specified, returns base_genesis unchanged.
    """
    if not features:
        return base_genesis

    # Load base genesis
    with open(base_genesis) as f:
        genesis = json.load(f)

    # Find Amendments entry in accountState
    amendments_entry = None
    for entry in genesis["ledger"]["accountState"]:
        if entry.get("LedgerEntryType") == "Amendments":
            amendments_entry = entry
            break

    if amendments_entry is None:
        raise ValueError("No Amendments entry found in genesis.json")

    current_amendments = set(amendments_entry.get("Amendments", []))

    # Process feature modifications
    for feature in features:
        feature = feature.upper()  # Normalize to uppercase
        if feature.startswith("-"):
            # Remove amendment
            hash_to_remove = feature[1:]
            current_amendments.discard(hash_to_remove)
        else:
            # Add amendment
            current_amendments.add(feature)

    # Update amendments array (sorted for deterministic output)
    amendments_entry["Amendments"] = sorted(current_amendments)

    # Write to temp file
    fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="genesis_")
    with os.fdopen(fd, "w") as f:
        json.dump(genesis, f, indent=2)

    return Path(temp_path)


@dataclass
class NetworkConfig:
    """Immutable network-wide configuration.

    Attributes:
        network_id: Network identifier for the test network
        node_count: Number of nodes in the network
        base_port_peer: Base port for peer connections (node N uses base + N)
        base_port_rpc: Base port for RPC connections (node N uses base + N)
        base_port_ws: Base port for WebSocket connections (node N uses base + N)
    """

    network_id: int = DEFAULT_NETWORK_ID
    node_count: int = DEFAULT_NODE_COUNT
    base_port_peer: int = DEFAULT_BASE_PORT_PEER
    base_port_rpc: int = DEFAULT_BASE_PORT_RPC
    base_port_ws: int = DEFAULT_BASE_PORT_WS

    def port_peer(self, node_id: int) -> int:
        """Get peer port for a node."""
        return self.base_port_peer + node_id

    def port_rpc(self, node_id: int) -> int:
        """Get RPC port for a node."""
        return self.base_port_rpc + node_id

    def port_ws(self, node_id: int) -> int:
        """Get WebSocket port for a node."""
        return self.base_port_ws + node_id


@dataclass
class LaunchConfig:
    """Configuration for launching nodes.

    Attributes:
        xahaud_root: Path to the xahaud repository root
        rippled_path: Path to the rippled binary
        genesis_file: Path to the genesis ledger file
        amendment_id: Optional amendment ID for injection
        quorum: Optional quorum value for consensus
        inject_type: Injection type ('rcl' or 'txq')
        flood: Inject every N ledgers (0 for once only)
        n_txns: Number of transactions per injection
        no_delays: Skip startup delays between nodes
        slave_delay: Delay in seconds between launching slave nodes
        slave_net: Add --net flag to slave nodes
        no_check_local: Disable CHECK_LOCAL_PSEUDO env var
        no_check_pseudo_valid: Disable CHECK_PSEUDO_VALIDITY env var
        extra_args: Additional arguments for rippled
    """

    xahaud_root: Path
    rippled_path: Path
    genesis_file: Path
    amendment_id: str | None = None
    quorum: int | None = None
    inject_type: str = "rcl"
    flood: int | None = None
    n_txns: int | None = None
    no_delays: bool = True
    slave_delay: int = 2
    slave_net: bool = False
    no_check_local: bool = False
    no_check_pseudo_valid: bool = False
    extra_args: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NodeInfo:
    """Immutable information about a single node.

    Attributes:
        id: Node ID (0, 1, 2, etc.)
        public_key: Validator public key
        token: Validator token (multi-line string)
        config_path: Path to the node's xahaud.cfg file
        port_peer: Peer port for this node
        port_rpc: RPC port for this node
        port_ws: WebSocket port for this node
        is_injector: True if this node is the exploit injector (node 0)
    """

    id: int
    public_key: str
    token: str
    config_path: Path
    port_peer: int
    port_rpc: int
    port_ws: int
    is_injector: bool = False

    @property
    def node_dir(self) -> Path:
        """Get the node's directory."""
        return self.config_path.parent

    @property
    def role(self) -> str:
        """Get the node's role string."""
        return "EXPLOIT" if self.is_injector else "CLEAN"


class ConfigBuilder:
    """Fluent builder for testnet configuration.

    Example:
        >>> builder = ConfigBuilder()
        >>> network_config, launch_config = (
        ...     builder
        ...     .xahaud_root()  # Auto-detect via git
        ...     .node_count(3)
        ...     .amendment_id("ABC123...")
        ...     .build()
        ... )
    """

    def __init__(self) -> None:
        self._xahaud_root: Path | None = None
        self._rippled_path: Path | None = None
        self._base_dir: Path | None = None
        self._genesis_file: Path | None = None
        self._node_count: int = DEFAULT_NODE_COUNT
        self._network_id: int = DEFAULT_NETWORK_ID
        self._base_port_peer: int = DEFAULT_BASE_PORT_PEER
        self._base_port_rpc: int = DEFAULT_BASE_PORT_RPC
        self._base_port_ws: int = DEFAULT_BASE_PORT_WS
        self._amendment_id: str | None = None
        self._quorum: int | None = None
        self._inject_type: str = "rcl"
        self._flood: int | None = None
        self._n_txns: int | None = None
        self._no_delays: bool = True
        self._slave_delay: int = 2
        self._slave_net: bool = False
        self._no_check_local: bool = False
        self._no_check_pseudo_valid: bool = False
        self._extra_args: list[str] = []

    def xahaud_root(self, path: Path | None = None) -> ConfigBuilder:
        """Set xahaud root. Auto-detects via git if None."""
        if path is None:
            path = Path(
                subprocess.check_output(
                    ["git", "rev-parse", "--show-toplevel"], text=True
                ).strip()
            )
        self._xahaud_root = path
        return self

    def rippled_path(self, path: Path | None = None) -> ConfigBuilder:
        """Set rippled path. Defaults to $root/build/rippled if None."""
        self._rippled_path = path
        return self

    def base_dir(self, path: Path | None = None) -> ConfigBuilder:
        """Set base directory for generated configs."""
        self._base_dir = path
        return self

    def genesis_file(self, path: Path | None = None) -> ConfigBuilder:
        """Set genesis file path."""
        self._genesis_file = path
        return self

    def node_count(self, count: int) -> ConfigBuilder:
        """Set number of nodes."""
        self._node_count = count
        return self

    def network_id(self, network_id: int) -> ConfigBuilder:
        """Set network ID."""
        self._network_id = network_id
        return self

    def ports(
        self,
        peer: int = DEFAULT_BASE_PORT_PEER,
        rpc: int = DEFAULT_BASE_PORT_RPC,
        ws: int = DEFAULT_BASE_PORT_WS,
    ) -> ConfigBuilder:
        """Set base ports."""
        self._base_port_peer = peer
        self._base_port_rpc = rpc
        self._base_port_ws = ws
        return self

    def amendment_id(self, amendment_id: str | None) -> ConfigBuilder:
        """Set amendment ID for injection."""
        self._amendment_id = amendment_id
        return self

    def quorum(self, quorum: int | None) -> ConfigBuilder:
        """Set quorum value."""
        self._quorum = quorum
        return self

    def inject_type(self, inject_type: str) -> ConfigBuilder:
        """Set injection type ('rcl' or 'txq')."""
        self._inject_type = inject_type
        return self

    def flood(self, flood: int | None) -> ConfigBuilder:
        """Set flood frequency (inject every N ledgers)."""
        self._flood = flood
        return self

    def n_txns(self, n_txns: int | None) -> ConfigBuilder:
        """Set number of transactions per injection."""
        self._n_txns = n_txns
        return self

    def no_delays(self, no_delays: bool = True) -> ConfigBuilder:
        """Skip startup delays between nodes."""
        self._no_delays = no_delays
        return self

    def slave_delay(self, delay: int) -> ConfigBuilder:
        """Set delay between launching slave nodes."""
        self._slave_delay = delay
        return self

    def slave_net(self, slave_net: bool = True) -> ConfigBuilder:
        """Add --net flag to slave nodes."""
        self._slave_net = slave_net
        return self

    def no_check_local(self, no_check_local: bool = True) -> ConfigBuilder:
        """Disable CHECK_LOCAL_PSEUDO."""
        self._no_check_local = no_check_local
        return self

    def no_check_pseudo_valid(
        self, no_check_pseudo_valid: bool = True
    ) -> ConfigBuilder:
        """Disable CHECK_PSEUDO_VALIDITY."""
        self._no_check_pseudo_valid = no_check_pseudo_valid
        return self

    def extra_args(self, args: list[str]) -> ConfigBuilder:
        """Set extra arguments for rippled."""
        self._extra_args = args
        return self

    def build(self) -> tuple[NetworkConfig, LaunchConfig]:
        """Build immutable config objects.

        Returns:
            Tuple of (NetworkConfig, LaunchConfig)

        Raises:
            ValueError: If required configuration is missing
        """
        # Auto-detect xahaud_root if not set
        if self._xahaud_root is None:
            self.xahaud_root()

        assert self._xahaud_root is not None  # For type checker

        # Default rippled_path
        if self._rippled_path is None:
            self._rippled_path = self._xahaud_root / "build" / "rippled"

        # Default base_dir
        if self._base_dir is None:
            self._base_dir = self._xahaud_root / "testnet"

        # Default genesis_file (use bundled genesis from package)
        if self._genesis_file is None:
            self._genesis_file = get_bundled_genesis_file()

        network_config = NetworkConfig(
            network_id=self._network_id,
            node_count=self._node_count,
            base_port_peer=self._base_port_peer,
            base_port_rpc=self._base_port_rpc,
            base_port_ws=self._base_port_ws,
        )

        launch_config = LaunchConfig(
            xahaud_root=self._xahaud_root,
            rippled_path=self._rippled_path,
            genesis_file=self._genesis_file,
            amendment_id=self._amendment_id,
            quorum=self._quorum,
            inject_type=self._inject_type,
            flood=self._flood,
            n_txns=self._n_txns,
            no_delays=self._no_delays,
            slave_delay=self._slave_delay,
            slave_net=self._slave_net,
            no_check_local=self._no_check_local,
            no_check_pseudo_valid=self._no_check_pseudo_valid,
            extra_args=self._extra_args,
        )

        return network_config, launch_config

    @property
    def base_dir_path(self) -> Path:
        """Get the base directory path (after building or with defaults)."""
        if self._base_dir is not None:
            return self._base_dir
        if self._xahaud_root is not None:
            return self._xahaud_root / "testnet"
        # Need to auto-detect
        self.xahaud_root()
        assert self._xahaud_root is not None
        return self._xahaud_root / "testnet"
