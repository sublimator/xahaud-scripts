"""Tests for testnet configuration helpers (testnet/config.py)."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any, cast

import pytest

from xahaud_scripts.testnet.config import (
    DEFAULT_BASE_PORT_PEER,
    DEFAULT_BASE_PORT_RPC,
    DEFAULT_BASE_PORT_WS,
    DEFAULT_NODE_COUNT,
    SKIP_LIST_INTERVAL,
    ConfigBuilder,
    LaunchConfig,
    NetworkConfig,
    NodeInfo,
    _generate_synthetic_hashes,
    _get_or_create_amendments_entry,
    _long_skip_index,
    _make_long_skiplist_entries,
    _make_short_skiplist_entry,
    _resolve_feature_hash,
    _short_skip_index,
    _synthetic_hash,
    _unl_report_index,
    feature_name_to_hash,
    get_bundled_genesis_file,
    prepare_genesis_file,
    resolve_feature_name,
)
from xahaud_scripts.testnet.generator import generate_node_config
from xahaud_scripts.testnet.suite import (
    _apply_runtime_topology,
    _build_launch_config,
    _create_network,
    _parse_topology_nodes,
)
from xahaud_scripts.testnet.topology import disconnect_managed_peer


def _name_to_hash(name: str) -> str:
    return hashlib.sha512(name.encode()).digest()[:32].hex().upper()


# --- feature_name_to_hash ---


def test_feature_name_to_hash_shape():
    h = feature_name_to_hash("RNG")
    assert len(h) == 64
    assert h == h.upper()
    assert set(h) <= set("0123456789ABCDEF")  # uppercase hex only


def test_feature_name_to_hash_matches_sha512_half():
    assert feature_name_to_hash("RNG") == _name_to_hash("RNG")


def test_feature_name_to_hash_deterministic():
    assert feature_name_to_hash("Hooks") == feature_name_to_hash("Hooks")


# --- _resolve_feature_hash ---


def test_resolve_feature_hash_at_prefix():
    assert _resolve_feature_hash("@RNG") == _name_to_hash("RNG")


def test_resolve_feature_hash_raw_hex_uppercased():
    raw = "ab" * 32  # 64 hex chars
    assert _resolve_feature_hash(raw) == raw.upper()


def test_resolve_feature_hash_strips_feature_keeps_fix():
    assert _resolve_feature_hash("featureExport") == _name_to_hash("Export")
    assert _resolve_feature_hash("fixXahauV2") == _name_to_hash("fixXahauV2")


# --- resolve_feature_name ---


def test_resolve_feature_name_strips_at():
    assert resolve_feature_name("@RNG") == "RNG"
    assert resolve_feature_name("RNG") == "RNG"


# --- _short_skip_index ---


def test_short_skip_index_matches_keylet_formula():
    expected = hashlib.sha512(struct.pack(">H", ord("s"))).digest()[:32].hex().upper()
    assert _short_skip_index() == expected


def test_unl_report_index_matches_keylet_formula():
    expected = hashlib.sha512(struct.pack(">H", ord("R"))).digest()[:32].hex().upper()
    assert _unl_report_index() == expected


# --- _generate_synthetic_hashes ---


def test_generate_synthetic_hashes_count_and_shape():
    hashes = _generate_synthetic_hashes(4)
    assert len(hashes) == 4
    for h in hashes:
        assert len(h) == 64
        assert h == h.upper()
    assert len(set(hashes)) == 4  # all distinct


def test_generate_synthetic_hashes_zero():
    assert _generate_synthetic_hashes(0) == []


def test_generate_synthetic_hashes_deterministic():
    assert _generate_synthetic_hashes(3) == _generate_synthetic_hashes(3)


# --- _make_short_skiplist_entry ---


def test_make_short_skiplist_entry_first_ledger_is_none():
    assert _make_short_skiplist_entry(1, []) is None


def test_make_short_skiplist_entry_wrong_hash_count_raises():
    with pytest.raises(ValueError, match="needs 2 prior hashes"):
        _make_short_skiplist_entry(3, ["deadbeef"])


def test_make_short_skiplist_entry_builds_sle():
    prior = _generate_synthetic_hashes(2)
    entry = _make_short_skiplist_entry(3, prior)
    assert entry is not None
    assert entry["LedgerEntryType"] == "LedgerHashes"
    assert entry["LastLedgerSequence"] == 2
    assert entry["Hashes"] == [h.upper() for h in prior]
    assert entry["index"] == _short_skip_index()


def test_make_short_skiplist_entry_uppercases_hashes():
    entry = _make_short_skiplist_entry(2, ["abc123"])
    assert entry is not None
    assert entry["Hashes"] == ["ABC123"]


def test_make_short_skiplist_entry_caps_at_interval():
    # A start past one flag interval supplies exactly SKIP_LIST_INTERVAL hashes.
    capped = _generate_synthetic_hashes(SKIP_LIST_INTERVAL)
    entry = _make_short_skiplist_entry(1000, capped)
    assert entry is not None
    assert len(entry["Hashes"]) == SKIP_LIST_INTERVAL
    assert entry["LastLedgerSequence"] == 999


# --- _long_skip_index / _make_long_skiplist_entries ---


def test_long_skip_index_matches_keylet_formula():
    # keylet::skip(ledger) = sha512Half(uint16_be('s'), uint32_be(ledger >> 16))
    expected = (
        hashlib.sha512(struct.pack(">H", ord("s")) + struct.pack(">I", 0))
        .digest()[:32]
        .hex()
        .upper()
    )
    assert _long_skip_index(0) == expected
    # Distinct windows give distinct indexes.
    assert _long_skip_index(1) != _long_skip_index(0)


def test_long_skiplist_empty_before_first_interval():
    assert _make_long_skiplist_entries(SKIP_LIST_INTERVAL) == []  # start=256
    assert _make_long_skiplist_entries(100) == []


def test_long_skiplist_records_aged_multiples():
    # Genesis at 600: chain would have recorded hashes of 256 and 512.
    entries = _make_long_skiplist_entries(600)
    assert len(entries) == 1  # single 65536-window bucket
    entry = entries[0]
    assert entry["index"] == _long_skip_index(0)
    assert entry["Hashes"] == [_synthetic_hash(256), _synthetic_hash(512)]
    assert entry["LastLedgerSequence"] == 512


def test_prepare_genesis_start_ledger_past_interval_caps_and_buckets():
    base = get_bundled_genesis_file()
    out = prepare_genesis_file(base, features=[], start_ledger=600)
    try:
        genesis = json.loads(out.read_text())
        skiplists = [
            e
            for e in genesis["ledger"]["accountState"]
            if e.get("LedgerEntryType") == "LedgerHashes"
        ]
        short = [e for e in skiplists if e["index"] == _short_skip_index()]
        longs = [e for e in skiplists if e["index"] == _long_skip_index(0)]
        assert len(short) == 1
        assert len(short[0]["Hashes"]) == SKIP_LIST_INTERVAL  # capped, not 599
        assert short[0]["LastLedgerSequence"] == 599
        # Newest short-list hash is the immediate parent (599); oldest is 344.
        assert short[0]["Hashes"][-1] == _synthetic_hash(599)
        assert short[0]["Hashes"][0] == _synthetic_hash(600 - SKIP_LIST_INTERVAL)
        assert len(longs) == 1
        assert longs[0]["Hashes"] == [_synthetic_hash(256), _synthetic_hash(512)]
    finally:
        out.unlink()


# --- _get_or_create_amendments_entry ---


def test_get_amendments_entry_found():
    state = [{"LedgerEntryType": "AccountRoot"}, {"LedgerEntryType": "Amendments"}]
    assert _get_or_create_amendments_entry(state)["LedgerEntryType"] == "Amendments"


def test_get_amendments_entry_missing_raises():
    with pytest.raises(ValueError, match="No Amendments entry"):
        _get_or_create_amendments_entry([{"LedgerEntryType": "AccountRoot"}])


# --- get_bundled_genesis_file ---


def test_bundled_genesis_file_exists_and_parses():
    path = get_bundled_genesis_file()
    assert path.exists()
    assert path.name == "genesis.json"
    genesis = json.loads(path.read_text())
    assert "accountState" in genesis["ledger"]


# --- prepare_genesis_file ---


def test_prepare_genesis_no_changes_returns_base():
    base = get_bundled_genesis_file()
    assert prepare_genesis_file(base, features=[]) is base


def test_prepare_genesis_adds_feature():
    base = get_bundled_genesis_file()
    new_hash = _name_to_hash("SomeBrandNewAmendment")
    out = prepare_genesis_file(base, features=["@SomeBrandNewAmendment"])
    try:
        assert out != base
        genesis = json.loads(out.read_text())
        amendments = _get_or_create_amendments_entry(genesis["ledger"]["accountState"])
        assert new_hash in amendments["Amendments"]
        # Amendment list is kept sorted.
        assert amendments["Amendments"] == sorted(amendments["Amendments"])
    finally:
        out.unlink()


def test_prepare_genesis_removes_feature():
    base = get_bundled_genesis_file()
    genesis = json.loads(base.read_text())
    existing = _get_or_create_amendments_entry(genesis["ledger"]["accountState"])[
        "Amendments"
    ][0]

    out = prepare_genesis_file(base, features=[f"-{existing}"])
    try:
        result = json.loads(out.read_text())
        amendments = _get_or_create_amendments_entry(result["ledger"]["accountState"])
        assert existing not in amendments["Amendments"]
    finally:
        out.unlink()


def test_prepare_genesis_start_ledger_injects_skiplist():
    base = get_bundled_genesis_file()
    out = prepare_genesis_file(base, features=[], start_ledger=5)
    try:
        genesis = json.loads(out.read_text())
        assert genesis["ledger"]["seqNum"] == "5"
        assert genesis["ledger"]["ledger_index"] == "5"
        skiplists = [
            e
            for e in genesis["ledger"]["accountState"]
            if e.get("LedgerEntryType") == "LedgerHashes"
        ]
        assert len(skiplists) == 1
        assert skiplists[0]["LastLedgerSequence"] == 4
        assert len(skiplists[0]["Hashes"]) == 4
    finally:
        out.unlink()


def test_prepare_genesis_start_ledger_one_no_skiplist():
    base = get_bundled_genesis_file()
    out = prepare_genesis_file(base, features=[], start_ledger=1)
    try:
        genesis = json.loads(out.read_text())
        assert genesis["ledger"]["seqNum"] == "1"
        skiplists = [
            e
            for e in genesis["ledger"]["accountState"]
            if e.get("LedgerEntryType") == "LedgerHashes"
        ]
        assert skiplists == []
    finally:
        out.unlink()


def test_prepare_genesis_seeds_unl_report():
    base = get_bundled_genesis_file()
    active_key = "02" + ("AB" * 32)
    out = prepare_genesis_file(base, features=[], unl_report_keys=[active_key])
    try:
        genesis = json.loads(out.read_text())
        reports = [
            e
            for e in genesis["ledger"]["accountState"]
            if e.get("LedgerEntryType") == "UNLReport"
        ]
        assert len(reports) == 1
        report = reports[0]
        assert report["index"] == _unl_report_index()
        assert report["PreviousTxnID"] == "0" * 64
        assert report["PreviousTxnLgrSeq"] == 0
        assert report["ActiveValidators"] == [
            {"ActiveValidator": {"PublicKey": active_key}}
        ]
    finally:
        out.unlink()


def test_prepare_genesis_seeds_majorities():
    base = get_bundled_genesis_file()
    out = prepare_genesis_file(
        base, features=[], majority_features=["@PendingAmendment"]
    )
    try:
        genesis = json.loads(out.read_text())
        amendments = _get_or_create_amendments_entry(genesis["ledger"]["accountState"])
        seeded = {m["Majority"]["Amendment"] for m in amendments["Majorities"]}
        assert _name_to_hash("PendingAmendment") in seeded
        for m in amendments["Majorities"]:
            if m["Majority"]["Amendment"] == _name_to_hash("PendingAmendment"):
                assert m["Majority"]["CloseTime"] == 0
    finally:
        out.unlink()


# --- NetworkConfig ---


def test_network_config_validator_count_defaults_to_node_count():
    assert NetworkConfig(node_count=7).validator_count == 7


def test_network_config_validator_count_explicit():
    assert NetworkConfig(node_count=7, validators=3).validator_count == 3


def test_network_config_ports_offset_by_node_id():
    cfg = NetworkConfig()
    assert cfg.port_peer(0) == DEFAULT_BASE_PORT_PEER
    assert cfg.port_rpc(2) == DEFAULT_BASE_PORT_RPC + 2
    assert cfg.port_ws(4) == DEFAULT_BASE_PORT_WS + 4


def test_generate_node_config_full_mesh_fixed_peers_by_default(tmp_path: Path):
    node_dir = tmp_path / "n0"
    node_dir.mkdir()
    validators_file = node_dir / "validators.txt"
    validators_file.write_text("")

    cfg_path = generate_node_config(
        node_id=0,
        node_dir=node_dir,
        validator_token="token",
        validators_file=validators_file,
        network_config=NetworkConfig(node_count=3),
    )

    text = cfg_path.read_text()
    assert "[ips_fixed]" in text
    assert f"127.0.0.1 {DEFAULT_BASE_PORT_PEER + 1}" in text
    assert f"127.0.0.1 {DEFAULT_BASE_PORT_PEER + 2}" in text


def test_generate_node_config_can_omit_fixed_peers(tmp_path: Path):
    node_dir = tmp_path / "n0"
    node_dir.mkdir()
    validators_file = node_dir / "validators.txt"
    validators_file.write_text("")

    cfg_path = generate_node_config(
        node_id=0,
        node_dir=node_dir,
        validator_token="token",
        validators_file=validators_file,
        network_config=NetworkConfig(node_count=3, fixed_peers=False),
    )

    text = cfg_path.read_text()
    assert "\n[ips_fixed]\n" not in text
    assert "[port_peer]" in text
    assert "[peers_max]" in text


# --- NodeInfo ---


def test_node_info_node_dir_is_config_parent(tmp_path: Path):
    cfg_path = tmp_path / "n0" / "xahaud.cfg"
    node = NodeInfo(
        id=0,
        public_key="pk",
        token="tok",
        config_path=cfg_path,
        port_peer=1,
        port_rpc=2,
        port_ws=3,
    )
    assert node.node_dir == tmp_path / "n0"


# --- LaunchConfig ---


def test_launch_config_rippled_path_default(tmp_path: Path):
    cfg = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "rippled",
        genesis_file=tmp_path / "genesis.json",
    )
    assert cfg.get_rippled_path(0) == tmp_path / "rippled"


def test_launch_config_rippled_path_node_override(tmp_path: Path):
    cfg = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "rippled",
        genesis_file=tmp_path / "genesis.json",
        node_rippled_paths={1: tmp_path / "special-rippled"},
    )
    assert cfg.get_rippled_path(0) == tmp_path / "rippled"
    assert cfg.get_rippled_path(1) == tmp_path / "special-rippled"


# --- ConfigBuilder ---


def test_config_builder_defaults(tmp_path: Path):
    network, launch = ConfigBuilder().xahaud_root(tmp_path).build()
    assert network.node_count == DEFAULT_NODE_COUNT
    assert launch.xahaud_root == tmp_path
    assert launch.rippled_path == tmp_path / "build" / "rippled"
    assert launch.genesis_file == get_bundled_genesis_file()


def test_config_builder_fluent_overrides(tmp_path: Path):
    network, launch = (
        ConfigBuilder()
        .xahaud_root(tmp_path)
        .node_count(3)
        .network_id(42)
        .ports(peer=30000, rpc=31000, ws=32000)
        .fixed_peers(False)
        .quorum(2)
        .slave_delay(2.5)
        .extra_args(["--foo"])
        .build()
    )
    assert network.node_count == 3
    assert network.network_id == 42
    assert network.base_port_peer == 30000
    assert network.fixed_peers is False
    assert network.port_rpc(1) == 31001
    assert launch.quorum == 2
    assert launch.slave_delay == 2.5
    assert launch.extra_args == ["--foo"]


def test_config_builder_base_dir_path_explicit(tmp_path: Path):
    builder = ConfigBuilder().xahaud_root(tmp_path).base_dir(tmp_path / "custom")
    assert builder.base_dir_path == tmp_path / "custom"


def test_config_builder_base_dir_path_defaults_to_root(tmp_path: Path):
    builder = ConfigBuilder().xahaud_root(tmp_path)
    assert builder.base_dir_path == tmp_path / "testnet"


def test_suite_network_fixed_peers_false_reaches_network_config(tmp_path: Path):
    network = _create_network(
        tmp_path,
        {"node_count": 3, "fixed_peers": False},
    )

    assert network.config.node_count == 3
    assert network.config.fixed_peers is False


def test_suite_launch_config_fixed_peers_false_keeps_validator_count(tmp_path: Path):
    launch = _build_launch_config(
        tmp_path,
        {"node_count": 3, "fixed_peers": False},
        network_config=NetworkConfig(node_count=3, fixed_peers=False),
    )

    assert launch.xahaud_root == tmp_path


class _TopologyRPC:
    def __init__(
        self,
        *,
        connect_result: dict[str, Any] | None = None,
        disconnect_result: dict[str, Any] | None = None,
        peers_by_node: dict[int, list[dict[str, Any]]] | None = None,
        pubkey_node_by_node: dict[int, str] | None = None,
    ) -> None:
        self.connect_result = connect_result or {"status": "success"}
        self.disconnect_result = disconnect_result or {"status": "success"}
        self.peers_by_node = peers_by_node or {}
        self.pubkey_node_by_node = pubkey_node_by_node or {}
        self.disconnect_calls: list[tuple[int, str, int]] = []

    def server_info(self, node_id: int) -> dict[str, Any]:
        return {"info": {"pubkey_node": self.pubkey_node_by_node.get(node_id)}}

    def peers(self, node_id: int) -> list[dict[str, Any]]:
        return self.peers_by_node.get(node_id, [])

    def connect(self, node_id: int, ip: str, port: int) -> dict[str, Any] | None:
        return self.connect_result

    def disconnect(self, node_id: int, ip: str, port: int) -> dict[str, Any] | None:
        self.disconnect_calls.append((node_id, ip, port))
        return self.disconnect_result


class _TopologyNetwork:
    def __init__(
        self,
        *,
        fixed_peers: bool,
        rpc_client: _TopologyRPC | None = None,
    ) -> None:
        self.config = NetworkConfig(node_count=2, fixed_peers=fixed_peers)
        self.nodes = [
            NodeInfo(
                id=0,
                public_key="pk0",
                token="token0",
                config_path=Path("/tmp/n0/xahaud.cfg"),
                port_peer=DEFAULT_BASE_PORT_PEER,
                port_rpc=DEFAULT_BASE_PORT_RPC,
                port_ws=DEFAULT_BASE_PORT_WS,
            ),
            NodeInfo(
                id=1,
                public_key="pk1",
                token="token1",
                config_path=Path("/tmp/n1/xahaud.cfg"),
                port_peer=DEFAULT_BASE_PORT_PEER + 1,
                port_rpc=DEFAULT_BASE_PORT_RPC + 1,
                port_ws=DEFAULT_BASE_PORT_WS + 1,
            ),
        ]
        self.rpc_client = rpc_client or _TopologyRPC()


def test_parse_topology_nodes_accepts_numeric_and_n_specs():
    assert _parse_topology_nodes([0, "1", "n2"]) == [0, 1, 2]


def test_apply_runtime_topology_exact_requires_no_fixed_peers():
    network = _TopologyNetwork(fixed_peers=True)

    with pytest.raises(ValueError, match="fixed_peers: false"):
        _apply_runtime_topology(
            cast(Any, network),
            {"topology": {"edges": ["n0->n1"], "stable_for": 0}},
        )


def test_apply_runtime_topology_rejects_edge_outside_selected_nodes():
    network = _TopologyNetwork(fixed_peers=False)

    with pytest.raises(ValueError, match="outside selected set"):
        _apply_runtime_topology(
            cast(Any, network),
            {
                "topology": {
                    "nodes": ["n0", "n1"],
                    "edges": ["n0->n2"],
                    "stable_for": 0,
                }
            },
        )


def test_apply_runtime_topology_surfaces_connect_rpc_errors():
    network = _TopologyNetwork(
        fixed_peers=False,
        rpc_client=_TopologyRPC(connect_result={"status": "error", "error": "nope"}),
    )

    with pytest.raises(RuntimeError, match="nope"):
        _apply_runtime_topology(
            cast(Any, network),
            {"topology": {"edges": ["n0->n1"], "stable_for": 0}},
        )


def test_disconnect_managed_peer_uses_live_inbound_endpoint():
    rpc = _TopologyRPC(
        peers_by_node={
            0: [
                {
                    "address": "127.0.0.1:64001",
                    "public_key": "nodepk1",
                    "inbound": True,
                }
            ]
        },
        pubkey_node_by_node={1: "nodepk1"},
    )
    network = _TopologyNetwork(fixed_peers=False, rpc_client=rpc)

    result = disconnect_managed_peer(
        cast(Any, rpc),
        network.nodes,
        source=0,
        target=1,
    )

    assert result == {"status": "success"}
    assert rpc.disconnect_calls == [(0, "127.0.0.1", 64001)]


def test_disconnect_managed_peer_falls_back_to_listen_port_when_peer_not_visible():
    rpc = _TopologyRPC()
    network = _TopologyNetwork(fixed_peers=False, rpc_client=rpc)

    result = disconnect_managed_peer(
        cast(Any, rpc),
        network.nodes,
        source=0,
        target=1,
    )

    assert result == {"status": "success"}
    assert rpc.disconnect_calls == [(0, "127.0.0.1", DEFAULT_BASE_PORT_PEER + 1)]
