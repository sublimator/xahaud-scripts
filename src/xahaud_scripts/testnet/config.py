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
# Peer port must be BELOW the ephemeral range (49152-65535 on macOS)
# to avoid collisions with outbound ephemeral ports from any process.
DEFAULT_BASE_PORT_PEER = 21235
DEFAULT_BASE_PORT_RPC = 5005
DEFAULT_BASE_PORT_WS = 6005
DEFAULT_NETWORK_ID = 99999
DEFAULT_NODE_COUNT = 5
MAX_NODE_COUNT = 20

# FLAG_LEDGER_INTERVAL in xahaud: both skip-list SLEs hold at most this many
# hashes, and the long skip list records the hash of every Nth ledger.
SKIP_LIST_INTERVAL = 256


def get_bundled_genesis_file() -> Path:
    """Get the path to the bundled genesis.json file.

    The amendments list is generated from named amendments in
    genesis_amendments.py rather than hardcoded hashes.
    """
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


def _long_skip_index(window: int) -> str:
    """Compute the keylet::skip(ledger) index for a long LedgerHashes SLE.

    This is sha512Half(uint16_be('s'), uint32_be(window)) where
    ``window = ledger >> 16``. The long skip list buckets the hash of every
    256th ledger into one SLE per 65536-ledger window.
    """
    key = struct.pack(">H", ord("s")) + struct.pack(">I", window)
    return hashlib.sha512(key).digest()[:32].hex().upper()


def _unl_report_index() -> str:
    """Compute the keylet::UNLReport() index.

    This is sha512Half(uint16_be('R')).
    """
    return hashlib.sha512(struct.pack(">H", ord("R"))).digest()[:32].hex().upper()


_XRPL_BASE58_ALPHABET = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
_XRPL_BASE58_REVERSE = {c: i for i, c in enumerate(_XRPL_BASE58_ALPHABET)}
_NODE_PUBLIC_TOKEN_TYPE = 28


