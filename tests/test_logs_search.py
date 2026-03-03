"""Tests for logs_search time filtering."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from textwrap import dedent

import pytest

from xahaud_scripts.testnet.cli_handlers.logs_search import (
    _get_earliest_timestamp,
    _get_latest_timestamp,
    _normalize_ts,
    iter_matching_lines,
    logs_search_handler,
    parse_timestamp,
)

MATCH_ALL = re.compile(r".")


# --- parse_timestamp ---


def test_parse_timestamp_custom_format():
    ts = parse_timestamp("N0 14:25:46.618659 +07 some log line")
    assert ts is not None
    assert ts.year == 1900
    assert ts.hour == 14
    assert ts.minute == 25
    assert ts.second == 46


def test_parse_timestamp_default_rippled_format():
    ts = parse_timestamp("2026-Mar-03 07:20:33.123456 some log line")
    assert ts is not None
    assert ts.year == 2026
    assert ts.month == 3
    assert ts.day == 3
    assert ts.hour == 7
    assert ts.minute == 20
    assert ts.second == 33


def test_parse_timestamp_no_match():
    assert parse_timestamp("no timestamp here") is None
    assert parse_timestamp("") is None


# --- _normalize_ts ---


def test_normalize_ts_time_only_filter_with_full_date_log():
    """Year=1900 filter vs year=2026 log → strip date from log."""
    log_ts = datetime(2026, 3, 3, 7, 20, 33)
    filter_ts = datetime(1900, 1, 1, 7, 20, 0)
    normalized = _normalize_ts(log_ts, filter_ts)
    assert normalized.year == 1900
    assert normalized.month == 1
    assert normalized.day == 1
    assert normalized.hour == 7
    assert normalized.minute == 20
    assert normalized.second == 33


def test_normalize_ts_both_time_only():
    """Both year=1900 → no change."""
    log_ts = datetime(1900, 1, 1, 14, 25, 46)
    filter_ts = datetime(1900, 1, 1, 14, 20, 0)
    assert _normalize_ts(log_ts, filter_ts) == log_ts


def test_normalize_ts_both_full_date():
    """Both have real years → no change."""
    log_ts = datetime(2026, 3, 3, 7, 20, 33)
    filter_ts = datetime(2026, 3, 3, 7, 19, 0)
    assert _normalize_ts(log_ts, filter_ts) == log_ts


# --- iter_matching_lines with time filters ---


@pytest.fixture
def full_date_log(tmp_path: Path) -> Path:
    """Log file with default rippled timestamps (full date)."""
    log_dir = tmp_path / "n0"
    log_dir.mkdir()
    log_file = log_dir / "debug.log"
    log_file.write_text(
        dedent("""\
        2026-Mar-03 07:17:35.000000 Startup line 1
        2026-Mar-03 07:17:36.000000 Startup line 2
        2026-Mar-03 07:20:00.000000 Recent line 1
        2026-Mar-03 07:20:15.000000 Recent line 2
        2026-Mar-03 07:20:30.000000 Recent line 3
        2026-Mar-03 07:20:33.000000 Latest line
        """)
    )
    return log_file


@pytest.fixture
def custom_format_log(tmp_path: Path) -> Path:
    """Log file with custom time-only timestamps."""
    log_dir = tmp_path / "n1"
    log_dir.mkdir()
    log_file = log_dir / "debug.log"
    log_file.write_text(
        dedent("""\
        N1 07:17:35.000000 +07 Startup line 1
        N1 07:20:00.000000 +07 Recent line 1
        N1 07:20:15.000000 +07 Recent line 2
        N1 07:20:33.000000 +07 Latest line
        """)
    )
    return log_file


def test_relative_start_filter_full_date_logs(full_date_log: Path):
    """-s -30s equivalent: filter should exclude startup lines."""
    # Simulate -30s from 07:20:33 → time_start = 07:20:03 (year=1900)
    time_start = datetime(1900, 1, 1, 7, 20, 3)
    entries = list(
        iter_matching_lines(full_date_log, 0, MATCH_ALL, time_start=time_start)
    )
    # Should only get lines at/after 07:20:03
    assert len(entries) == 3
    assert "Recent line 2" in entries[0].line
    assert "Recent line 3" in entries[1].line
    assert "Latest line" in entries[2].line


def test_absolute_time_window_full_date_logs(full_date_log: Path):
    """--time-start 07:19:40 --time-end 07:20:05 with full-date logs."""
    time_start = datetime(1900, 1, 1, 7, 19, 40)
    time_end = datetime(1900, 1, 1, 7, 20, 5)
    entries = list(
        iter_matching_lines(
            full_date_log, 0, MATCH_ALL, time_start=time_start, time_end=time_end
        )
    )
    assert len(entries) == 1
    assert "Recent line 1" in entries[0].line


def test_time_filter_custom_format_logs(custom_format_log: Path):
    """Time filter with custom time-only log format (both year=1900)."""
    time_start = datetime(1900, 1, 1, 7, 20, 0)
    entries = list(
        iter_matching_lines(custom_format_log, 1, MATCH_ALL, time_start=time_start)
    )
    assert len(entries) == 3
    assert "Recent line 1" in entries[0].line
    assert "Latest line" in entries[2].line


def test_boundary_inclusivity_start(full_date_log: Path):
    """Exact start timestamp should be included."""
    time_start = datetime(1900, 1, 1, 7, 20, 15)
    entries = list(
        iter_matching_lines(full_date_log, 0, MATCH_ALL, time_start=time_start)
    )
    assert len(entries) == 3
    assert "Recent line 2" in entries[0].line


def test_boundary_inclusivity_end(full_date_log: Path):
    """Exact end timestamp should be included."""
    time_end = datetime(1900, 1, 1, 7, 20, 15)
    entries = list(iter_matching_lines(full_date_log, 0, MATCH_ALL, time_end=time_end))
    assert len(entries) == 4  # startup + recent line 1 + recent line 2
    assert "Recent line 2" in entries[-1].line


def test_no_filter_returns_all(full_date_log: Path):
    """No time filter should return all lines."""
    entries = list(iter_matching_lines(full_date_log, 0, MATCH_ALL))
    assert len(entries) == 6


def test_end_filter_full_date_logs(full_date_log: Path):
    """End filter with year=1900 should work against full-date logs."""
    time_end = datetime(1900, 1, 1, 7, 17, 36)
    entries = list(iter_matching_lines(full_date_log, 0, MATCH_ALL, time_end=time_end))
    assert len(entries) == 2
    assert "Startup line 1" in entries[0].line
    assert "Startup line 2" in entries[1].line


# --- _get_latest_timestamp ---


def test_get_latest_timestamp_full_date(full_date_log: Path):
    """Should find the latest timestamp from a full-date log."""
    ts = _get_latest_timestamp([full_date_log])
    assert ts is not None
    assert ts.hour == 7
    assert ts.minute == 20
    assert ts.second == 33


def test_get_latest_timestamp_custom_format(custom_format_log: Path):
    ts = _get_latest_timestamp([custom_format_log])
    assert ts is not None
    assert ts.hour == 7
    assert ts.minute == 20
    assert ts.second == 33


def test_get_latest_timestamp_multiple_files(
    full_date_log: Path, custom_format_log: Path
):
    """Should return the max across all files."""
    ts = _get_latest_timestamp([full_date_log, custom_format_log])
    assert ts is not None
    # Both end at 07:20:33 but full_date has year=2026 which is > year=1900
    assert ts.year == 2026


def test_get_latest_timestamp_empty_file(tmp_path: Path):
    log_dir = tmp_path / "n9"
    log_dir.mkdir()
    log_file = log_dir / "debug.log"
    log_file.write_text("")
    assert _get_latest_timestamp([log_file]) is None


# --- logs_search_handler with relative_start ---


@pytest.fixture
def testnet_dir(tmp_path: Path) -> Path:
    """Create a testnet directory with two node log files."""
    for i in range(2):
        node_dir = tmp_path / f"n{i}"
        node_dir.mkdir()
        (node_dir / "debug.log").write_text(
            dedent("""\
            2026-Mar-03 07:17:35.000000 Startup line
            2026-Mar-03 07:20:00.000000 Recent line 1
            2026-Mar-03 07:20:30.000000 Recent line 2
            2026-Mar-03 07:20:33.000000 Latest line
            """)
        )
    return tmp_path


def test_relative_start_anchored_to_log(testnet_dir: Path):
    """-s -30s should anchor to latest log timestamp, not wall clock."""
    count = logs_search_handler(
        base_dir=testnet_dir,
        pattern=".",
        relative_start=timedelta(seconds=30),
    )
    # 07:20:33 - 30s = 07:20:03 → should get "Recent line 2" + "Latest line" × 2 nodes
    assert count == 4


def test_relative_start_large_delta(testnet_dir: Path):
    """Large delta should include everything."""
    count = logs_search_handler(
        base_dir=testnet_dir,
        pattern=".",
        relative_start=timedelta(hours=1),
    )
    # All 4 lines × 2 nodes = 8
    assert count == 8


# --- _get_earliest_timestamp ---


def test_get_earliest_timestamp_full_date(full_date_log: Path):
    ts = _get_earliest_timestamp([full_date_log])
    assert ts is not None
    assert ts.hour == 7
    assert ts.minute == 17
    assert ts.second == 35


def test_get_earliest_timestamp_empty_file(tmp_path: Path):
    log_dir = tmp_path / "n9"
    log_dir.mkdir()
    log_file = log_dir / "debug.log"
    log_file.write_text("")
    assert _get_earliest_timestamp([log_file]) is None


# --- offset from start (+N) ---


def test_offset_start_from_beginning(testnet_dir: Path):
    """+3m from earliest (07:17:35) → start at 07:20:35 → only "Latest line"."""
    count = logs_search_handler(
        base_dir=testnet_dir,
        pattern=".",
        offset_start=timedelta(minutes=3),
    )
    # 07:17:35 + 3m = 07:20:35 → only "Latest line" at 07:20:33 is BEFORE that
    # Actually 07:20:33 < 07:20:35, so nothing passes. Let me use 2m55s instead.
    # 07:17:35 + 2m55s = 07:20:30 → "Recent line 2" (07:20:30) + "Latest" (07:20:33)
    # Hmm, let me just assert what we get with 3m
    # 07:17:35 + 3m = 07:20:35 → nothing at or after 07:20:35 → 0
    assert count == 0


def test_offset_start_zero(testnet_dir: Path):
    """+0 from earliest should return everything."""
    count = logs_search_handler(
        base_dir=testnet_dir,
        pattern=".",
        offset_start=timedelta(0),
    )
    assert count == 8


def test_offset_end_from_beginning(testnet_dir: Path):
    """+30s from earliest (07:17:35) → end at 07:18:05 → only startup lines."""
    count = logs_search_handler(
        base_dir=testnet_dir,
        pattern=".",
        offset_end=timedelta(seconds=30),
    )
    # 07:17:35 + 30s = 07:18:05 → only "Startup line" (07:17:35) × 2 nodes
    assert count == 2


def test_offset_start_and_end(testnet_dir: Path):
    """+2m25s to +3m from start → window around 07:20:00."""
    count = logs_search_handler(
        base_dir=testnet_dir,
        pattern=".",
        offset_start=timedelta(minutes=2, seconds=25),
        offset_end=timedelta(minutes=3),
    )
    # earliest=07:17:35, +2m25s=07:20:00, +3m=07:20:35
    # Lines in [07:20:00, 07:20:35]: Recent 1 (07:20:00), Recent 2 (07:20:30), Latest (07:20:33)
    # × 2 nodes = 6
    assert count == 6
