"""Breadth-first crawl of the peer overlay /crawl endpoint.

Every xahaud/rippled peer port serves an HTTPS ``/crawl`` endpoint (self-signed
cert) listing its active peers, each with a ``public_key``, ``version`` and
sometimes an ``ip``/``port``. We start from seed hubs, BFS every reachable
node, and union peers by ``public_key`` so each node is counted once. The
result is the distribution of ``version`` strings across unique nodes.

Concurrency uses a thread pool: worker threads only do HTTP (``_fetch``); all
shared-state mutation happens on the driver thread (``_merge``), so no locks.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

import requests
import urllib3

# Self-signed certs on peer ports — we never send credentials, only GET /crawl.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

Endpoint = tuple[str, int]


@dataclass
class Node:
    """A unique overlay node, keyed by its public_key."""

    public_key: str
    version: str | None = None
    endpoints: set[Endpoint] = field(default_factory=set)
    has_endpoint: bool = False  # did anyone expose a contactable ip/port?


@dataclass
class CrawlResult:
    """Parsed payload from one node's /crawl (returned by worker threads)."""

    server_header: str | None
    peers: list[dict]


@dataclass
class CrawlStats:
    """Live progress snapshot passed to the on_progress callback."""

    queried: int
    reachable: int
    unreachable: int
    in_flight: int
    nodes: int


def parse_seed(seed: str, default_port: int) -> Endpoint:
    """Parse 'host', 'host:port', '[ipv6]:port' or 'host port' into (host, port)."""
    seed = seed.strip()
    m = re.match(r"^\[(?P<h>[^\]]+)\](?::(?P<p>\d+))?$", seed)  # [ipv6]:port
    if m:
        return m.group("h"), int(m.group("p") or default_port)
    if " " in seed:  # "host 21337" config style
        host, _, port = seed.partition(" ")
        return host.strip(), int(port.strip() or default_port)
    if seed.count(":") == 1:  # host:port
        host, _, port = seed.partition(":")
        return host, int(port or default_port)
    return seed, default_port


def coerce_port(raw: object, default_port: int) -> int:
    """Crawl reports port as int (inbound) or string (outbound)."""
    if isinstance(raw, bool):  # bool is an int subclass; treat as garbage
        return default_port
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return default_port
    return default_port


def short_version(v: str | None) -> str:
    """'xahaud-2026.6.21-release+3350' -> '2026.6.21-release+3350'."""
    if not v:
        return "(unknown)"
    return re.sub(r"^(xahaud|rippled)-", "", v)


def release_date(v: str | None) -> str:
    """The YYYY.M.D portion of a version, ignoring build/channel suffix."""
    short = short_version(v)
    m = re.match(r"^(\d{4}\.\d+\.\d+)", short)
    return m.group(1) if m else short


def _release_key(date: str) -> tuple[int, int, int]:
    m = re.match(r"^(\d{4})\.(\d+)\.(\d+)", date)
    if not m:
        return (0, 0, 0)
    year, month, day = m.groups()
    return (int(year), int(month), int(day))


class Crawler:
    def __init__(
        self,
        *,
        default_port: int,
        concurrency: int = 64,
        timeout: float = 10.0,
        max_nodes: int = 5000,
        probe_default_port: bool = True,
    ) -> None:
        self.default_port = default_port
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_nodes = max_nodes
        self.probe_default_port = probe_default_port

        self.nodes: dict[str, Node] = {}
        self.visited: set[Endpoint] = set()
        self.ok = 0
        self.failed = 0

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "xahau-inspect-net/1.0"})

    def _fetch(self, ep: Endpoint) -> CrawlResult | None:
        """Worker-thread side: HTTP only, no shared-state writes."""
        host, port = ep
        url = f"https://{host}:{port}/crawl"
        try:
            r = self._session.get(url, timeout=self.timeout, verify=False)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        active = (data.get("overlay") or {}).get("active") or []
        return CrawlResult(server_header=r.headers.get("server"), peers=active)

    def _enqueue(self, ep: Endpoint) -> bool:
        """Driver-thread side: mark an endpoint visited if it's new and under cap."""
        if ep in self.visited or len(self.visited) >= self.max_nodes:
            return False
        self.visited.add(ep)
        return True

    def _merge(self, result: CrawlResult) -> list[Endpoint]:
        """Driver-thread side: fold peers into nodes, return new endpoints."""
        new: list[Endpoint] = []
        for peer in result.peers:
            pk = peer.get("public_key")
            if not pk:
                continue
            node = self.nodes.get(pk)
            if node is None:
                node = self.nodes[pk] = Node(public_key=pk)
            ver = peer.get("version")
            if ver and not node.version:
                node.version = ver

            ip = peer.get("ip")
            if not ip:
                continue
            node.has_endpoint = True
            if peer.get("port") is not None:
                port = coerce_port(peer.get("port"), self.default_port)
            elif self.probe_default_port:
                port = self.default_port
            else:
                continue
            node.endpoints.add((ip, port))
            new.append((ip, port))
        return new

    def crawl(
        self,
        seeds: list[Endpoint],
        on_progress: Callable[[CrawlStats], None] | None = None,
    ) -> None:
        """Run the BFS crawl to completion (or until max_nodes is hit)."""
        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futures: dict[Future[CrawlResult | None], Endpoint] = {}

            def submit(ep: Endpoint) -> None:
                if self._enqueue(ep):
                    futures[ex.submit(self._fetch, ep)] = ep

            for ep in seeds:
                submit(ep)

            while futures:
                done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                for fut in done:
                    futures.pop(fut)
                    result = fut.result()
                    if result is None:
                        self.failed += 1
                    else:
                        self.ok += 1
                        for ep in self._merge(result):
                            submit(ep)
                if on_progress:
                    on_progress(
                        CrawlStats(
                            queried=len(self.visited),
                            reachable=self.ok,
                            unreachable=self.failed,
                            in_flight=len(futures),
                            nodes=len(self.nodes),
                        )
                    )

    # --- aggregation (pure reads over self.nodes) ---

    def version_counts(self) -> Counter[str]:
        return Counter(short_version(n.version) for n in self.nodes.values())

    def release_rollup(self) -> list[tuple[str, int]]:
        """(release-date, node-count), newest release first."""
        by_date: dict[str, int] = defaultdict(int)
        for n in self.nodes.values():
            by_date[release_date(n.version)] += 1
        return [
            (d, by_date[d]) for d in sorted(by_date, key=_release_key, reverse=True)
        ]

    @property
    def contactable(self) -> int:
        return sum(1 for n in self.nodes.values() if n.has_endpoint)
