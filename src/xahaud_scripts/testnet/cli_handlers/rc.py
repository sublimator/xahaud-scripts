"""Runtime config DSL parser and handlers for network simulation.

Parses specs like:
    delay=200                           # all nodes, all peers
    n0:delay=500,jitter=50              # node 0 only
    n0@n2:drop=100,msg=proposal         # node 0 → peer n2, proposals only

DSL format: [NODE[@PEER]:]PARAM=VALUE[,PARAM=VALUE,...]
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import click
from rich.console import Console
from rich.table import Table

from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import NodeInfo
    from xahaud_scripts.testnet.protocols import RPCClient

logger = make_logger(__name__)
console = Console()

# Valid message type names (must match C++ side)
VALID_MSG_TYPES = frozenset(
    {
        "proposal",
        "validation",
        "transaction",
        "manifests",
        "ledger_data",
        "get_ledger",
    }
)

# DSL param names → RPC/env var key names
PARAM_MAP = {
    "delay": "send_delay_ms",
    "jitter": "send_delay_jitter_ms",
    "drop": "send_drop_pct",
}


@dataclass
class RuntimeConfigSpec:
    """Parsed runtime config specification."""

    node_id: int | None = None  # None = all nodes
    peer_id: int | None = None  # None = target "*"
    delay: int | None = None
    jitter: int | None = None
    drop: float | None = None
    msg: list[str] = field(default_factory=list)

    def to_rpc_config(self) -> dict[str, Any]:
        """Convert to RPC config dict (send_delay_ms etc.)."""
        cfg: dict[str, Any] = {}
        if self.delay is not None:
            cfg["send_delay_ms"] = self.delay
        if self.jitter is not None:
            cfg["send_delay_jitter_ms"] = self.jitter
        if self.drop is not None:
            cfg["send_drop_pct"] = self.drop
        if self.msg:
            cfg["message_types"] = self.msg
        return cfg


def parse_rc_spec(spec: str) -> RuntimeConfigSpec:
    """Parse a runtime config DSL spec string.

    Format: [NODE[@PEER]:]PARAM=VALUE[,PARAM=VALUE,...]

    Examples:
        delay=200
        delay=200,jitter=50
        n0:delay=500
        n0@n2:drop=100
        n0@n2:drop=100,msg=proposal+validation

    Args:
        spec: The DSL spec string.

    Returns:
        Parsed RuntimeConfigSpec.

    Raises:
        click.BadParameter: If the spec is invalid.
    """
    result = RuntimeConfigSpec()

    # Split target prefix from params
    if ":" in spec:
        # Check if the colon separates target from params
        # (not just part of a value)
        prefix, params_str = spec.split(":", 1)
        if re.match(r"^n\d+(@n\d+)?$", prefix):
            # Parse target: n0 or n0@n2
            if "@" in prefix:
                node_part, peer_part = prefix.split("@", 1)
                result.node_id = _parse_node_id(node_part)
                result.peer_id = _parse_node_id(peer_part)
            else:
                result.node_id = _parse_node_id(prefix)
        else:
            raise click.BadParameter(f"Invalid target: {prefix!r}. Use n0, n0@n2, etc.")
    else:
        params_str = spec

    # Parse params: delay=200,jitter=50,msg=proposal+validation
    if not params_str:
        raise click.BadParameter(f"No parameters in spec: {spec!r}")

    for part in params_str.split(","):
        if "=" not in part:
            raise click.BadParameter(
                f"Invalid param: {part!r}. Use param=value (e.g. delay=200)"
            )
        key, value = part.split("=", 1)

        if key == "delay":
            result.delay = _parse_int(value, "delay")
        elif key == "jitter":
            result.jitter = _parse_int(value, "jitter")
        elif key == "drop":
            result.drop = _parse_float(value, "drop")
            if not (0 <= result.drop <= 100):
                raise click.BadParameter("drop must be 0-100")
        elif key == "msg":
            types = value.split("+")
            for t in types:
                if t not in VALID_MSG_TYPES:
                    raise click.BadParameter(
                        f"Unknown message type: {t!r}. "
                        f"Valid: {', '.join(sorted(VALID_MSG_TYPES))}"
                    )
            result.msg = types
        else:
            raise click.BadParameter(
                f"Unknown param: {key!r}. Valid: delay, jitter, drop, msg"
            )

    return result


def _parse_node_id(s: str) -> int:
    """Parse 'n0' to 0."""
    if s.startswith("n") and s[1:].isdigit():
        return int(s[1:])
    raise click.BadParameter(f"Invalid node: {s!r}. Use n0, n1, etc.")


def _parse_int(s: str, name: str) -> int:
    try:
        return int(s)
    except ValueError:
        raise click.BadParameter(f"{name} must be an integer, got {s!r}") from None


def _parse_float(s: str, name: str) -> float:
    try:
        return float(s)
    except ValueError:
        raise click.BadParameter(f"{name} must be a number, got {s!r}") from None


# ---------------------------------------------------------------------------
# Env var building (for generate/run)
# ---------------------------------------------------------------------------


def resolve_peer_address(nodes: list[NodeInfo], peer_id: int) -> str:
    """Resolve a node ID to its peer address (127.0.0.1:<port>)."""
    for node in nodes:
        if node.id == peer_id:
            return f"127.0.0.1:{node.port_peer}"
    raise click.ClickException(f"Unknown peer node: n{peer_id}")


def reverse_resolve_peer(address: str, nodes: list[NodeInfo]) -> str | None:
    """Reverse-resolve a peer address to a node name, if possible."""
    for node in nodes:
        if address == f"127.0.0.1:{node.port_peer}":
            return f"n{node.id}"
    return None


def build_runtime_config_envs(
    specs: list[RuntimeConfigSpec],
    nodes: list[NodeInfo],
) -> dict[int, str]:
    """Build XAHAU_RUNTIME_CONFIG JSON per node from specs.

    Groups specs by node, resolves peer addresses, and builds
    the JSON env var value for each node.

    Args:
        specs: Parsed runtime config specs.
        nodes: Node info list (for peer address resolution).

    Returns:
        Dict mapping node_id -> JSON string for XAHAU_RUNTIME_CONFIG.
    """
    node_ids = [n.id for n in nodes]

    # Group specs by node. None node_id means "all nodes".
    # Per-node specs override global specs for the same target.
    # Per-peer specs are additional entries.
    #
    # Build: {node_id: {target_key: config_dict}}
    per_node: dict[int, dict[str, dict[str, Any]]] = {nid: {} for nid in node_ids}

    for spec in specs:
        target_key = (
            resolve_peer_address(nodes, spec.peer_id)
            if spec.peer_id is not None
            else "*"
        )
        cfg = spec.to_rpc_config()

        if spec.node_id is not None:
            # Specific node
            if spec.node_id not in per_node:
                raise click.ClickException(f"Unknown node: n{spec.node_id}")
            _merge_config(per_node[spec.node_id], target_key, cfg)
        else:
            # All nodes
            for nid in node_ids:
                _merge_config(per_node[nid], target_key, cfg)

    # Build JSON for nodes that have configs
    result: dict[int, str] = {}
    for nid, targets in per_node.items():
        if targets:
            result[nid] = json.dumps(targets)

    return result


def _merge_config(
    targets: dict[str, dict[str, Any]],
    target_key: str,
    cfg: dict[str, Any],
) -> None:
    """Merge config into a target entry, with later values overriding."""
    if target_key in targets:
        targets[target_key].update(cfg)
    else:
        targets[target_key] = dict(cfg)


# ---------------------------------------------------------------------------
# RPC handlers (for x-testnet rc)
# ---------------------------------------------------------------------------


def rc_show_handler(
    rpc_client: RPCClient,
    nodes: list[NodeInfo],
    node_ids: list[int] | None = None,
) -> None:
    """Query runtime_config on nodes and display as a Rich table."""
    target_ids = node_ids if node_ids is not None else [n.id for n in nodes]
    node_by_id = {n.id: n for n in nodes}

    table = Table(title="Runtime Config")
    table.add_column("Node", style="cyan", no_wrap=True)
    table.add_column("Peer", style="dim", no_wrap=True)
    table.add_column("Target", style="green")
    table.add_column("Delay ms", justify="right")
    table.add_column("Jitter ms", justify="right")
    table.add_column("Drop %", justify="right")
    table.add_column("Msg Types")

    any_data = False

    for nid in target_ids:
        node = node_by_id.get(nid)
        peer_label = f":{node.port_peer}" if node else ""

        result = rpc_client.runtime_config(nid)
        if result is None:
            table.add_row(f"n{nid}", peer_label, "[red]offline[/red]", "", "", "", "")
            continue

        configs = result.get("configs", {})
        if not configs:
            table.add_row(f"n{nid}", peer_label, "[dim]—[/dim]", "", "", "", "")
            continue

        any_data = True
        first = True
        for target, cfg in sorted(configs.items()):
            node_label = f"n{nid}" if first else ""
            peer_col = peer_label if first else ""
            first = False

            # Reverse-resolve peer address to node name
            if target == "*":
                target_label = "*"
            else:
                node_name = reverse_resolve_peer(target, nodes)
                target_label = node_name if node_name else target

            delay = cfg.get("send_delay_ms")
            jitter = cfg.get("send_delay_jitter_ms")
            drop = cfg.get("send_drop_pct")
            msg_types = cfg.get("message_types", [])

            table.add_row(
                node_label,
                peer_col,
                target_label,
                str(delay) if delay is not None else "—",
                str(jitter) if jitter is not None else "—",
                f"{drop}" if drop is not None else "—",
                "+".join(msg_types) if msg_types else "all",
            )

    console.print(table)

    if not any_data:
        console.print("[dim]No runtime config active on any node.[/dim]")


def rc_set_handler(
    rpc_client: RPCClient,
    nodes: list[NodeInfo],
    specs: list[RuntimeConfigSpec],
) -> None:
    """Parse specs and send runtime_config set RPCs."""
    node_ids = [n.id for n in nodes]

    # Group specs into RPC calls: {node_id: {target: config}}
    rpc_calls: dict[int, dict[str, dict[str, Any]]] = {}

    for spec in specs:
        target_key = (
            resolve_peer_address(nodes, spec.peer_id)
            if spec.peer_id is not None
            else "*"
        )
        cfg = spec.to_rpc_config()

        targets = [spec.node_id] if spec.node_id is not None else node_ids
        for nid in targets:
            if nid not in rpc_calls:
                rpc_calls[nid] = {}
            _merge_config(rpc_calls[nid], target_key, cfg)

    # Send RPCs in parallel
    with ThreadPoolExecutor(max_workers=len(rpc_calls) or 1) as pool:
        futures = {}
        for nid, target_configs in rpc_calls.items():
            params = {"set": target_configs}
            futures[pool.submit(rpc_client.runtime_config, nid, params)] = nid

        for future in as_completed(futures):
            nid = futures[future]
            result = future.result()
            if result is None:
                console.print(f"[red]n{nid}: failed (offline?)[/red]")
            else:
                console.print(f"[green]n{nid}: ok[/green]")


def rc_clear_handler(
    rpc_client: RPCClient,
    nodes: list[NodeInfo],
    node_ids: list[int] | None = None,
    peer_ids: list[int] | None = None,
) -> None:
    """Send runtime_config clear RPCs."""
    target_nids = node_ids if node_ids is not None else [n.id for n in nodes]

    with ThreadPoolExecutor(max_workers=len(target_nids) or 1) as pool:
        futures = {}
        for nid in target_nids:
            if peer_ids is not None:
                # Clear specific peer targets
                targets = [resolve_peer_address(nodes, pid) for pid in peer_ids]
                params: dict[str, Any] = {"clear": targets}
            else:
                params = {"clear_all": True}
            futures[pool.submit(rpc_client.runtime_config, nid, params)] = nid

        for future in as_completed(futures):
            nid = futures[future]
            result = future.result()
            if result is None:
                console.print(f"[red]n{nid}: failed (offline?)[/red]")
            else:
                console.print(f"[green]n{nid}: cleared[/green]")
