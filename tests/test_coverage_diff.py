"""Tests for diff coverage analysis (utils/coverage_diff.py)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from xahaud_scripts.utils.coverage_diff import (
    DiffCoverageResult,
    DiffCoverageSummary,
    _group_lines_with_context,
    _parse_gcovr_line_coverage,
    compute_diff_coverage,
    parse_diff_hunks,
    parse_gcovr_exclusions,
)

# --- parse_gcovr_exclusions ---


def test_parse_gcovr_exclusions_missing_file(tmp_path: Path):
    assert parse_gcovr_exclusions(tmp_path / "nope.cpp") == set()


def test_parse_gcovr_exclusions_single_line(tmp_path: Path):
    src = tmp_path / "foo.cpp"
    src.write_text("ok\nbad();  // GCOVR_EXCL_LINE\nok\n")
    assert parse_gcovr_exclusions(src) == {2}


def test_parse_gcovr_exclusions_block(tmp_path: Path):
    src = tmp_path / "foo.cpp"
    src.write_text(
        "line1\n// GCOVR_EXCL_START\nline3\nline4\n// GCOVR_EXCL_STOP\nline6\n"
    )
    # START, STOP and everything between are excluded; line6 is not.
    assert parse_gcovr_exclusions(src) == {2, 3, 4, 5}


def test_parse_gcovr_exclusions_unterminated_block(tmp_path: Path):
    src = tmp_path / "foo.cpp"
    src.write_text("line1\n// GCOVR_EXCL_START\nline3\nline4\n")
    # No STOP — block runs to end of file.
    assert parse_gcovr_exclusions(src) == {2, 3, 4}


# --- DiffCoverageResult properties ---


def test_result_coverage_pct_empty_is_full():
    assert DiffCoverageResult(filepath="f.cpp").coverage_pct == 100.0


def test_result_coverage_pct_partial():
    r = DiffCoverageResult(
        filepath="f.cpp",
        changed_lines={1, 2, 3, 4},
        covered_lines={1, 2, 3},
    )
    assert r.total_changed == 4
    assert r.total_covered == 3
    assert r.coverage_pct == 75.0


def test_result_branch_pct_none_without_data():
    assert DiffCoverageResult(filepath="f.cpp").branch_pct is None


def test_result_branch_pct_with_data():
    r = DiffCoverageResult(filepath="f.cpp", branches_taken=3, branches_total=4)
    assert r.branch_pct == 75.0


# --- DiffCoverageSummary properties ---


def test_summary_aggregates_across_files():
    a = DiffCoverageResult(
        filepath="a.cpp",
        changed_lines={1, 2},
        covered_lines={1, 2},
    )
    b = DiffCoverageResult(
        filepath="b.cpp",
        changed_lines={3, 4, 5},
        covered_lines={3},
        uncovered_lines={4, 5},
    )
    summary = DiffCoverageSummary(file_results=[a, b])
    assert summary.total_changed == 5
    assert summary.total_covered == 3
    assert summary.patch_coverage_pct == 60.0
    assert summary.files_with_uncovered == [b]


def test_summary_patch_coverage_empty_is_full():
    assert DiffCoverageSummary(file_results=[]).patch_coverage_pct == 100.0
    assert DiffCoverageSummary(file_results=[]).branch_coverage_pct is None


def test_summary_branch_aggregation():
    a = DiffCoverageResult(filepath="a.cpp", branches_taken=1, branches_total=2)
    b = DiffCoverageResult(filepath="b.cpp", branches_taken=2, branches_total=2)
    summary = DiffCoverageSummary(file_results=[a, b])
    assert summary.total_branches_taken == 3
    assert summary.total_branches == 4
    assert summary.branch_coverage_pct == 75.0


# --- _group_lines_with_context ---


def test_group_lines_empty():
    assert _group_lines_with_context([], 3, 100) == []


def test_group_lines_single_with_context():
    assert _group_lines_with_context([10], 3, 100) == [(7, 13)]


def test_group_lines_clamps_to_bounds():
    assert _group_lines_with_context([1], 3, 100) == [(1, 4)]
    assert _group_lines_with_context([99], 3, 100) == [(96, 100)]


def test_group_lines_merges_overlapping_regions():
    # Regions (8,12) and (12,16) touch -> merged.
    assert _group_lines_with_context([10, 14], 2, 100) == [(8, 16)]


def test_group_lines_keeps_distant_regions_separate():
    assert _group_lines_with_context([10, 30], 2, 100) == [(8, 12), (28, 32)]


# --- compute_diff_coverage ---


def _abs(repo: Path, rel: str) -> str:
    return os.path.join(str(repo), rel)


def test_compute_diff_coverage_covered_and_uncovered(tmp_path: Path):
    src = tmp_path / "src" / "foo.cpp"
    src.parent.mkdir(parents=True)
    src.write_text("a\nb\nc\nd\n")

    diff_hunks = {"src/foo.cpp": [(1, 4)]}
    # Line 4 absent from coverage -> non-executable, not counted.
    line_coverage = {_abs(tmp_path, "src/foo.cpp"): {1: 5, 2: 0, 3: 0}}

    summary = compute_diff_coverage(diff_hunks, line_coverage, str(tmp_path))
    assert len(summary.file_results) == 1
    r = summary.file_results[0]
    assert r.covered_lines == {1}
    assert r.uncovered_lines == {2, 3}
    assert r.changed_lines == {1, 2, 3}
    assert r.no_coverage_data is False


def test_compute_diff_coverage_no_coverage_data(tmp_path: Path):
    diff_hunks = {"src/foo.cpp": [(1, 2)]}
    summary = compute_diff_coverage(diff_hunks, {}, str(tmp_path))
    r = summary.file_results[0]
    assert r.no_coverage_data is True
    assert r.uncovered_lines == {1, 2}
    assert r.changed_lines == {1, 2}


def test_compute_diff_coverage_skips_non_source_and_tests(tmp_path: Path):
    diff_hunks = {
        "README.md": [(1, 2)],
        "src/test/Foo_test.cpp": [(1, 2)],
        "external/lib/x.cpp": [(1, 2)],
    }
    summary = compute_diff_coverage(diff_hunks, {}, str(tmp_path))
    assert summary.file_results == []


def test_compute_diff_coverage_honours_gcovr_exclusions(tmp_path: Path):
    src = tmp_path / "src" / "foo.cpp"
    src.parent.mkdir(parents=True)
    src.write_text("excluded();  // GCOVR_EXCL_LINE\nb\nc\n")

    diff_hunks = {"src/foo.cpp": [(1, 3)]}
    line_coverage = {_abs(tmp_path, "src/foo.cpp"): {1: 0, 2: 0, 3: 5}}

    summary = compute_diff_coverage(diff_hunks, line_coverage, str(tmp_path))
    r = summary.file_results[0]
    # Line 1 dropped via GCOVR_EXCL_LINE despite being uncovered.
    assert r.uncovered_lines == {2}
    assert r.covered_lines == {3}
    assert r.changed_lines == {2, 3}


def test_compute_diff_coverage_branch_data(tmp_path: Path):
    src = tmp_path / "src" / "foo.cpp"
    src.parent.mkdir(parents=True)
    src.write_text("a\nb\n")

    diff_hunks = {"src/foo.cpp": [(1, 2)]}
    abs_path = _abs(tmp_path, "src/foo.cpp")
    line_coverage = {abs_path: {1: 5, 2: 3}}
    # Line 1: true side fired, false side never -> partial branch.
    branch_coverage = {abs_path: {1: [(10, 5, 0)]}}

    summary = compute_diff_coverage(
        diff_hunks, line_coverage, str(tmp_path), branch_coverage
    )
    r = summary.file_results[0]
    assert r.branches_taken == 1
    assert r.branches_total == 2
    assert r.partial_branch_lines == {1}
    assert r.branches_per_line == {1: [(10, 5, 0)]}
    assert r.branch_pct == 50.0


def test_compute_diff_coverage_sorts_files(tmp_path: Path):
    diff_hunks = {"src/zeta.cpp": [(1, 1)], "src/alpha.cpp": [(1, 1)]}
    summary = compute_diff_coverage(diff_hunks, {}, str(tmp_path))
    assert [r.filepath for r in summary.file_results] == [
        "src/alpha.cpp",
        "src/zeta.cpp",
    ]


# --- _parse_gcovr_line_coverage ---


def test_parse_gcovr_line_coverage(tmp_path: Path):
    report = tmp_path / "cov.json"
    report.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "file": "src/foo.cpp",
                        "lines": [
                            {"line_number": 1, "count": 5},
                            {"line_number": 2, "count": 0},
                        ],
                    }
                ]
            }
        )
    )
    assert _parse_gcovr_line_coverage(report) == {"src/foo.cpp": {1: 5, 2: 0}}


def test_parse_gcovr_line_coverage_empty_report(tmp_path: Path):
    report = tmp_path / "cov.json"
    report.write_text(json.dumps({}))
    assert _parse_gcovr_line_coverage(report) == {}


# --- parse_diff_hunks (real git repo) ---


@pytest.fixture
def git_repo(tmp_path: Path):
    """A throwaway git repo with a 'base' branch at the first commit."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    src = tmp_path / "src" / "foo.cpp"
    src.parent.mkdir(parents=True)
    src.write_text("a\nb\nc\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("branch", "base")
    return tmp_path, git, src


def test_parse_diff_hunks_detects_changes(git_repo):
    repo, git, src = git_repo
    # Modify line 2, append line 4.
    src.write_text("a\nB2\nc\nd\n")
    git("add", "-A")
    git("commit", "-qm", "change")

    hunks = parse_diff_hunks("base", str(repo))
    assert "src/foo.cpp" in hunks

    changed: set[int] = set()
    for start, end in hunks["src/foo.cpp"]:
        changed.update(range(start, end + 1))
    assert changed == {2, 4}


def test_parse_diff_hunks_ignores_pure_deletions(git_repo):
    repo, git, src = git_repo
    # Only delete a line — git diff records a zero-count addition hunk.
    src.write_text("a\nc\n")
    git("add", "-A")
    git("commit", "-qm", "delete")

    hunks = parse_diff_hunks("base", str(repo))
    assert "src/foo.cpp" not in hunks


def test_parse_diff_hunks_returns_empty_on_git_failure(git_repo):
    repo, _git, _src = git_repo
    assert parse_diff_hunks("no-such-ref", str(repo)) == {}
