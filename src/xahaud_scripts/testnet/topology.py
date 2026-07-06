"""Topology helpers for local x-testnet scenarios.

The generated local network can start as a fixed full mesh or as isolated
nodes. These helpers operate on the live peer graph reported by RPC and build
directed edge sets for scenario scripts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from xahaud_scripts.testnet.config import NodeInfo

Edge = tuple[int, int]


class PeerRPC(Protocol):
    """Subset of RPCClient needed for topology snapshots."""

    def peers(self, node_id: int) -> list[dict[str, Any]] | None:
        """Return active peers for a node, or None if unreachable."""
        ...

    def server_info(self, node_id: int) -> dict[str, Any] | None:
        """Return server_info for a node, or None if unreachable."""
        ...


class DisconnectRPC(PeerRPC, Protocol):
    """Peer RPC subset that can also disconnect a live endpoint."""

    def disconnect(
        self,
        node_id: int,
        ip: str,
        port: int,
    ) -> dict[str, Any] | None:
        """Tell a node to disconnect from a peer endpoint."""
        ...


@dataclass(frozen=True)
class TopologySnapshot:
    """Live peer graph among managed testnet nodes."""

    outbound_edges: set[Edge]
    adjacent_edges: set[frozenset[int]]
    raw_peers: dict[int, list[dict[str, Any]]]
    unreachable_nodes: set[int]

    def restricted(self, nodes: Iterable[int]) -> TopologySnapshot:
        """Return a snapshot restricted to the induced graph over nodes."""
        node_set = set(nodes)
        return TopologySnapshot(
            outbound_edges={
                edge
                for edge in self.outbound_edges
                if edge[0] in node_set and edge[1] in node_set
            },
            adjacent_edges={
                edge for edge in self.adjacent_edges if set(edge).issubset(node_set)
            },
            raw_peers={nid: self.raw_peers.get(nid, []) for nid in node_set},
            unreachable_nodes=self.unreachable_nodes & node_set,
        )


def all_nodes(node_count: int) -> list[int]:
    """Return all node ids for a local testnet."""
    return list(range(node_count))


def parse_node_ref(spec: int | str) -> int:
    """Parse a node id from an int, decimal string, or n0-style string."""
    if isinstance(spec, bool):
        raise ValueError(f"Invalid node spec: {spec!r}; expected n0, n1, ...")
    if isinstance(spec, int):
        return spec

    text = str(spec).strip()
    if text.isdigit():
        return int(text)
    return parse_node_spec(text)


def parse_node_spec(spec: str) -> int:
    """Parse a node spec like n0 into a node id."""
    text = spec.strip()
    if text.startswith("n") and text[1:].isdigit():
        return int(text[1:])
    raise ValueError(f"Invalid node spec: {spec!r}; expected n0, n1, ...")


def parse_edge_spec(spec: str) -> Edge:
    """Parse a directed edge spec like n0->n1."""
    if "->" not in spec:
        raise ValueError(f"Invalid edge spec: {spec!r}; expected n0->n1")
    source, target = (part.strip() for part in spec.split("->", 1))
    return parse_node_spec(source), parse_node_spec(target)


def parse_edge_specs(
    specs: Iterable[str],
    *,
    bidirectional: bool = False,
) -> set[Edge]:
    """Parse and normalize directed edge specs."""
    return normalize_edges(
        (parse_edge_spec(spec) for spec in specs),
        bidirectional=bidirectional,
    )


def normalize_edges(
    edges: Iterable[Edge],
    *,
    bidirectional: bool = False,
) -> set[Edge]:
    """Normalize directed edges and optionally add reverse edges."""
    result: set[Edge] = set()
    for source, target in edges:
        if source == target:
            raise ValueError(f"Self-edge is not valid: n{source}->n{target}")
        result.add((source, target))
        if bidirectional:
            result.add((target, source))
    return result


def validate_edges_in_nodes(edges: Iterable[Edge], nodes: Iterable[int]) -> None:
    """Fail fast if any topology edge references outside the selected nodes."""
    edge_set = set(edges)
    node_set = set(nodes)
    invalid = sorted(
        {node for edge in edge_set for node in edge if node not in node_set}
    )
    if invalid:
        raise ValueError(
            "Topology edge references node(s) outside selected set: "
            f"{format_nodes(invalid)}; selected={format_nodes(node_set)}; "
            f"edges={format_edges(edge_set)}"
        )


def topology_star(
    *,
    center: int,
    nodes: Iterable[int],
    bidirectional: bool = True,
) -> set[Edge]:
    """Build a star topology edge set."""
    return normalize_edges(
        ((center, node) for node in nodes if node != center),
        bidirectional=bidirectional,
    )


def topology_chain(
    nodes: Iterable[int],
    *,
    bidirectional: bool = True,
) -> set[Edge]:
    """Build a chain topology edge set from ordered nodes."""
    ordered = list(nodes)
    return normalize_edges(
        zip(ordered, ordered[1:], strict=False),
        bidirectional=bidirectional,
    )


def topology_clique(
    nodes: Iterable[int],
    *,
    bidirectional: bool = True,
) -> set[Edge]:
    """Build a clique topology edge set."""
    ordered = list(nodes)
    edges = (
        (source, target) for source in ordered for target in ordered if source != target
    )
    return normalize_edges(edges, bidirectional=bidirectional)


def peer_address_to_node_id(
    address: str | None,
    *,
    port_to_node: dict[int, int],
) -> int | None:
    """Map a peer address like 127.0.0.1:21236 to a managed node id."""
    if not address or ":" not in address:
        return None
    try:
        port = int(address.rsplit(":", 1)[1])
    except ValueError:
        return None
    return port_to_node.get(port)


def peer_address_endpoint(address: str | None) -> tuple[str, int] | None:
    """Parse a peer RPC address into an ``(ip, port)`` endpoint."""
    if not address or ":" not in address:
        return None
    host, port_text = address.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError:
        return None
    if not host or port <= 0:
        return None
    return host, port


def node_identity_map(rpc: PeerRPC, nodes: list[NodeInfo]) -> dict[str, int]:
    """Map known validator and overlay peer public keys to managed node ids."""
    key_to_node = {node.public_key: node.id for node in nodes}
    for node in nodes:
        result = rpc.server_info(node.id)
        info = result.get("info", {}) if result else {}
        peer_key = info.get("pubkey_node")
        if isinstance(peer_key, str):
            key_to_node[peer_key] = node.id
    return key_to_node


def snapshot_topology(
    rpc: PeerRPC,
    nodes: list[NodeInfo],
    *,
    include_nodes: Iterable[int] | None = None,
) -> TopologySnapshot:
    """Read active peers and build directed/adjacent managed-node edges."""
    selected = (
        set(include_nodes) if include_nodes is not None else {n.id for n in nodes}
    )
    nodes_by_id = {node.id: node for node in nodes}
    port_to_node = {node.port_peer: node.id for node in nodes}
    key_to_node = node_identity_map(rpc, nodes)

    raw_peers: dict[int, list[dict[str, Any]]] = {}
    outbound_edges: set[Edge] = set()
    adjacent_edges: set[frozenset[int]] = set()
    unreachable_nodes: set[int] = set()

    for node_id in selected:
        if node_id not in nodes_by_id:
            raise ValueError(f"Unknown node id: n{node_id}")
        peers = rpc.peers(node_id)
        if peers is None:
            unreachable_nodes.add(node_id)
            raw_peers[node_id] = []
            continue
        raw_peers[node_id] = peers

        for peer in peers:
            target = None
            peer_key = peer.get("public_key")
            if isinstance(peer_key, str):
                target = key_to_node.get(peer_key)
            if target is None:
                target = peer_address_to_node_id(
                    peer.get("address"),
                    port_to_node=port_to_node,
                )
            if target is not None and target in selected and target != node_id:
                outbound_edges.add((node_id, target))
                adjacent_edges.add(frozenset((node_id, target)))

            target = peer_address_to_node_id(
                peer.get("address"),
                port_to_node=port_to_node,
            )
            if target is not None and target in selected and target != node_id:
                adjacent_edges.add(frozenset((node_id, target)))

    return TopologySnapshot(
        outbound_edges=outbound_edges,
        adjacent_edges=adjacent_edges,
        raw_peers=raw_peers,
        unreachable_nodes=unreachable_nodes,
    )


def managed_peer_endpoint(
    rpc: PeerRPC,
    nodes: list[NodeInfo],
    *,
    source: int,
    target: int,
) -> tuple[str, int] | None:
    """Return the live endpoint ``source`` sees for managed peer ``target``.

    ``disconnect`` matches an active peer's remote endpoint, not necessarily the
    target's listening peer port. For inbound connections that remote endpoint
    is often an ephemeral source port, so resolve by the peer identity exposed by
    ``peers`` before falling back to the managed listen port.
    """
    peers = rpc.peers(source)
    if peers is None:
        return None

    port_to_node = {node.port_peer: node.id for node in nodes}
    key_to_node = node_identity_map(rpc, nodes)

    for peer in peers:
        address = peer.get("address")
        endpoint = peer_address_endpoint(address)
        if endpoint is None:
            continue

        peer_key = peer.get("public_key")
        if isinstance(peer_key, str) and key_to_node.get(peer_key) == target:
            return endpoint

        if peer_address_to_node_id(address, port_to_node=port_to_node) == target:
            return endpoint

    return None


def disconnect_managed_peer(
    rpc: DisconnectRPC,
    nodes: list[NodeInfo],
    *,
    source: int,
    target: int,
) -> dict[str, Any] | None:
    """Disconnect ``source`` from managed ``target`` using the live endpoint."""
    endpoint = managed_peer_endpoint(rpc, nodes, source=source, target=target)
    if endpoint is None:
        target_node = next((node for node in nodes if node.id == target), None)
        if target_node is None:
            raise ValueError(f"Unknown target node: n{target}")
        endpoint = ("127.0.0.1", target_node.port_peer)
    ip, port = endpoint
    return rpc.disconnect(source, ip, port)


def topology_diff(
    snapshot: TopologySnapshot,
    expected_edges: Iterable[Edge],
    *,
    nodes: Iterable[int] | None = None,
    exact: bool = True,
) -> tuple[bool, str]:
    """Compare a topology snapshot with expected directed edges."""
    expected = set(expected_edges)
    actual_snapshot = snapshot.restricted(nodes) if nodes is not None else snapshot
    actual = actual_snapshot.outbound_edges

    missing = expected - actual
    extra = actual - expected if exact else set()
    ok = not missing and not extra and not actual_snapshot.unreachable_nodes
    if ok:
        return True, "topology matches"

    parts: list[str] = []
    if missing:
        parts.append("missing=" + format_edges(missing))
    if extra:
        parts.append("extra=" + format_edges(extra))
    if actual_snapshot.unreachable_nodes:
        parts.append("unreachable=" + format_nodes(actual_snapshot.unreachable_nodes))
    parts.append("actual=" + format_edges(actual))
    return False, "; ".join(parts)


def require_rpc_success(result: dict[str, Any] | None, action: str) -> None:
    """Raise an actionable error when a topology RPC did not succeed."""
    if result is None:
        raise RuntimeError(f"{action}: RPC failed or node is offline")
    if result.get("status") != "success":
        detail = result.get("error_message") or result.get("error") or result
        raise RuntimeError(f"{action}: RPC returned {detail!r}")


def format_nodes(nodes: Iterable[int]) -> str:
    """Format node ids for diagnostics."""
    return "[" + ", ".join(f"n{node}" for node in sorted(nodes)) + "]"


def format_edges(edges: Iterable[Edge]) -> str:
    """Format directed edges for diagnostics."""
    return (
        "["
        + ", ".join(f"n{source}->n{target}" for source, target in sorted(edges))
        + "]"
    )
