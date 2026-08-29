"""Tests for x-testnet topology helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from xahaud_scripts.testnet import loopback
from xahaud_scripts.testnet.config import NodeInfo
from xahaud_scripts.testnet.loopback import LoopbackAliasError
from xahaud_scripts.testnet.topology import (
    connect_managed_peer,
    disconnect_managed_peer,
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
                {"address": "127.0.0.2:21236", "public_key": "pk1"},
                {"address": "127.0.0.3:64000", "public_key": "pk2"},
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
            0: [{"address": "127.0.0.2:59371", "public_key": "node1"}],
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
            0: [{"address": "127.0.0.2:21236", "public_key": "pk1"}],
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


class DialRPC(FakeRPC):
    """FakeRPC that records the endpoints it was asked to dial/drop."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.connects: list[tuple[int, str, int]] = []
        self.disconnects: list[tuple[int, str, int]] = []

    def connect(self, node_id: int, ip: str, port: int) -> dict[str, Any]:
        self.connects.append((node_id, ip, port))
        return {"status": "success"}

    def disconnect(self, node_id: int, ip: str, port: int) -> dict[str, Any]:
        self.disconnects.append((node_id, ip, port))
        return {"status": "success"}


def _only_base_loopback() -> set[str]:
    """Probe result for a machine with no aliases created."""
    return {"127.0.0.1"}


def test_connect_managed_peer_dials_the_targets_distinct_alias():
    """Regression guard for the addressing scheme.

    Dialing 127.0.0.1 for every node looks like it works — the RPC succeeds —
    but rippled's peerfinder dedups peers by IP ignoring port, so the mesh
    collapses. Every dial must use the target's own 127.0.0.<id+1>.
    """
    nodes = [_node(0, "pk0"), _node(1, "pk1"), _node(2, "pk2")]
    rpc = DialRPC({})

    connect_managed_peer(rpc, nodes, source=0, target=2)

    assert rpc.connects == [(0, "127.0.0.3", 21237)]


def test_connect_managed_peer_rejects_unknown_target():
    nodes = [_node(0, "pk0")]
    with pytest.raises(ValueError, match="n7"):
        connect_managed_peer(DialRPC({}), nodes, source=0, target=7)


def test_connect_managed_peer_fails_fast_when_alias_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """A missing alias must fail here, not silently 15s later as actual=[]."""
    monkeypatch.setattr(loopback, "aliases_required", lambda: True)
    monkeypatch.setattr(loopback, "_probe_loopback_addresses", _only_base_loopback)
    loopback.reset_cache()

    nodes = [_node(0, "pk0"), _node(1, "pk1")]
    rpc = DialRPC({})

    with pytest.raises(LoopbackAliasError) as exc:
        connect_managed_peer(rpc, nodes, source=0, target=1)

    message = str(exc.value)
    assert "n0->n1 connect" in message
    assert "127.0.0.2" in message
    # The remedy must be copy-pasteable, both forms.
    assert "x-testnet setup-aliases -n 2" in message
    assert "sudo ifconfig lo0 alias 127.0.0.2 up" in message
    # And nothing was dialed.
    assert rpc.connects == []


def test_disconnect_managed_peer_fallback_is_not_alias_guarded(
    monkeypatch: pytest.MonkeyPatch,
):
    """Disconnect must work even with no alias — unlike dialing.

    The daemon's disconnect handler only parses the endpoint and compares it
    against active peers; it never dials or binds that address. Refusing here
    because a local alias is absent could only reduce recoverability.
    """
    monkeypatch.setattr(loopback, "aliases_required", lambda: True)
    monkeypatch.setattr(loopback, "_probe_loopback_addresses", _only_base_loopback)
    loopback.reset_cache()

    nodes = [_node(0, "pk0"), _node(1, "pk1")]
    rpc = DialRPC({0: [], 1: []})

    disconnect_managed_peer(rpc, nodes, source=0, target=1)

    assert rpc.disconnects == [(0, "127.0.0.2", 21236)]


def test_disconnect_managed_peer_uses_live_endpoint_without_alias_check():
    """A matched live session needs no alias check — the socket already exists."""
    nodes = [_node(0, "pk0"), _node(1, "pk1")]
    rpc = DialRPC({0: [{"address": "127.0.0.2:21236", "public_key": "pk1"}]})

    disconnect_managed_peer(rpc, nodes, source=0, target=1)

    assert rpc.disconnects == [(0, "127.0.0.2", 21236)]
