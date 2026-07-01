"""Tests for x-testnet topology helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xahaud_scripts.testnet.config import NodeInfo
from xahaud_scripts.testnet.topology import (
    normalize_edges,
    parse_edge_spec,
    parse_edge_specs,
    parse_node_ref,
    require_rpc_success,
    snapshot_topology,
    topology_chain,
    topology_clique,
    topology_diff,
    topology_star,
    validate_edges_in_nodes,
)


class FakeRPC:
    def __init__(
        self,
        peers: dict[int, list[dict[str, Any]] | None],
        *,
        node_keys: dict[int, str] | None = None,
    ) -> None:
        self._peers = peers
        self._node_keys = node_keys or {}

    def peers(self, node_id: int) -> list[dict[str, Any]] | None:
        return self._peers.get(node_id)

    def server_info(self, node_id: int) -> dict[str, Any] | None:
        key = self._node_keys.get(node_id)
        if key is None:
            return {"info": {}}
        return {"info": {"pubkey_node": key}}


def _node(node_id: int, public_key: str) -> NodeInfo:
    return NodeInfo(
        id=node_id,
        public_key=public_key,
        token=f"token-{node_id}",
        config_path=Path(f"/tmp/n{node_id}/xahaud.cfg"),
        port_peer=21235 + node_id,
        port_rpc=5005 + node_id,
        port_ws=6005 + node_id,
    )


def test_topology_builders():
    assert topology_star(center=0, nodes=[0, 1, 2]) == {
        (0, 1),
        (1, 0),
        (0, 2),
        (2, 0),
    }
    assert topology_chain([0, 1, 2], bidirectional=False) == {(0, 1), (1, 2)}
    assert topology_clique([0, 1], bidirectional=True) == {(0, 1), (1, 0)}


def test_normalize_edges_rejects_self_edge():
    try:
        normalize_edges([(0, 0)])
    except ValueError as exc:
        assert "Self-edge" in str(exc)
    else:
        raise AssertionError("expected self-edge ValueError")


def test_parse_edge_specs():
    assert parse_edge_spec("n0->n1") == (0, 1)
    assert parse_edge_specs(["n0->n1"], bidirectional=True) == {(0, 1), (1, 0)}


def test_parse_node_ref_accepts_ids_and_node_specs():
    assert parse_node_ref(2) == 2
    assert parse_node_ref("2") == 2
    assert parse_node_ref("n2") == 2


def test_validate_edges_in_nodes_rejects_out_of_scope_edge():
    try:
        validate_edges_in_nodes({(0, 2)}, [0, 1])
    except ValueError as exc:
        assert "outside selected set" in str(exc)
        assert "n2" in str(exc)
    else:
        raise AssertionError("expected out-of-scope edge ValueError")


def test_snapshot_topology_tracks_outbound_and_adjacent_edges():
    nodes = [_node(0, "pk0"), _node(1, "pk1"), _node(2, "pk2")]
    rpc = FakeRPC(
        {
            0: [
                {"address": "127.0.0.1:21236", "public_key": "pk1"},
                {"address": "127.0.0.1:64000", "public_key": "pk2"},
            ],
            1: [{"address": "127.0.0.1:21235", "public_key": "pk0"}],
            2: [],
        }
    )

    snapshot = snapshot_topology(rpc, nodes)

    assert snapshot.outbound_edges == {(0, 1), (0, 2), (1, 0)}
    assert snapshot.adjacent_edges == {frozenset((0, 1)), frozenset((0, 2))}
    assert snapshot.unreachable_nodes == set()


def test_snapshot_topology_maps_inbound_ephemeral_peer_by_node_key():
    nodes = [_node(0, "validator0"), _node(1, "validator1")]
    rpc = FakeRPC(
        {
            0: [{"address": "127.0.0.1:59371", "public_key": "node1"}],
            1: [{"address": "127.0.0.1:21235", "public_key": "node0"}],
        },
        node_keys={0: "node0", 1: "node1"},
    )

    snapshot = snapshot_topology(rpc, nodes)

    assert snapshot.outbound_edges == {(0, 1), (1, 0)}
    assert snapshot.adjacent_edges == {frozenset((0, 1))}


def test_topology_diff_reports_missing_and_extra_edges():
    nodes = [_node(0, "pk0"), _node(1, "pk1"), _node(2, "pk2")]
    rpc = FakeRPC(
        {
            0: [{"address": "127.0.0.1:21236", "public_key": "pk1"}],
            1: [],
            2: None,
        }
    )
    snapshot = snapshot_topology(rpc, nodes)

    ok, message = topology_diff(snapshot, {(1, 0)}, nodes=[0, 1, 2])

    assert not ok
    assert "missing=[n1->n0]" in message
    assert "extra=[n0->n1]" in message
    assert "unreachable=[n2]" in message


def test_require_rpc_success_rejects_none_and_error_status():
    require_rpc_success({"status": "success"}, "connect")

    for result in (None, {"status": "error", "error_message": "bad peer"}):
        try:
            require_rpc_success(result, "connect")
        except RuntimeError as exc:
            assert "connect" in str(exc)
        else:
            raise AssertionError("expected RPC failure")
