"""Logs search handler - merges logs from multiple nodes by timestamp.

Uses a heap-based streaming merge to avoid loading all logs into memory.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import click

# Timestamp patterns for different log formats
# Format 1: N0 14:25:46.618659 +07 ... (custom LOG_DATE_FORMAT)
# Format 2: 2024-Jan-15 10:30:45.123456 ... (default rippled format)
TS_PATTERNS = [
    # Custom format: N0 14:25:46.618659 +07
    re.compile(r"^N\d+\s+(\d{2}:\d{2}:\d{2}\.\d+)"),
    # Default rippled format: 2024-Jan-15 10:30:45.123456
    re.compile(r"^(\d{4}-\w{3}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)"),
]

TS_FORMATS = [
    "%H:%M:%S.%f",  # Time only (custom format)
    "%Y-%b-%d %H:%M:%S.%f",  # Full date (default format)
]


@dataclass(order=True)
class LogEntry:
    """A single log entry with timestamp for heap ordering."""

    timestamp: datetime
    node_id: int = field(compare=False)
    line: str = field(compare=False)


def parse_timestamp(line: str) -> datetime | None:
    """Parse timestamp from log line, trying multiple formats."""
    for pattern, fmt in zip(TS_PATTERNS, TS_FORMATS, strict=True):
        match = pattern.match(line)
        if match:
            try:
                return datetime.strptime(match.group(1), fmt)
            except ValueError:
                continue
    return None


def _normalize_ts(ts: datetime, reference: datetime) -> datetime:
    """Normalize ts to be comparable with reference.

    If reference is time-only (year=1900), strip date from ts too.
    This handles the mismatch between full-date log timestamps (year=2026)
    and time-only filter values (year=1900).
    """
    if reference.year == 1900 and ts.year != 1900:
        return ts.replace(year=1900, month=1, day=1)
    return ts


def iter_matching_lines(
    log_file: Path,
    node_id: int,
    pattern: re.Pattern[str],
    tail: int | None = None,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    exclude: re.Pattern[str] | None = None,
) -> Iterator[LogEntry]:
    """Iterate over matching lines from a log file.

    Args:
        log_file: Path to the log file
        node_id: Node ID for prefixing
        pattern: Compiled regex pattern to match
        tail: If set, only read last N lines
        time_start: Only include entries at or after this time
        time_end: Only include entries at or before this time
        exclude: If set, skip lines that match this pattern

    Yields:
        LogEntry objects for matching lines
    """
    try:
        with open(log_file) as f:
            # For tail, we need to read all lines first
            lines = f.readlines()[-tail:] if tail else f.readlines()

            for line in lines:
                if pattern.search(line):
                    if exclude and exclude.search(line):
                        continue
                    ts = parse_timestamp(line)
                    # Skip continuation lines (no timestamp = part of multi-line entry)
                    if ts is None:
                        continue
                    # Filter by time range (normalize to handle year mismatch)
                    if time_start and _normalize_ts(ts, time_start) < time_start:
                        continue
                    if time_end and _normalize_ts(ts, time_end) > time_end:
                        continue
                    yield LogEntry(
                        timestamp=ts,
                        node_id=node_id,
                        line=line.rstrip(),
                    )
    except OSError as e:
        click.echo(f"Warning: Could not read {log_file}: {e}", err=True)


def merge_log_streams(
    log_files: list[Path],
    pattern: re.Pattern[str],
    tail: int | None = None,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    exclude: re.Pattern[str] | None = None,
) -> Iterator[LogEntry]:
    """Merge multiple log files by timestamp using a heap.

    This is memory-efficient for streaming large log files since
    we only keep one line per file in memory at a time (plus the heap).

    Args:
        log_files: List of log file paths
        pattern: Compiled regex pattern to match
        tail: If set, only read last N lines from each file
        time_start: Only include entries at or after this time
        time_end: Only include entries at or before this time
        exclude: If set, skip lines that match this pattern

    Yields:
        LogEntry objects in timestamp order
    """
    # Create iterators for each log file
    iterators: list[tuple[int, Iterator[LogEntry]]] = []
    for log_file in log_files:
        node_id = int(log_file.parent.name[1:])  # n0 -> 0, n1 -> 1, etc.
        it = iter_matching_lines(
            log_file, node_id, pattern, tail, time_start, time_end, exclude
        )
        iterators.append((node_id, it))

    # Initialize heap with first entry from each iterator
    heap: list[tuple[LogEntry, int, Iterator[LogEntry]]] = []

    for idx, (_node_id, it) in enumerate(iterators):
        try:
            entry = next(it)
            heapq.heappush(heap, (entry, idx, it))
        except StopIteration:
            pass  # Empty or no matches

    # Merge by repeatedly popping smallest and pushing next from same iterator
    while heap:
        entry, idx, it = heapq.heappop(heap)
        yield entry

        try:
            next_entry = next(it)
            heapq.heappush(heap, (next_entry, idx, it))
        except StopIteration:
            pass  # This iterator is exhausted


def parse_node_spec(spec: str) -> set[int]:
    """Parse a node specification like '0-2,5,7-9' into a set of node IDs.

    Examples:
        '0-2' -> {0, 1, 2}
        '1,3,5' -> {1, 3, 5}
        '0-2,5,7-9' -> {0, 1, 2, 5, 7, 8, 9}
    """
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            result.update(range(int(start), int(end) + 1))
        else:
            result.add(int(part))
    return result


def _get_latest_timestamp(
    log_files: list[Path], scan_lines: int = 50
) -> datetime | None:
    """Get the latest timestamp across all log files by scanning their tails.

    Args:
        log_files: Log files to scan.
        scan_lines: Number of lines from the end to scan per file.

    Returns:
        The latest timestamp found, or None.
    """
    latest: datetime | None = None
    for log_file in log_files:
        try:
            with open(log_file) as f:
                lines = f.readlines()[-scan_lines:]
            for line in reversed(lines):
                ts = parse_timestamp(line)
                if ts is not None:
                    if latest is None or ts > latest:
                        latest = ts
                    break  # First (latest) timestamp from this file is enough
        except OSError:
            continue
    return latest


def _get_earliest_timestamp(
    log_files: list[Path], scan_lines: int = 50
) -> datetime | None:
    """Get the earliest timestamp across all log files by scanning their heads.

    Args:
        log_files: Log files to scan.
        scan_lines: Number of lines from the start to scan per file.

    Returns:
        The earliest timestamp found, or None.
    """
    earliest: datetime | None = None
    for log_file in log_files:
        try:
            with open(log_file) as f:
                for _, line in zip(range(scan_lines), f, strict=False):
                    ts = parse_timestamp(line)
                    if ts is not None:
                        if earliest is None or ts < earliest:
                            earliest = ts
                        break  # First timestamp from this file is enough
        except OSError:
            continue
    return earliest


def logs_search_handler(
    base_dir: Path,
    pattern: str,
    tail: int | None = None,
    no_sort: bool = False,
    limit: int | None = None,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    relative_start: timedelta | None = None,
    offset_start: timedelta | None = None,
    offset_end: timedelta | None = None,
    nodes: str | None = None,
    exclude_patterns: list[str] | None = None,
) -> int:
    """Search all node logs for a regex pattern and merge by timestamp.

    Args:
        base_dir: Base directory containing n0/, n1/, etc.
        pattern: Regex pattern to search for
        tail: Only search last N lines of each file
        no_sort: Don't sort by timestamp (faster, less memory)
        limit: Maximum number of results to display
        time_start: Only include entries at or after this time
        time_end: Only include entries at or before this time
        relative_start: If set, compute time_start as (latest - delta).
        offset_start: If set, compute time_start as (earliest + delta).
        offset_end: If set, compute time_end as (earliest + delta).
        nodes: Node spec like '0-2,5' to filter which nodes to search
        exclude_patterns: Regex patterns to exclude from results

    Returns:
        Number of matching lines found
    """
    # Compile the pattern
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise click.BadParameter(f"Invalid regex: {e}") from e

    # Compile exclude pattern (join multiple with |)
    exclude: re.Pattern[str] | None = None
    if exclude_patterns:
        try:
            exclude = re.compile("|".join(exclude_patterns))
        except re.error as e:
            raise click.BadParameter(f"Invalid exclude regex: {e}") from e

    # Check base_dir exists
    if not base_dir.exists():
        raise click.ClickException(f"Base directory does not exist: {base_dir}")

    # Find all node directories
    node_dirs = sorted(base_dir.glob("n[0-9]*"))
    if nodes is not None:
        # Filter to specified nodes
        try:
            node_ids = parse_node_spec(nodes)
        except ValueError as e:
            raise click.BadParameter(f"Invalid node spec '{nodes}': {e}") from e
        node_dirs = [d for d in node_dirs if int(d.name[1:]) in node_ids]
    if not node_dirs:
        raise click.ClickException(
            f"No node directories (n0, n1, ...) found in {base_dir}"
        )

    # Find all debug.log files and report what we're looking for
    log_files: list[Path] = []
    missing_logs: list[Path] = []

    for node_dir in node_dirs:
        log_file = node_dir / "debug.log"
        if log_file.exists():
            log_files.append(log_file)
        else:
            missing_logs.append(log_file)

    if not log_files:
        click.echo(f"Looking in: {base_dir}", err=True)
        click.echo(f"Found {len(node_dirs)} node directories:", err=True)
        for node_dir in node_dirs:
            click.echo(f"  {node_dir.name}/ -> debug.log NOT FOUND", err=True)
        raise click.ClickException(
            "No debug.log files found. Are the nodes running with logging enabled?"
        )

    # Show what we found
    click.echo(f"Searching {len(log_files)} log files in {base_dir}:", err=True)
    for log_file in log_files:
        size_kb = log_file.stat().st_size / 1024
        click.echo(f"  {log_file.parent.name}/debug.log ({size_kb:.1f} KB)", err=True)
    if missing_logs:
        for missing in missing_logs:
            click.echo(f"  {missing.parent.name}/debug.log (not found)", err=True)

    # Resolve relative/offset times from actual log timestamps (no clock dependency)
    if relative_start is not None:
        latest = _get_latest_timestamp(log_files)
        if latest is not None:
            time_start = latest - relative_start
            click.echo(
                f"  Latest log: {latest}, start: {time_start} (-{relative_start})",
                err=True,
            )
        else:
            click.echo("  Warning: could not find any timestamps in logs", err=True)

    if offset_start is not None or offset_end is not None:
        earliest = _get_earliest_timestamp(log_files)
        if earliest is not None:
            if offset_start is not None:
                time_start = earliest + offset_start
            if offset_end is not None:
                time_end = earliest + offset_end
            click.echo(
                f"  Earliest log: {earliest}, start: {time_start}, end: {time_end}",
                err=True,
            )
        else:
            click.echo("  Warning: could not find any timestamps in logs", err=True)

    count = 0

    if no_sort:
        # Fast path: just grep each file, no sorting
        for log_file in log_files:
            node_id = int(log_file.parent.name[1:])
            for entry in iter_matching_lines(
                log_file, node_id, regex, tail, time_start, time_end, exclude
            ):
                click.echo(entry.line)
                count += 1
                if limit and count >= limit:
                    break
            if limit and count >= limit:
                break
    else:
        # Use heap-based merge for sorted output
        for entry in merge_log_streams(
            log_files, regex, tail, time_start, time_end, exclude
        ):
            click.echo(entry.line)
            count += 1
            if limit and count >= limit:
                break

    click.echo(f"\n{count} matching lines from {len(log_files)} log files")
    return count