def _decode_node_public_key(value: str) -> str:
    """Decode a node-public token to the hex bytes used by ledger JSON."""
    if len(value) in (66, 68) and all(c in "0123456789abcdefABCDEF" for c in value):
        return value.upper()

    n = 0
    for char in value:
        try:
            digit = _XRPL_BASE58_REVERSE[char]
        except KeyError as exc:
            raise ValueError(f"Invalid XRPL base58 character in {value!r}") from exc
        n = n * 58 + digit

    decoded = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    leading_zeroes = len(value) - len(value.lstrip(_XRPL_BASE58_ALPHABET[0]))
    decoded = (b"\x00" * leading_zeroes) + decoded
    if len(decoded) < 6:
        raise ValueError(f"Invalid node public key token {value!r}")

    payload = decoded[:-4]
    checksum = decoded[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        raise ValueError(f"Invalid node public key checksum for {value!r}")
    if payload[0] != _NODE_PUBLIC_TOKEN_TYPE:
        raise ValueError(f"Unexpected token type for node public key {value!r}")

    return payload[1:].hex().upper()


def _make_unl_report_entry(active_keys: list[str]) -> dict:
    """Build a test-bootstrap UNLReport SLE from validator public keys.

    The real voting path canonicalizes and enriches this SLE. The seed only
    carries the active validator keys needed by consumers that read the
    ledger-anchored active validator view before the first flag-ledger cycle.
    """
    return {
        "Flags": 0,
        "LedgerEntryType": "UNLReport",
        "PreviousTxnID": "0" * 64,
        "PreviousTxnLgrSeq": 0,
        "ActiveValidators": [
            {"ActiveValidator": {"PublicKey": _decode_node_public_key(key)}}
            for key in active_keys
        ],
        "index": _unl_report_index(),
    }


def _make_short_skiplist_entry(
    ledger_index: int, prior_hashes: list[str]
) -> dict | None:
    """Build the short LedgerHashes SLE (keylet::skip()) for a synthetic start.

    The short list holds at most the last ``SKIP_LIST_INTERVAL`` (256) ledger
    hashes, oldest to newest, mirroring how xahaud evicts the front once full.

    Args:
        ledger_index: The starting ledger index (>= 1).
        prior_hashes: Hashes of ledgers ending at ledger_index-1, oldest to
            newest, already capped to the last SKIP_LIST_INTERVAL entries.

    Returns:
        The LedgerHashes entry dict, or None if ledger_index is 1 (no prior hashes).
    """
    expected = min(ledger_index - 1, SKIP_LIST_INTERVAL)
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


def _synthetic_hash(seq: int) -> str:
    """Deterministic fake hash for a synthetic pre-genesis ledger.

    Keyed by absolute sequence so the short and long skip lists agree on the
    hash of any given ledger. This is deliberately fake prehistory — it only
    exists so hashOfSeq() returns *something*; it is NOT real ancestor data.
    """
    h = hashlib.sha512(b"synthetic-ledger-" + str(seq).encode()).digest()[:32]
    return h.hex().upper()


def _generate_synthetic_hashes(count: int) -> list[str]:
    """Generate fake ledger hashes for ledgers 1..count, oldest to newest.

    See _synthetic_hash() for the caveat: this is fake prehistory, not real
    ancestor data.
    """
    return [_synthetic_hash(i) for i in range(1, count + 1)]


def _make_long_skiplist_entries(start_ledger: int) -> list[dict]:
    """Build long LedgerHashes SLEs (keylet::skip(seq)) for a synthetic start.

    A real chain records the hash of every 256th ledger, bucketed into one SLE
    per 65536-ledger window (keylet::skip keys on seq >> 16). Reproduce those
    buckets so hashOfSeq() resolves multiple-of-256 ancestors that have aged
    out of the short (last-256) list. The hash of ledger 0 is intentionally
    omitted: it is meaningless and never queried.
    """
    # Largest multiple of 256 a chain at `start_ledger` would already have
    # recorded (recorded when building the *next* ledger, hence start_ledger-1).
    last_recorded = (start_ledger - 1) // SKIP_LIST_INTERVAL * SKIP_LIST_INTERVAL
    if last_recorded < SKIP_LIST_INTERVAL:
        return []  # nothing has aged into the long list yet

    buckets: dict[int, list[int]] = {}
    for m in range(SKIP_LIST_INTERVAL, last_recorded + 1, SKIP_LIST_INTERVAL):
        buckets.setdefault(m >> 16, []).append(m)

    return [
        {
            "Flags": 0,
            "LedgerEntryType": "LedgerHashes",
            "Hashes": [_synthetic_hash(s) for s in seqs],  # ascending
            "LastLedgerSequence": seqs[-1],
            "index": _long_skip_index(window),
        }
        for window, seqs in sorted(buckets.items())
    ]


def _resolve_feature_hash(spec: str) -> str:
    """Resolve a feature spec to its amendment hash.

    Accepts:
        @Name           — name-to-hash via sha512Half
        ABC123... (64)  — raw 64-char hex hash
        Name            — bare name, hashed via sha512Half

    The C++ convention uses ``featureX`` for the variable name but the
    amendment name is just ``X`` (without ``feature`` prefix).  The
    ``fix`` prefix IS part of the name.  So we strip ``feature`` but
    keep ``fix``.
    """
    if spec.startswith("@"):
        spec = spec[1:]
    elif len(spec) == 64 and all(c in "0123456789abcdefABCDEF" for c in spec):
        return spec.upper()

    # Strip C++ "feature" prefix convention
    if spec.startswith("feature"):
        spec = spec.removeprefix("feature")

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
    unl_report_keys: list[str] | None = None,
) -> Path:
    """Create a modified genesis.json with custom amendments and/or start ledger.

    Args:
        base_genesis: Path to the base genesis.json file
        features: List of amendment hashes or @names. Prefix with '-' to remove.
                  Use @Name syntax to compute hash from name (e.g., @RNG).
        start_ledger: Optional starting ledger sequence number. Synthetic short
                      and long skip lists are injected so hashOfSeq() resolves
                      for arbitrary starts (this is fake prehistory, not real
                      ancestor data).
        majority_features: Optional list of feature names/@hashes to pre-seed
                          in sfMajorities of the Amendments SLE. Uses CloseTime=0
                          so the hold time is already satisfied. Nodes must still
                          vote yes (feature accept) before the voting ledger, or
                          the seeded majority will be cleared via tfLostMajority.
        unl_report_keys: Optional validator public keys to seed into UNLReport.

    Returns:
        Path to the (possibly modified) genesis file.
        If no modifications needed, returns base_genesis unchanged.
    """
    if (
        not features
        and start_ledger is None
        and not majority_features
        and not unl_report_keys
    ):
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

    # Testnet-only bootstrap: seed the ledger-anchored active validator view
    # directly. The normal NegativeUNL reporting path needs a full flag-ledger
    # validation history window, so start-ledger shortcuts cannot create this
    # SLE faithfully.
    if unl_report_keys:
        account_state[:] = [
            e
            for e in account_state
            if not (isinstance(e, dict) and e.get("LedgerEntryType") == "UNLReport")
        ]
        account_state.append(_make_unl_report_entry(unl_report_keys))

    # Modify start ledger if requested
    if start_ledger is not None:
        genesis["ledger"]["seqNum"] = str(start_ledger)
        genesis["ledger"]["ledger_index"] = str(start_ledger)

        # Inject fake skip lists so hashOfSeq() resolves for arbitrary starts.
        # loadLedgerFromFile() rebuilds from ledger_index + accountState;
        # wrapper fields like hash/parent_hash are not trusted.
        #
        # The short list (keylet::skip()) holds the last <=256 ledger hashes;
        # the long list (keylet::skip(seq)) holds every 256th hash for deeper
        # ancestry. Both are needed once start_ledger > 256 — without capping
        # the short list at 256 entries, the next ledger trips xahaud's
        # "hashes.size() <= 256" assertion in updateSkipList().
        if start_ledger > 1:
            # Remove any existing LedgerHashes entries
            account_state[:] = [
                e
                for e in account_state
                if not (
                    isinstance(e, dict) and e.get("LedgerEntryType") == "LedgerHashes"
                )
            ]
            short_start = max(1, start_ledger - SKIP_LIST_INTERVAL)
            short_hashes = [
                _synthetic_hash(s) for s in range(short_start, start_ledger)
            ]
            short_entry = _make_short_skiplist_entry(start_ledger, short_hashes)
            if short_entry is not None:
                account_state.append(short_entry)
            account_state.extend(_make_long_skiplist_entries(start_ledger))

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
        fixed_peers: If True, generated configs include full-mesh [ips_fixed].
    """

    network_id: int = DEFAULT_NETWORK_ID
    node_count: int = DEFAULT_NODE_COUNT
    validators: int | None = None
    fixed_peers: bool = True
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

    @property
    def peer_host(self) -> str:
        """Loopback address this node's peers dial it at: 127.0.0.<id+1>.

        Each node gets a distinct loopback IP because rippled's peerfinder dedups
        fixed peers by IP address (ignoring port); see generator.py's [ips_fixed]
        and `x-testnet setup-aliases`.
        """
        return f"127.0.0.{self.id + 1}"

    @property
    def peer_addr(self) -> str:
        """This node's peer endpoint as host:port (e.g. 127.0.0.2:21236)."""
        return f"{self.peer_host}:{self.port_peer}"


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
        self._fixed_peers: bool = True
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

    def fixed_peers(self, enabled: bool = True) -> ConfigBuilder:
        """Set whether generated configs include full-mesh fixed peers."""
        self._fixed_peers = enabled
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
            fixed_peers=self._fixed_peers,
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
