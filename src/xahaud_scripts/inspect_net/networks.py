"""Network presets shared by the inspect-net subcommands."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Network:
    """A known network: its public JSON-RPC endpoint and overlay seed hubs.

    Attributes:
        name: Short key (e.g. "mainnet").
        rpc_url: Public JSON-RPC endpoint for amendment/server queries.
        seeds: Overlay crawl seed hubs (host or host:port).
        peer_port: Default peer port used when a crawled peer hides its own.
    """

    name: str
    rpc_url: str
    seeds: tuple[str, ...]
    peer_port: int


NETWORKS: dict[str, Network] = {
    "mainnet": Network(
        name="mainnet",
        rpc_url="https://xahau.network",
        seeds=("bacab.alloy.ee", "hubs.xahau.as16089.net"),
        peer_port=21337,
    ),
    "testnet": Network(
        name="testnet",
        rpc_url="https://xahau-test.net",
        seeds=(
            "79.110.60.121",
            "79.110.60.122",
            "79.110.60.124",
            "79.110.60.125",
        ),
        peer_port=21338,
    ),
    "xrpl": Network(
        name="xrpl",
        rpc_url="https://s1.ripple.com:51234",
        seeds=("r.ripple.com", "zaphod.alloy.ee", "sahyadri.isrdc.in"),
        peer_port=51235,
    ),
}

# Networks whose `server_definitions` returns the full amendment table to
# anonymous callers. (rippled does not, so XRPL is crawl-only here.)
AMENDMENT_NETWORKS: tuple[str, ...] = ("mainnet", "testnet")
