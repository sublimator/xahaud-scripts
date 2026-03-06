"""Scenario testing primitives for x-testnet.

Provides timing anchors (Marker, Range, Operation) and log assertions
that work against live networks or archived snapshots. Reuses the
logs_search backend for all log operations.

Example usage in a scenario script:

    from xahaud_scripts.testnet.scenario import ScenarioContext

    async def scenario(ctx: ScenarioContext):
        await ctx.wait_for_ledger(10)
        pre = ctx.mark("pre-kill")
        ctx.stop_nodes([1, 2])
        await ctx.sleep(5)
        post = ctx.mark("post-kill")

        window = pre.until(post)
        ctx.assert_log("LedgerConsensus", within=window, nodes=[0, 3, 4])
        ctx.assert_not_log("error", within=window)
"""

from __future__ import annotations

import asyncio
import re
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xahaud_scripts.testnet.protocols import RPCClient

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
        f"Ledger {target} not reached within {timeout}s on node {node_id}"
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
        rpc, target,
        node_id=node_id,
        timeout=timeout,
        poll_interval=poll_interval,
        name=name or f"ledgers-+{count}",
    )


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

    for entry in merge_log_streams(log_files, regex, time_start=time_start, time_end=time_end):
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

    def __init__(self, message: str, result: LogSearchResult) -> None:
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
        base_dir, pattern, within=within, since=since, until=until, nodes=nodes,
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
        base_dir, pattern, within=within, since=since, until=until, nodes=nodes,
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
            base_dir, pattern, within=within, since=since, until=until, nodes=nodes,
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
