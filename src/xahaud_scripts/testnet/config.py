"""Configuration dataclasses and builder for testnet.

This module provides:
- NetworkConfig: Network-wide settings (ports, network ID, node count)
- LaunchConfig: Settings for launching nodes (paths, flags)
- NodeInfo: Information about a single node
- ConfigBuilder: Fluent builder for creating configurations
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import struct
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
MAX_NODE_COUNT = 20


def get_bundled_genesis_file() -> Path:
    """Get the path to the bundled genesis.json file."""
    return Path(
        str(
            importlib.resources.files("xahaud_scripts.testnet.data").joinpath(
                "genesis.json"
            )
        )
    )


def feature_name_to_hash(name: str) -> str:
    """Convert a feature name to its amendment hash.

    Uses first 256 bits of SHA-512 of the name (uppercase).

    Args:
        name: Feature name (e.g., "RNG")

    Returns:
        64-character hex string (256 bits)
    """
    digest = hashlib.sha512(name.encode("utf-8")).digest()
    return digest[:32].hex().upper()  # First 256 bits = 32 bytes


def _short_skip_index() -> str:
    """Compute the keylet::skip() index for the short LedgerHashes SLE.

    This is sha512Half(uint16_be('s')).
    """
    return hashlib.sha512(struct.pack(">H", ord("s"))).digest()[:32].hex().upper()


def _make_short_skiplist_entry(
    ledger_index: int, prior_hashes: list[str]
) -> dict | None:
    """Build the short LedgerHashes SLE for a synthetic ledger in 1..256.

    Args:
        ledger_index: The starting ledger index (1-256).
        prior_hashes: Hashes of ledgers 1..ledger_index-1, oldest to newest.

    Returns:
        The LedgerHashes entry dict, or None if ledger_index is 1 (no prior hashes).
    """
    expected = ledger_index - 1
    if expected == 0:
        return None

    if len(prior_hashes) != expected:
        raise ValueError(
            f"ledger {ledger_index} needs {expected} prior hashes, "
            f"got {len(prior_hashes)}"
        )

    return {
        "Flags": 0,
        "LedgerEntryType": "LedgerHashes",
        "Hashes": [h.upper() for h in prior_hashes],
        "LastLedgerSequence": ledger_index - 1,
        "index": _short_skip_index(),
    }


def _generate_synthetic_hashes(count: int) -> list[str]:
    """Generate fake prior ledger hashes to satisfy short skip-list lookups.

    These are deliberately fake prehistory — they only exist so that
    hashOfSeq() returns *something* for synthetic starts <= 256.
    This is NOT real ledger history and will not satisfy anything that
    needs actual ancestor ledger data.

    Args:
        count: Number of hashes to generate (for ledgers 1..count).

    Returns:
        List of 64-char hex hash strings.
    """
    hashes = []
    for i in range(1, count + 1):
        h = hashlib.sha512(b"synthetic-ledger-" + str(i).encode()).digest()[:32]
        hashes.append(h.hex().upper())
    return hashes


def _resolve_feature_hash(spec: str) -> str:
    """Resolve a feature spec to its amendment hash.

    Accepts:
        @Name           — name-to-hash via sha512Half
        ABC123... (64)  — raw 64-char hex hash
        Name            — bare name, auto-hashed via sha512Half
    """
    if spec.startswith("@"):
        return feature_name_to_hash(spec[1:])
    if len(spec) == 64 and all(c in "0123456789abcdefABCDEF" for c in spec):
        return spec.upper()
    return feature_name_to_hash(spec)


def resolve_feature_name(spec: str) -> str:
    """Resolve a feature spec to a bare feature name (for RPC calls).

    Strips leading @ if present.
    """
    return spec[1:] if spec.startswith("@") else spec


def _get_or_create_amendments_entry(
    account_state: list[dict],
) -> dict:
    """Find or create the Amendments SLE in accountState."""
    for entry in account_state:
        if entry.get("LedgerEntryType") == "Amendments":
            return entry
    raise ValueError("No Amendments entry found in genesis.json")


def prepare_genesis_file(
    base_genesis: Path,
    features: list[str],
    start_ledger: int | None = None,
    majority_features: list[str] | None = None,
) -> Path:
    """Create a modified genesis.json with custom amendments and/or start ledger.

    Args:
        base_genesis: Path to the base genesis.json file
        features: List of amendment hashes or @names. Prefix with '-' to remove.
                  Use @Name syntax to compute hash from name (e.g., @RNG).
        start_ledger: Optional starting ledger sequence number (1-256).
        majority_features: Optional list of feature names/@hashes to pre-seed
                          in sfMajorities of the Amendments SLE. Uses CloseTime=0
                          so the hold time is already satisfied. Nodes must still
                          vote yes (feature accept) before the voting ledger, or
                          the seeded majority will be cleared via tfLostMajority.

    Returns:
        Path to the (possibly modified) genesis file.
        If no modifications needed, returns base_genesis unchanged.
    """
    if not features and start_ledger is None and not majority_features:
        return base_genesis

    # Load base genesis
    with open(base_genesis) as f:
        genesis = json.load(f)

    account_state = genesis["ledger"]["accountState"]

    # Modify amendments if requested
    if features:
        amendments_entry = _get_or_create_amendments_entry(account_state)
        current_amendments = set(amendments_entry.get("Amendments", []))

        for feature in features:
            remove = feature.startswith("-")
            if remove:
                feature = feature[1:]

            amendment_hash = _resolve_feature_hash(feature)

            if remove:
                current_amendments.discard(amendment_hash)
            else:
                current_amendments.add(amendment_hash)

        amendments_entry["Amendments"] = sorted(current_amendments)

    # Pre-seed sfMajorities in the Amendments SLE.
    # Uses CloseTime=0 (ripple epoch start) so the hold time is already satisfied.
    # Nodes must still vote yes before the voting ledger — if validators don't
    # advertise the amendment, the next amendment round takes the tfLostMajority
    # path and clears the seeded majority. Testnet-only scaffolding.
    if majority_features:
        amendments_entry = _get_or_create_amendments_entry(account_state)
        existing_majorities = amendments_entry.get("Majorities", [])

        # Build set of already-seeded amendment hashes
        seeded = {
            m["Majority"]["Amendment"] for m in existing_majorities if "Majority" in m
        }

        for spec in majority_features:
            amendment_hash = _resolve_feature_hash(spec)
            if amendment_hash not in seeded:
                existing_majorities.append(
                    {"Majority": {"Amendment": amendment_hash, "CloseTime": 0}}
                )

        amendments_entry["Majorities"] = existing_majorities

    # Modify start ledger if requested
    if start_ledger is not None:
        genesis["ledger"]["seqNum"] = str(start_ledger)
        genesis["ledger"]["ledger_index"] = str(start_ledger)

        # Inject fake short skip list so hashOfSeq() works for starts <= 256.
        # loadLedgerFromFile() rebuilds from ledger_index + accountState;
        # wrapper fields like hash/parent_hash are not trusted.
        if start_ledger > 1:
            # Remove any existing LedgerHashes entries
            account_state[:] = [
                e
                for e in account_state
                if not (
                    isinstance(e, dict) and e.get("LedgerEntryType") == "LedgerHashes"
                )
            ]
            prior_hashes = _generate_synthetic_hashes(start_ledger - 1)
            entry = _make_short_skiplist_entry(start_ledger, prior_hashes)
            if entry is not None:
                account_state.append(entry)

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
        validators: Number of nodes on the UNL (default: all).
            Nodes 0..validators-1 are validators; the rest are non-UNL peers.
        base_port_peer: Base port for peer connections (node N uses base + N)
        base_port_rpc: Base port for RPC connections (node N uses base + N)
        base_port_ws: Base port for WebSocket connections (node N uses base + N)
    """

    network_id: int = DEFAULT_NETWORK_ID
    node_count: int = DEFAULT_NODE_COUNT
    validators: int | None = None
    base_port_peer: int = DEFAULT_BASE_PORT_PEER
    base_port_rpc: int = DEFAULT_BASE_PORT_RPC
    base_port_ws: int = DEFAULT_BASE_PORT_WS

    @property
    def validator_count(self) -> int:
        """Number of UNL validators (defaults to node_count)."""
        return self.validators if self.validators is not None else self.node_count

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
        quorum: Optional quorum value for consensus
        no_delays: Skip startup delays between nodes
        slave_delay: Delay in seconds between launching nodes
        extra_args: Additional arguments for rippled
        extra_env: Additional environment variables for rippled (all nodes)
        node_env: Node-specific environment variables (node_id -> {key: value})
        desktop: macOS desktop number to place window on (1-9)
    """

    xahaud_root: Path
    rippled_path: Path
    genesis_file: Path
    quorum: int | None = None
    no_delays: bool = True
    slave_delay: float = 1.0
    extra_args: list[str] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)
    node_env: dict[int, dict[str, str]] = field(default_factory=dict)
    node_rippled_paths: dict[int, Path] = field(default_factory=dict)
    desktop: int | None = None
    lldb_nodes: set[int] = field(default_factory=set)

    def get_rippled_path(self, node_id: int) -> Path:
        """Get the effective binary path for a node."""
        return self.node_rippled_paths.get(node_id, self.rippled_path)


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
    """

    id: int
    public_key: str
    token: str
    config_path: Path
    port_peer: int
    port_rpc: int
    port_ws: int

    @property
    def node_dir(self) -> Path:
        """Get the node's directory."""
        return self.config_path.parent


class ConfigBuilder:
    """Fluent builder for testnet configuration.

    Example:
        >>> builder = ConfigBuilder()
        >>> network_config, launch_config = (
        ...     builder
        ...     .xahaud_root()  # Auto-detect via git
        ...     .node_count(3)
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
        self._quorum: int | None = None
        self._no_delays: bool = True
        self._slave_delay: float = 1.0
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

    def quorum(self, quorum: int | None) -> ConfigBuilder:
        """Set quorum value."""
        self._quorum = quorum
        return self

    def no_delays(self, no_delays: bool = True) -> ConfigBuilder:
        """Skip startup delays between nodes."""
        self._no_delays = no_delays
        return self

    def slave_delay(self, delay: float) -> ConfigBuilder:
        """Set delay between launching nodes."""
        self._slave_delay = delay
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
            quorum=self._quorum,
            no_delays=self._no_delays,
            slave_delay=self._slave_delay,
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
