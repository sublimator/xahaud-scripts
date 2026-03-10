"""Scenario testing primitives for x-testnet.

Provides timing anchors (Marker, Range, Operation) and log assertions
that work against live networks or archived snapshots. Reuses the
logs_search backend for all log operations.

Scenario scripts define:

    async def scenario(ctx):
        await ctx.wait_for_ledger(10)
        pre = ctx.mark("pre-kill")
        await ctx.sleep(5)
        post = ctx.mark("post-kill")

        window = pre.until(post)
        ctx.assert_log("LedgerConsensus", within=window, nodes=[0, 3, 4])
        ctx.assert_not_log("error", within=window)

Run via:

    x-testnet run --scenario-script my_scenario.py
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import re
import sys
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xahaud_scripts.testnet.network import TestNetwork
    from xahaud_scripts.testnet.protocols import RPCClient
    from xahaud_scripts.testnet.txn_generator import TxnGenerator

from xahaud_scripts.testnet.cli_handlers.logs_search import (
    LogEntry,
    merge_log_streams,
)
from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)


# ---------------------------------------------------------------------------
# Core timing primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Marker:
    """A named point in time, used as an anchor for log slicing."""

    name: str
    utc: datetime
    monotonic_ns: int

    def until(self, other: Marker) -> Range:
        """Create a Range from this marker to another."""
        return Range(start=self, end=other)

    def since(self, duration: timedelta) -> Range:
        """Create a Range from (self - duration) to self."""
        earlier = Marker(
            name=f"{self.name}-{duration}",
            utc=self.utc - duration,
            monotonic_ns=self.monotonic_ns - int(duration.total_seconds() * 1e9),
        )
        return Range(start=earlier, end=self)


@dataclass(frozen=True)
class Range:
    """A time window between two markers."""

    start: Marker
    end: Marker

    @property
    def duration(self) -> timedelta:
        return self.end.utc - self.start.utc


@dataclass(frozen=True)
class Operation[T]:
    """An executed step with timing and result."""

    kind: str
    started: Marker
    ended: Marker
    status: str
    result: T

    @property
    def window(self) -> Range:
        return Range(self.started, self.ended)


def now_marker(name: str) -> Marker:
    """Create a Marker anchored to the current time."""
    return Marker(
        name=name,
        utc=datetime.utcnow(),
        monotonic_ns=_time.monotonic_ns(),
    )


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------


def _get_validated_ledger(rpc: RPCClient, node_id: int = 0) -> int | None:
    """Get the validated ledger index from a node, or None if unreachable."""
    info = rpc.server_info(node_id)
    if not info or "info" not in info:
        return None
    validated = info["info"].get("validated_ledger", {})
    seq = validated.get("seq")
    return int(seq) if seq else None


# ---------------------------------------------------------------------------
# Wait primitives
# ---------------------------------------------------------------------------


async def wait_for_ledger_close(
    rpc: RPCClient,
    *,
    node_id: int = 0,
    timeout: float = 120,
    poll_interval: float = 1.0,
    name: str | None = None,
) -> Operation[int]:
    """Wait for the next ledger close event.

    Polls until the validated ledger advances. Handles the node not being
    ready yet (no validated ledger) — keeps polling until one appears and
    then waits for it to advance.

    Returns:
        Operation with the new ledger index as result.

    Raises:
        TimeoutError: If no close observed within timeout.
    """
    label = name or "ledger-close"
    started = now_marker(f"{label}-start")
    baseline = _get_validated_ledger(rpc, node_id)

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        current = _get_validated_ledger(rpc, node_id)
        if current is not None and (baseline is None or current > baseline):
            ended = now_marker(f"{label}-end")
            return Operation(
                kind="wait_for_ledger_close",
                started=started,
                ended=ended,
                status="ok",
                result=current,
            )

    raise TimeoutError(
        f"Timed out waiting for ledger close ({timeout}s) on node {node_id}"
    )


async def wait_for_ledger(
    rpc: RPCClient,
    target: int,
    *,
    node_id: int = 0,
    timeout: float = 120,
    poll_interval: float = 1.0,
    name: str | None = None,
) -> Operation[int]:
    """Wait until validated ledger reaches target index.

    Args:
        rpc: RPC client to poll.
        target: Ledger index to wait for.
        node_id: Node to poll (default: 0).
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.
        name: Optional name for the operation markers.

    Returns:
        Operation with the reached ledger index as result.

    Raises:
        TimeoutError: If target not reached within timeout.
    """
    label = name or f"ledger-{target}"
    started = now_marker(f"{label}-start")

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        current = _get_validated_ledger(rpc, node_id)
        if current is not None and current >= target:
            ended = now_marker(f"{label}-end")
            return Operation(
                kind="wait_for_ledger",
                started=started,
                ended=ended,
                status="ok",
                result=current,
            )
        await asyncio.sleep(poll_interval)

    raise TimeoutError(
        f"Timed out waiting for ledger {target} ({timeout}s) on node {node_id}"
    )


async def wait_for_ledgers(
    rpc: RPCClient,
    count: int,
    *,
    node_id: int = 0,
    timeout: float = 120,
    poll_interval: float = 1.0,
    name: str | None = None,
) -> Operation[int]:
    """Wait for N more ledgers to close from the current position.

    Args:
        rpc: RPC client to poll.
        count: Number of additional ledgers to wait for.
        node_id: Node to poll (default: 0).
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.
        name: Optional name for the operation markers.

    Returns:
        Operation with the reached ledger index as result.

    Raises:
        TimeoutError: If not reached within timeout.
    """
    current = _get_validated_ledger(rpc, node_id)
    if current is None:
        raise RuntimeError(f"Cannot get current ledger from node {node_id}")

    target = current + count
    return await wait_for_ledger(
        rpc,
        target,
        node_id=node_id,
        timeout=timeout,
        poll_interval=poll_interval,
        name=name or f"ledgers-+{count}",
    )


async def wait_for_nodes(
    fn: Callable[[int], bool],
    node_ids: list[int],
    *,
    timeout: float = 120,
    poll_interval: float = 2.0,
    name: str | None = None,
) -> Operation[bool]:
    """Poll a per-node function until it returns True for all nodes.

    Args:
        fn: Callable that takes a node_id and returns True/False.
        node_ids: List of node IDs to check.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.
        name: Optional name for the operation markers.

    Returns:
        Operation with True as result.

    Raises:
        TimeoutError: If not all nodes pass within timeout.
    """
    label = name or "wait-for-nodes"
    started = now_marker(f"{label}-start")

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if all(fn(nid) for nid in node_ids):
            ended = now_marker(f"{label}-end")
            return Operation(
                kind="wait_for_nodes",
                started=started,
                ended=ended,
                status="ok",
                result=True,
            )
        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"wait_for_nodes({label}) timed out after {timeout}s")


async def wait_for[T](
    fn: Callable[[], T | None],
    *,
    timeout: float = 120,
    poll_interval: float = 2.0,
    name: str | None = None,
) -> Operation[T]:
    """Poll a function until it returns a truthy value.

    Args:
        fn: Callable that returns a truthy value on success, or None/falsy to keep waiting.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.
        name: Optional name for the operation markers.

    Returns:
        Operation with the truthy return value as result.

    Raises:
        TimeoutError: If fn never returns truthy within timeout.
    """
    label = name or "wait-for"
    started = now_marker(f"{label}-start")

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        result = fn()
        if result:
            ended = now_marker(f"{label}-end")
            return Operation(
                kind="wait_for",
                started=started,
                ended=ended,
                status="ok",
                result=result,
            )
        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"wait_for({label}) timed out after {timeout}s")


# ---------------------------------------------------------------------------
# Log assertion results
# ---------------------------------------------------------------------------


@dataclass
class LogSearchResult:
    """Result of a log search operation."""

    pattern: str
    matches: list[LogEntry]
    node_filter: list[int] | None
    time_start: datetime | None
    time_end: datetime | None

    @property
    def count(self) -> int:
        return len(self.matches)

    @property
    def found(self) -> bool:
        return len(self.matches) > 0

    def by_node(self, node_id: int) -> list[LogEntry]:
        """Filter matches to a specific node."""
        return [e for e in self.matches if e.node_id == node_id]


# ---------------------------------------------------------------------------
# Log search (reuses logs_search backend)
# ---------------------------------------------------------------------------


def _get_log_files(
    base_dir: Path,
    nodes: list[int] | None = None,
) -> list[Path]:
    """Get debug.log paths for specified nodes."""
    node_dirs = sorted(base_dir.glob("n[0-9]*"))
    if nodes is not None:
        node_dirs = [d for d in node_dirs if int(d.name[1:]) in nodes]

    log_files = []
    for node_dir in node_dirs:
        log_file = node_dir / "debug.log"
        if log_file.exists():
            log_files.append(log_file)
    return log_files


def search_logs(
    base_dir: Path,
    pattern: str,
    *,
    within: Range | None = None,
    since: Marker | None = None,
    until: Marker | None = None,
    nodes: list[int] | None = None,
    limit: int | None = None,
) -> LogSearchResult:
    """Search logs with optional time window and node filtering.

    Args:
        base_dir: Network directory containing n0/, n1/, etc.
        pattern: Regex pattern to search for.
        within: Time range to search within.
        since: Start searching from this marker's time.
        until: Stop searching at this marker's time.
        nodes: Node IDs to search (default: all).
        limit: Maximum matches to return.

    Returns:
        LogSearchResult with matching entries.
    """
    time_start: datetime | None = None
    time_end: datetime | None = None

    if within:
        time_start = within.start.utc
        time_end = within.end.utc
    if since:
        time_start = since.utc
    if until:
        time_end = until.utc

    log_files = _get_log_files(base_dir, nodes)
    if not log_files:
        return LogSearchResult(
            pattern=pattern,
            matches=[],
            node_filter=nodes,
            time_start=time_start,
            time_end=time_end,
        )

    regex = re.compile(pattern)
    matches: list[LogEntry] = []

    for entry in merge_log_streams(
        log_files, regex, time_start=time_start, time_end=time_end
    ):
        matches.append(entry)
        if limit and len(matches) >= limit:
            break

    return LogSearchResult(
        pattern=pattern,
        matches=matches,
        node_filter=nodes,
        time_start=time_start,
        time_end=time_end,
    )


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


class AssertionError(Exception):
    """Raised when a scenario assertion fails."""

    def __init__(self, message: str, result: LogSearchResult | None = None) -> None:
        super().__init__(message)
        self.result = result


def assert_log(
    base_dir: Path,
    pattern: str,
    *,
    within: Range | None = None,
    since: Marker | None = None,
    until: Marker | None = None,
    nodes: list[int] | None = None,
    min_count: int = 1,
) -> LogSearchResult:
    """Assert that a pattern appears in logs.

    Args:
        base_dir: Network directory.
        pattern: Regex pattern that must appear.
        within: Time range to search within.
        since: Start from this marker.
        until: End at this marker.
        nodes: Node IDs to search.
        min_count: Minimum number of matches required.

    Returns:
        LogSearchResult on success.

    Raises:
        AssertionError: If pattern not found or count too low.
    """
    result = search_logs(
        base_dir,
        pattern,
        within=within,
        since=since,
        until=until,
        nodes=nodes,
    )
    if result.count < min_count:
        window_desc = ""
        if within:
            window_desc = f" within {within.start.name}..{within.end.name}"
        elif since:
            window_desc = f" since {since.name}"
        node_desc = f" on nodes {nodes}" if nodes else ""
        raise AssertionError(
            f"Expected >= {min_count} matches for /{pattern}/{node_desc}{window_desc}, "
            f"got {result.count}",
            result,
        )
    return result


def assert_not_log(
    base_dir: Path,
    pattern: str,
    *,
    within: Range | None = None,
    since: Marker | None = None,
    until: Marker | None = None,
    nodes: list[int] | None = None,
) -> LogSearchResult:
    """Assert that a pattern does NOT appear in logs.

    Raises:
        AssertionError: If pattern is found.
    """
    result = search_logs(
        base_dir,
        pattern,
        within=within,
        since=since,
        until=until,
        nodes=nodes,
    )
    if result.found:
        window_desc = ""
        if within:
            window_desc = f" within {within.start.name}..{within.end.name}"
        elif since:
            window_desc = f" since {since.name}"
        node_desc = f" on nodes {nodes}" if nodes else ""
        raise AssertionError(
            f"Expected no matches for /{pattern}/{node_desc}{window_desc}, "
            f"got {result.count}",
            result,
        )
    return result


def assert_log_order(
    base_dir: Path,
    patterns: list[str],
    *,
    within: Range | None = None,
    since: Marker | None = None,
    until: Marker | None = None,
    nodes: list[int] | None = None,
) -> list[LogSearchResult]:
    """Assert that patterns appear in order in logs.

    Each pattern must have at least one match, and the first match of
    each pattern must come after the first match of the previous pattern.

    Args:
        base_dir: Network directory.
        patterns: Ordered list of regex patterns.
        within/since/until/nodes: Filtering options.

    Returns:
        List of LogSearchResult for each pattern.

    Raises:
        AssertionError: If any pattern is missing or order is wrong.
    """
    results = []
    last_ts: datetime | None = None

    for i, pattern in enumerate(patterns):
        result = search_logs(
            base_dir,
            pattern,
            within=within,
            since=since,
            until=until,
            nodes=nodes,
        )
        if not result.found:
            raise AssertionError(
                f"Pattern {i + 1}/{len(patterns)} /{pattern}/ not found",
                result,
            )

        first_ts = result.matches[0].timestamp
        if last_ts is not None and first_ts < last_ts:
            prev_pattern = patterns[i - 1]
            raise AssertionError(
                f"Order violation: /{pattern}/ ({first_ts}) appeared before "
                f"/{prev_pattern}/ ({last_ts})",
                result,
            )
        last_ts = first_ts
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# ScenarioContext — the "we call you" interface
# ---------------------------------------------------------------------------


class ScenarioContext:
    """Context passed to scenario scripts.

    Wraps the TestNetwork and all scenario primitives into a single
    object that the framework passes to ``async def scenario(ctx)``.
    """

    def __init__(self, network: TestNetwork) -> None:
        self._network = network

    def log(self, message: str) -> None:
        """Log a message from the scenario script."""
        logger.info(message)

    @property
    def rpc(self) -> RPCClient:
        return self._network.rpc_client

    @property
    def base_dir(self) -> Path:
        return self._network.base_dir

    @property
    def node_count(self) -> int:
        return self._network.config.node_count

    # -- RPC helpers -------------------------------------------------------

    def validated_ledger_index(self, node_id: int = 0) -> int | None:
        """Get the validated ledger index from a node, or None if unreachable."""
        return _get_validated_ledger(self.rpc, node_id)

    def ledger(
        self,
        ledger_index: str | int = "validated",
        *,
        node_id: int = 0,
        transactions: bool = False,
        expand: bool = True,
    ) -> dict[str, Any] | None:
        """Fetch a ledger from a node.

        Args:
            ledger_index: Ledger seq or "validated"/"current"/"closed".
            node_id: Node to query (default: 0).
            transactions: Include transactions (default: False).
            expand: Expand transaction details (default: True).

        Returns:
            The ledger result dict, or None if query failed.
        """
        return self.rpc.ledger(
            node_id,
            ledger_index=ledger_index,
            transactions=transactions,
            expand=expand,
        )

    # -- Transaction generator ---------------------------------------------

    def txn_generator(
        self,
        min_txns: int = 3,
        max_txns: int = 10,
        start_ledger: int = 0,
        **kwargs: Any,
    ) -> TxnGenerator:
        """Create a TxnGenerator for this network.

        The generator must be started with ``await gen.start()`` and
        stopped with ``await gen.stop()``.

        Args:
            min_txns: Minimum txns per ledger.
            max_txns: Maximum txns per ledger.
            start_ledger: Don't submit until this ledger index.
            **kwargs: Additional TxnGeneratorConfig fields.

        Returns:
            Configured (but not started) TxnGenerator instance.
        """
        from xahaud_scripts.testnet.txn_generator import (
            TxnGenerator,
            TxnGeneratorConfig,
        )

        ws_url = f"ws://127.0.0.1:{self._network.config.base_port_ws}"
        config = TxnGeneratorConfig(
            min_txns=min_txns,
            max_txns=max_txns,
            start_ledger=start_ledger,
            **kwargs,
        )
        return TxnGenerator(ws_url, config=config)

    # -- Timing ------------------------------------------------------------

    def mark(self, name: str) -> Marker:
        """Create a Marker anchored to the current time."""
        return now_marker(name)

    async def sleep(
        self, seconds: float, *, name: str | None = None
    ) -> Operation[float]:
        """Async sleep, returning an Operation whose window spans the delay.

        Args:
            seconds: Duration to sleep.
            name: Optional name for the markers (default: "sleep-{seconds}s").

        Returns:
            Operation with the sleep duration as result and a .window
            spanning the start/end of the sleep.
        """
        label = name or f"sleep-{seconds}s"
        started = now_marker(f"{label}-start")
        await asyncio.sleep(seconds)
        ended = now_marker(f"{label}-end")
        return Operation(
            kind="sleep",
            started=started,
            ended=ended,
            status="ok",
            result=seconds,
        )

    async def pause(self, message: str = "Press Enter to continue...") -> str:
        """Pause scenario and wait for user input.

        Useful for debugging (inspect state mid-scenario) or manual steps.
        Returns whatever the user typed before pressing Enter.
        """
        logger.info(f"PAUSED: {message}")
        result = await asyncio.get_event_loop().run_in_executor(
            None, input, f"\n>> {message} "
        )
        logger.info(f"Resumed (input: {result!r})")
        return result

    # -- Wait primitives ---------------------------------------------------

    async def wait_for_ledger_close(
        self,
        *,
        node_id: int = 0,
        timeout: float = 120,
        poll_interval: float = 1.0,
        name: str | None = None,
    ) -> Operation[int]:
        """Wait for the next ledger close event."""
        return await wait_for_ledger_close(
            self.rpc,
            node_id=node_id,
            timeout=timeout,
            poll_interval=poll_interval,
            name=name,
        )

    async def wait_for_ledger(
        self,
        target: int,
        *,
        node_id: int = 0,
        timeout: float = 120,
        poll_interval: float = 1.0,
        name: str | None = None,
    ) -> Operation[int]:
        """Wait until validated ledger reaches target index."""
        return await wait_for_ledger(
            self.rpc,
            target,
            node_id=node_id,
            timeout=timeout,
            poll_interval=poll_interval,
            name=name,
        )

    async def wait_for_ledgers(
        self,
        count: int,
        *,
        node_id: int = 0,
        timeout: float = 120,
        poll_interval: float = 1.0,
        name: str | None = None,
    ) -> Operation[int]:
        """Wait for N more ledgers to close from the current position."""
        return await wait_for_ledgers(
            self.rpc,
            count,
            node_id=node_id,
            timeout=timeout,
            poll_interval=poll_interval,
            name=name,
        )

    def _resolve_nodes(
        self,
        nodes: list[int] | None = None,
        exclude_nodes: list[int] | None = None,
    ) -> list[int]:
        """Resolve node targeting to a concrete list of node IDs."""
        target = nodes if nodes is not None else list(range(self.node_count))
        if exclude_nodes:
            target = [n for n in target if n not in exclude_nodes]
        return target

    async def wait_for_nodes(
        self,
        fn: Callable[[int], bool],
        *,
        nodes: list[int] | None = None,
        exclude_nodes: list[int] | None = None,
        timeout: float = 120,
        poll_interval: float = 2.0,
        name: str | None = None,
    ) -> Operation[bool]:
        """Wait until a per-node predicate passes on all targeted nodes."""
        return await wait_for_nodes(
            fn,
            self._resolve_nodes(nodes, exclude_nodes),
            timeout=timeout,
            poll_interval=poll_interval,
            name=name,
        )

    async def wait_for_nodes_down(
        self,
        *,
        nodes: list[int] | None = None,
        exclude_nodes: list[int] | None = None,
        timeout: float = 300,
        poll_interval: float = 2.0,
        name: str | None = None,
    ) -> Operation[bool]:
        """Wait until nodes stop responding to RPC.

        Args:
            nodes: Specific node IDs to wait for (default: all).
            exclude_nodes: Node IDs to skip.
        """
        rpc = self.rpc
        return await wait_for_nodes(
            lambda nid: rpc.server_info(nid) is None,
            self._resolve_nodes(nodes, exclude_nodes),
            timeout=timeout,
            poll_interval=poll_interval,
            name=name or "node-down",
        )

    # -- Network control ---------------------------------------------------

    def stop_node(self, node_id: int) -> bool:
        """Stop a single node (sends Ctrl+C via launcher)."""
        return self._network.stop_nodes([node_id]).get(node_id, False)

    def start_node(self, node_id: int) -> bool:
        """Start a single node (re-sends launch command via launcher)."""
        return self._network.start_nodes([node_id]).get(node_id, False)

    def stop_nodes(self, *, nodes: list[int]) -> dict[int, bool]:
        """Stop multiple nodes (sends Ctrl+C via launcher).

        Args:
            nodes: Node IDs to stop (required, no default).

        Returns:
            Dict mapping node_id -> success.
        """
        return self._network.stop_nodes(nodes)

    def start_nodes(self, *, nodes: list[int]) -> dict[int, bool]:
        """Start multiple nodes (re-sends launch command via launcher).

        Args:
            nodes: Node IDs to start (required, no default).

        Returns:
            Dict mapping node_id -> success.
        """
        return self._network.start_nodes(nodes)

    def capture_output(self, node_id: int, lines: int = 1000) -> str | None:
        """Capture terminal output from a node."""
        return self._network.capture_output(node_id, lines)

    def assert_exit_status(
        self,
        expected: int,
        *,
        nodes: list[int] | None = None,
        exclude_nodes: list[int] | None = None,
        timeout: float = 10,
    ) -> None:
        """Assert that nodes exited with the expected status.

        Polls for the exit status file (written by _xrun) since there
        may be a brief delay between RPC going down and the process
        fully exiting.

        Raises:
            AssertionError: If any node's exit status doesn't match.
        """
        import time

        for nid in self._resolve_nodes(nodes, exclude_nodes):
            deadline = _time.monotonic() + timeout
            status: int | None = None
            while _time.monotonic() < deadline:
                status = self._network.get_exit_status(nid)
                if status is not None:
                    break
                time.sleep(0.5)
            if status is None:
                raise AssertionError(
                    f"Node {nid}: could not read exit status after {timeout}s"
                )
            if status != expected:
                raise AssertionError(
                    f"Node {nid}: expected exit status {expected}, got {status}"
                )

    def log_level(
        self,
        partition: str,
        severity: str,
        *,
        nodes: list[int] | None = None,
        exclude_nodes: list[int] | None = None,
    ) -> None:
        """Set log level for a partition across nodes.

        Args:
            partition: Log partition name (e.g., "Validations", "Amendments").
            severity: Log severity (e.g., "trace", "debug", "info", "warning").
            nodes: Specific node IDs to target (default: all).
            exclude_nodes: Node IDs to skip.
        """
        for nid in self._resolve_nodes(nodes, exclude_nodes):
            self.rpc.log_level(nid, partition, severity)

    def feature(
        self,
        feature_name: str,
        *,
        vetoed: bool | None = None,
        nodes: list[int] | None = None,
        exclude_nodes: list[int] | None = None,
    ) -> dict[int, dict[str, Any] | None]:
        """Vote on or query an amendment across nodes.

        Args:
            feature_name: Amendment name or hash.
            vetoed: True to reject, False to accept, None to query.
            nodes: Specific node IDs to target (default: all).
            exclude_nodes: Node IDs to skip.

        Returns:
            Dict mapping node_id -> RPC result (or None on failure).
        """
        results: dict[int, dict[str, Any] | None] = {}
        for nid in self._resolve_nodes(nodes, exclude_nodes):
            results[nid] = self.rpc.feature(nid, feature_name, vetoed=vetoed)
        return results

    def feature_check(
        self, feature_name: str, node_id: int = 0
    ) -> dict[str, Any] | None:
        """Query amendment status on a single node.

        Returns:
            Dict with 'enabled', 'vetoed', 'supported' keys, or None.
        """
        result = self.rpc.feature(node_id, feature_name)
        if not result:
            return None
        # Response is {hash: {name, enabled, supported, vetoed}}
        for feat in result.values():
            if isinstance(feat, dict):
                return feat
        return None

    async def wait_for_feature(
        self,
        feature_name: str,
        check: Callable[[dict[str, Any]], bool],
        *,
        nodes: list[int] | None = None,
        exclude_nodes: list[int] | None = None,
        timeout: float = 120,
        poll_interval: float = 2.0,
        name: str | None = None,
    ) -> Operation[bool]:
        """Wait until a feature check passes on all targeted nodes.

        Args:
            feature_name: Amendment name or hash.
            check: Predicate applied to each node's feature status dict.
                   Must return True on all targeted nodes to succeed.
            nodes: Specific node IDs to target (default: all).
            exclude_nodes: Node IDs to skip.
        """

        def _check_node(nid: int) -> bool:
            status = self.feature_check(feature_name, node_id=nid)
            return bool(status and check(status))

        return await wait_for_nodes(
            _check_node,
            self._resolve_nodes(nodes, exclude_nodes),
            timeout=timeout,
            poll_interval=poll_interval,
            name=name or f"feature-{feature_name}",
        )

    async def wait_for[T](
        self,
        fn: Callable[[], T | None],
        *,
        timeout: float = 120,
        poll_interval: float = 2.0,
        name: str | None = None,
    ) -> Operation[T]:
        """Poll a function until it returns a truthy value."""
        return await wait_for(
            fn, timeout=timeout, poll_interval=poll_interval, name=name
        )

    # -- Log operations ----------------------------------------------------

    def search_logs(
        self,
        pattern: str,
        *,
        within: Range | None = None,
        since: Marker | None = None,
        until: Marker | None = None,
        nodes: list[int] | None = None,
        limit: int | None = None,
    ) -> LogSearchResult:
        """Search logs with optional time window and node filtering."""
        return search_logs(
            self.base_dir,
            pattern,
            within=within,
            since=since,
            until=until,
            nodes=nodes,
            limit=limit,
        )

    def assert_log(
        self,
        pattern: str,
        *,
        within: Range | None = None,
        since: Marker | None = None,
        until: Marker | None = None,
        nodes: list[int] | None = None,
        min_count: int = 1,
    ) -> LogSearchResult:
        """Assert that a pattern appears in logs."""
        return assert_log(
            self.base_dir,
            pattern,
            within=within,
            since=since,
            until=until,
            nodes=nodes,
            min_count=min_count,
        )

    def assert_not_log(
        self,
        pattern: str,
        *,
        within: Range | None = None,
        since: Marker | None = None,
        until: Marker | None = None,
        nodes: list[int] | None = None,
    ) -> LogSearchResult:
        """Assert that a pattern does NOT appear in logs."""
        return assert_not_log(
            self.base_dir,
            pattern,
            within=within,
            since=since,
            until=until,
            nodes=nodes,
        )

    def assert_log_order(
        self,
        patterns: list[str],
        *,
        within: Range | None = None,
        since: Marker | None = None,
        until: Marker | None = None,
        nodes: list[int] | None = None,
    ) -> list[LogSearchResult]:
        """Assert that patterns appear in order in logs."""
        return assert_log_order(
            self.base_dir,
            patterns,
            within=within,
            since=since,
            until=until,
            nodes=nodes,
        )


# ---------------------------------------------------------------------------
# Script loading and runner
# ---------------------------------------------------------------------------


def _load_scenario_module(script_path: Path) -> Any:
    """Load a scenario script module, adding its directory to sys.path."""
    spec = importlib.util.spec_from_file_location("scenario_script", script_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load script: {script_path}")

    module = importlib.util.module_from_spec(spec)
    script_dir = str(script_path.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec.loader.exec_module(module)
    return module


def load_scenario_script(script_path: Path) -> Any:
    """Load a scenario script and return the ``scenario`` coroutine function.

    The script must define ``async def scenario(ctx, log, **kwargs):``.

    Raises:
        ValueError: If script is missing the required function.
    """
    module = _load_scenario_module(script_path)

    if not hasattr(module, "scenario"):
        raise ValueError(f"Script must define 'async def scenario(ctx)': {script_path}")

    fn = module.scenario
    if not callable(fn):
        raise ValueError(f"'scenario' must be a callable: {script_path}")

    return fn


def load_scenario_matrix(script_path: Path) -> list[dict[str, Any]] | None:
    """Extract and validate a ``matrix`` list from a scenario script.

    Each matrix entry must be a dict with a unique ``label`` key.
    Returns None if the script has no ``matrix`` attribute.

    Raises:
        ValueError: If matrix is malformed or has duplicate/missing labels.
    """
    module = _load_scenario_module(script_path)
    matrix = getattr(module, "matrix", None)
    if matrix is None:
        return None

    if not isinstance(matrix, list) or not matrix:
        raise ValueError(f"'matrix' must be a non-empty list: {script_path}")

    labels: set[str] = set()
    for i, entry in enumerate(matrix):
        if not isinstance(entry, dict):
            raise ValueError(f"matrix[{i}] must be a dict: {script_path}")
        label = entry.get("label")
        if not label or not isinstance(label, str):
            raise ValueError(
                f"matrix[{i}] missing required 'label' string: {script_path}"
            )
        if not re.fullmatch(r"[a-zA-Z0-9_]+", label):
            raise ValueError(
                f"matrix[{i}] label '{label}' must be alphanumeric/underscore only: "
                f"{script_path}"
            )
        if label in labels:
            raise ValueError(f"Duplicate matrix label '{label}': {script_path}")
        labels.add(label)

    return matrix


async def run_scenario_with_monitor(
    script_path: Path,
    network: TestNetwork,
    tracked_features: list[str] | None = None,
    params: dict[str, Any] | None = None,
) -> bool:
    """Run a scenario script with the network monitor in background.

    Args:
        script_path: Path to the scenario script.
        network: The TestNetwork instance.
        tracked_features: Optional list of feature names to track.
        params: Optional keyword arguments passed to the scenario function.

    Returns:
        True if scenario passed, False if it failed.
    """
    from xahaud_scripts.testnet.monitor import NetworkMonitor

    scenario_fn = load_scenario_script(script_path)
    ctx = ScenarioContext(network)

    stop_event = asyncio.Event()
    monitor = NetworkMonitor(
        rpc_client=network.rpc_client,
        network_config=network.config,
        tracked_features=tracked_features,
    )

    async def run_monitor() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await monitor.monitor(stop_event=stop_event)

    monitor_task = asyncio.create_task(run_monitor())

    try:
        label = script_path.name
        if params:
            label += f" [{', '.join(f'{k}={v}' for k, v in params.items())}]"
        logger.info(f"Running scenario: {label}")
        if params:
            await scenario_fn(ctx, ctx.log, **params)
        else:
            await scenario_fn(ctx, ctx.log)
        logger.info("Scenario completed successfully")
        return True
    except Exception:
        logger.exception("Scenario failed")
        return False
    finally:
        stop_event.set()
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
