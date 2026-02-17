"""Diff coverage analysis and visualization.

Cross-references git diff hunks with LLVM coverage data to show
which changed lines are not covered by tests. Uses Rich for
syntax-highlighted output of uncovered code regions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from xahaud_scripts.utils.coverage import (
    check_coverage_build,
    find_and_merge_profdata,
    find_binary,
    get_llvm_tools,
)
from xahaud_scripts.utils.logging import make_logger
from xahaud_scripts.utils.paths import get_xahaud_root

logger = make_logger(__name__)
console = Console()

# Source file extensions we care about for coverage
SOURCE_EXTENSIONS = (".cpp", ".h", ".c", ".hpp", ".ipp")

# Directories to skip (test files, external deps)
SKIP_PREFIXES = ("src/test/", "src/tests/", "external/")


@dataclass
class DiffCoverageResult:
    """Coverage results for a single file's changed lines."""

    filepath: str
    changed_lines: set[int] = field(default_factory=set)
    covered_lines: set[int] = field(default_factory=set)
    uncovered_lines: set[int] = field(default_factory=set)
    no_coverage_data: bool = False

    @property
    def total_changed(self) -> int:
        return len(self.changed_lines)

    @property
    def total_covered(self) -> int:
        return len(self.covered_lines)

    @property
    def coverage_pct(self) -> float:
        if not self.changed_lines:
            return 100.0
        return (len(self.covered_lines) / len(self.changed_lines)) * 100


@dataclass
class DiffCoverageSummary:
    """Aggregated diff coverage across all files."""

    file_results: list[DiffCoverageResult]

    @property
    def total_changed(self) -> int:
        return sum(r.total_changed for r in self.file_results)

    @property
    def total_covered(self) -> int:
        return sum(r.total_covered for r in self.file_results)

    @property
    def patch_coverage_pct(self) -> float:
        if self.total_changed == 0:
            return 100.0
        return (self.total_covered / self.total_changed) * 100

    @property
    def files_with_uncovered(self) -> list[DiffCoverageResult]:
        return [r for r in self.file_results if r.uncovered_lines]


def parse_diff_hunks(
    commitish: str,
    repo_root: str,
) -> dict[str, list[tuple[int, int]]]:
    """Parse git diff to get changed line ranges per file.

    Runs ``git diff --unified=0 <commitish>`` and extracts the
    ``@@`` hunk headers to determine which lines were added/modified.

    Args:
        commitish: Git ref to diff against (e.g. "origin/dev").
        repo_root: Root directory of the git repo.

    Returns:
        Dict mapping relative filepath to list of (start, end) tuples.
        Both start and end are 1-indexed inclusive.
    """
    result = subprocess.run(
        ["git", "diff", "--unified=0", "--diff-filter=ACMR", commitish],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        logger.error(f"git diff failed: {result.stderr.strip()}")
        return {}

    hunks: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None

    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@") and current_file:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                if count > 0:
                    hunks.setdefault(current_file, []).append(
                        (start, start + count - 1)
                    )

    return hunks


def export_llvm_coverage(
    profdata_path: str,
    binary_path: str,
    tool_commands: dict[str, list[str]],
    source_files: list[str] | None = None,
) -> dict | None:
    """Run llvm-cov export and return parsed JSON.

    Args:
        profdata_path: Path to merged .profdata file.
        binary_path: Path to the instrumented binary.
        tool_commands: Dict from get_llvm_tools().
        source_files: If provided, restrict export to these files
            (absolute paths) to limit JSON size.

    Returns:
        Parsed JSON dict, or None on failure.
    """
    cmd = tool_commands["llvm-cov"] + [
        "export",
        binary_path,
        f"-instr-profile={profdata_path}",
        "-skip-functions",
    ]

    if source_files:
        cmd.extend(source_files)

    logger.info(f"Running llvm-cov export ({len(source_files or [])} files)...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        logger.error(f"llvm-cov export failed: {e.stderr[:500]}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse llvm-cov export JSON: {e}")
        return None


def parse_line_coverage(
    export_data: dict,
) -> dict[str, dict[int, int]]:
    """Extract per-line execution counts from llvm-cov export JSON.

    Converts the segment-based coverage data into a simple
    ``{filepath: {line_number: execution_count}}`` mapping.

    The segment format is ``[line, col, count, hasCount, isRegionEntry, isGapRegion]``.
    Segments define boundaries where coverage changes. Between consecutive
    segments, the execution count is the count from the most recent segment.

    Args:
        export_data: Parsed JSON from llvm-cov export.

    Returns:
        Dict mapping absolute filepath to ``{line_number: count}``.
    """
    file_coverage: dict[str, dict[int, int]] = {}

    for data_entry in export_data.get("data", []):
        for file_data in data_entry.get("files", []):
            filepath = file_data["filename"]
            segments = file_data.get("segments", [])
            line_counts: dict[int, int] = {}

            for i, seg in enumerate(segments):
                seg_line = seg[0]
                seg_count = seg[2]
                has_count = seg[3]

                if not has_count:
                    continue

                # Determine end line from next segment
                if i + 1 < len(segments):
                    next_line = segments[i + 1][0]
                    next_col = segments[i + 1][1]
                    # If next segment starts at col 1, it owns that line
                    end_line = next_line - 1 if next_col == 1 else next_line
                else:
                    end_line = seg_line

                for line_no in range(seg_line, end_line + 1):
                    # Max-count: line is "covered" if any region on it executed
                    if line_no in line_counts:
                        line_counts[line_no] = max(line_counts[line_no], seg_count)
                    else:
                        line_counts[line_no] = seg_count

            file_coverage[filepath] = line_counts

    return file_coverage


def compute_diff_coverage(
    diff_hunks: dict[str, list[tuple[int, int]]],
    line_coverage: dict[str, dict[int, int]],
    repo_root: str,
) -> DiffCoverageSummary:
    """Cross-reference diff hunks with coverage data.

    Only counts lines that appear in BOTH the diff and the coverage data
    (non-executable lines like blanks and comments are skipped).

    Args:
        diff_hunks: From parse_diff_hunks(), relative paths to line ranges.
        line_coverage: From parse_line_coverage(), absolute paths to line counts.
        repo_root: To convert between relative and absolute paths.

    Returns:
        DiffCoverageSummary with per-file results.
    """
    results: list[DiffCoverageResult] = []

    # Build realpath lookup for coverage data
    cov_by_realpath: dict[str, dict[int, int]] = {}
    for abs_path, counts in line_coverage.items():
        cov_by_realpath[os.path.realpath(abs_path)] = counts

    for rel_path, ranges in sorted(diff_hunks.items()):
        # Filter to source files
        if not rel_path.endswith(SOURCE_EXTENSIONS):
            continue
        if any(rel_path.startswith(p) for p in SKIP_PREFIXES):
            continue

        # Expand ranges to individual line numbers
        all_changed: set[int] = set()
        for start, end in ranges:
            all_changed.update(range(start, end + 1))

        # Find coverage data for this file
        abs_path = os.path.join(repo_root, rel_path)
        real_path = os.path.realpath(abs_path)
        file_cov = cov_by_realpath.get(real_path)

        if file_cov is None:
            results.append(
                DiffCoverageResult(
                    filepath=rel_path,
                    changed_lines=all_changed,
                    uncovered_lines=all_changed,
                    no_coverage_data=True,
                )
            )
            continue

        covered = set()
        uncovered = set()
        for line in all_changed:
            if line in file_cov:
                if file_cov[line] > 0:
                    covered.add(line)
                else:
                    uncovered.add(line)
            # Lines not in file_cov are non-executable — don't count them

        results.append(
            DiffCoverageResult(
                filepath=rel_path,
                changed_lines=covered | uncovered,
                covered_lines=covered,
                uncovered_lines=uncovered,
            )
        )

    return DiffCoverageSummary(file_results=results)


def _group_lines_with_context(
    lines: list[int], context: int, max_line: int
) -> list[tuple[int, int]]:
    """Group nearby line numbers into (start, end) regions with context.

    Args:
        lines: Sorted list of line numbers.
        context: Number of context lines around each region.
        max_line: Maximum line number in the file.

    Returns:
        List of (start, end) tuples, 1-indexed inclusive.
    """
    if not lines:
        return []

    regions: list[tuple[int, int]] = []
    region_start = max(1, lines[0] - context)
    region_end = min(max_line, lines[0] + context)

    for line in lines[1:]:
        new_start = max(1, line - context)
        new_end = min(max_line, line + context)

        if new_start <= region_end + 1:
            region_end = new_end
        else:
            regions.append((region_start, region_end))
            region_start = new_start
            region_end = new_end

    regions.append((region_start, region_end))
    return regions


def display_diff_coverage(
    summary: DiffCoverageSummary,
    repo_root: str,
    context_lines: int = 3,
) -> None:
    """Display diff coverage results using Rich panels and syntax highlighting.

    Args:
        summary: The computed diff coverage summary.
        repo_root: To read source files for display.
        context_lines: Lines of context around uncovered regions.
    """
    # Overall summary
    pct = summary.patch_coverage_pct
    color = "green" if pct >= 80 else "yellow" if pct >= 50 else "red"
    console.print(
        Panel(
            f"Patch Coverage: [{color}]{pct:.1f}%[/{color}] "
            f"({summary.total_covered}/{summary.total_changed} lines)",
            title="Diff Coverage Report",
            border_style=color,
        )
    )

    for result in summary.file_results:
        if result.no_coverage_data:
            console.print(f"\n[dim]{result.filepath} - no coverage data[/dim]")
            continue

        if not result.uncovered_lines:
            # Fully covered — brief one-liner
            console.print(
                f"\n[bold]{result.filepath}[/bold] "
                f"[green]{result.coverage_pct:.0f}%[/green] "
                f"({result.total_covered}/{result.total_changed})"
            )
            continue

        file_color = "red" if result.coverage_pct < 50 else "yellow"
        console.print(
            f"\n[bold]{result.filepath}[/bold] "
            f"[{file_color}]{result.coverage_pct:.0f}%[/{file_color}] "
            f"({result.total_covered}/{result.total_changed})"
        )

        # Read source file
        abs_path = os.path.join(repo_root, result.filepath)
        try:
            with open(abs_path) as f:
                source_lines = f.readlines()
        except FileNotFoundError:
            console.print("  [dim]Could not read source file[/dim]")
            continue

        # Group uncovered lines into regions with context
        regions = _group_lines_with_context(
            sorted(result.uncovered_lines),
            context_lines,
            len(source_lines),
        )

        for region_start, region_end in regions:
            snippet = "".join(source_lines[region_start - 1 : region_end])

            ext = Path(result.filepath).suffix.lstrip(".")
            lang = {"cpp": "cpp", "h": "cpp", "hpp": "cpp", "ipp": "cpp", "c": "c"}.get(
                ext, "text"
            )

            syntax = Syntax(
                snippet,
                lang,
                line_numbers=True,
                start_line=region_start,
                highlight_lines={
                    line
                    for line in result.uncovered_lines
                    if region_start <= line <= region_end
                },
                theme="monokai",
            )
            label = (
                f"L{region_start}"
                if region_start == region_end
                else f"L{region_start}-{region_end}"
            )
            console.print(
                Panel(
                    syntax,
                    title=f"[red]{label}[/red]",
                    border_style="red",
                    expand=False,
                )
            )


def do_diff_coverage_report(
    build_dir: str,
    commitish: str,
    prefix: str | None = None,
    context_lines: int = 3,
) -> bool:
    """Generate and display a diff coverage report.

    Orchestrates the full flow: parse diff, export coverage, cross-reference,
    and display results.

    Args:
        build_dir: Build directory with profraw/profdata and binary.
        commitish: Git ref to diff against (e.g. "origin/dev").
        prefix: Coverage file prefix filter.
        context_lines: Context lines for display.

    Returns:
        True if successful.
    """
    if not check_coverage_build(build_dir):
        return False

    tool_commands = get_llvm_tools()
    if tool_commands is None:
        return False

    profdata = find_and_merge_profdata(build_dir, tool_commands, prefix)
    if profdata is None:
        return False

    binary = find_binary(build_dir)
    if binary is None:
        return False

    repo_root = get_xahaud_root()

    # Parse diff
    diff_hunks = parse_diff_hunks(commitish, repo_root)
    if not diff_hunks:
        logger.info(f"No changes found since {commitish}")
        return True

    source_hunks = {
        k: v
        for k, v in diff_hunks.items()
        if k.endswith(SOURCE_EXTENSIONS)
        and not any(k.startswith(p) for p in SKIP_PREFIXES)
    }
    logger.info(f"Found changes in {len(source_hunks)} source files since {commitish}")

    if not source_hunks:
        logger.info("No source file changes to analyze")
        return True

    # Export coverage (restricted to changed files for performance)
    abs_source_files = [os.path.join(repo_root, f) for f in source_hunks]
    export_data = export_llvm_coverage(
        str(profdata), str(binary), tool_commands, abs_source_files
    )
    if export_data is None:
        return False

    # Parse and cross-reference
    line_coverage = parse_line_coverage(export_data)
    summary = compute_diff_coverage(diff_hunks, line_coverage, repo_root)

    # Display
    display_diff_coverage(summary, repo_root, context_lines)

    return True


# ── v2 (gcovr) support ──────────────────────────────────────────────


def _detect_gcov_tool() -> str:
    """Detect the gcov executable, using llvm-cov gcov on macOS if available."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["xcrun", "-f", "llvm-cov"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                tool = f"{result.stdout.strip()} gcov"
                logger.debug(f"Using gcov tool: {tool}")
                return tool
        except FileNotFoundError:
            pass
    return "gcov"


def _build_gcovr_cmd(
    build_dir: str,
    repo_root: str,
    filter_files: list[str] | None = None,
) -> list[str]:
    """Build the base gcovr command with standard flags."""
    gcov_tool = _detect_gcov_tool()
    jobs = str(os.cpu_count() or 1)

    cmd = [
        "gcovr",
        "--gcov-executable",
        gcov_tool,
        "--gcov-ignore-parse-errors=negative_hits.warn_once_per_file",
        "--gcov-ignore-errors=source_not_found",
        "--gcov-ignore-errors=no_working_dir_found",
        "--merge-mode-functions=merge-use-line-0",
        "-r",
        repo_root,
        "--exclude-throw-branches",
        "--exclude-noncode-lines",
        "--exclude-unreachable-branches",
        "-s",
        "-j",
        jobs,
        "-e",
        "src/test",
        "-e",
        "src/tests",
        f"--object-directory={build_dir}",
    ]

    if filter_files:
        for ff in filter_files:
            cmd += ["--filter", ff]

    return cmd


def _run_gcovr_json(
    build_dir: str,
    repo_root: str,
    filter_files: list[str] | None = None,
) -> Path | None:
    """Run gcovr and produce a JSON coverage report.

    Args:
        build_dir: Build directory containing .gcda/.gcno files.
        repo_root: Repository root for source file resolution.
        filter_files: If provided, only include these files (relative paths).

    Returns:
        Path to the JSON report, or None on failure.
    """
    if not shutil.which("gcovr"):
        logger.error("gcovr not found on PATH")
        return None

    report_dir = Path(build_dir) / "coverage"
    report_dir.mkdir(exist_ok=True)
    json_report = report_dir / "diff_coverage.json"

    gcovr_cmd = _build_gcovr_cmd(build_dir, repo_root, filter_files)
    gcovr_cmd += ["--json", str(json_report)]

    logger.info(
        f"Running gcovr for diff coverage ({len(filter_files or [])} file filters)..."
    )
    try:
        subprocess.run(gcovr_cmd, cwd=repo_root, check=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"gcovr failed with exit code {e.returncode}")
        return None

    if not json_report.exists():
        logger.error("gcovr did not produce JSON output")
        return None

    return json_report


def do_generate_coverage_report_v2(
    build_dir: str,
    html: bool = True,
) -> bool:
    """Generate a coverage report using gcovr (v2/PR #661).

    Runs gcovr directly on .gcda/.gcno files in the build directory.
    Produces JSON summary and optionally an HTML detail report.

    Args:
        build_dir: Build directory with .gcda/.gcno files.
        html: If True, also generate an HTML detail report.

    Returns:
        True if successful.
    """
    if not shutil.which("gcovr"):
        logger.error("gcovr not found on PATH")
        return False

    repo_root = get_xahaud_root()
    report_dir = Path(build_dir) / "coverage"
    report_dir.mkdir(exist_ok=True)

    gcovr_cmd = _build_gcovr_cmd(build_dir, repo_root)

    json_report = report_dir / "coverage.json"
    output_args = ["--json", str(json_report)]

    if html:
        html_report = report_dir / "index.html"
        output_args += ["--html-details", str(html_report)]

    # Single gcovr invocation for all output formats
    logger.info("Generating coverage report (v2/gcovr)...")
    try:
        subprocess.run(
            gcovr_cmd + output_args,
            cwd=repo_root,
            check=True,
        )
        logger.info(f"JSON coverage report: {json_report}")
        if html:
            logger.info(f"HTML coverage report: {html_report}")
    except subprocess.CalledProcessError as e:
        if html:
            # Retry without --html-details (may fail with missing sources)
            logger.warning("gcovr with HTML details failed, retrying JSON-only...")
            try:
                subprocess.run(
                    gcovr_cmd + ["--json", str(json_report)],
                    cwd=repo_root,
                    check=True,
                )
                logger.info(f"JSON coverage report: {json_report}")
            except subprocess.CalledProcessError as e2:
                logger.error(f"gcovr JSON report failed: {e2.returncode}")
                return False
        else:
            logger.error(f"gcovr report failed with exit code {e.returncode}")
            return False

    return True


def _parse_gcovr_line_coverage(
    gcovr_json_path: Path,
) -> dict[str, dict[int, int]]:
    """Parse gcovr JSON into per-file line coverage.

    Args:
        gcovr_json_path: Path to gcovr JSON report.

    Returns:
        Dict mapping filepath to {line_number: execution_count}.
    """
    with open(gcovr_json_path) as f:
        cov_data = json.load(f)

    file_coverage: dict[str, dict[int, int]] = {}
    for file_entry in cov_data.get("files", []):
        filepath = file_entry["file"]
        line_hits: dict[int, int] = {}
        for line in file_entry.get("lines", []):
            line_hits[line["line_number"]] = line["count"]
        file_coverage[filepath] = line_hits

    return file_coverage


def do_diff_coverage_report_v2(
    build_dir: str,
    commitish: str,
    context_lines: int = 3,
) -> bool:
    """Generate and display a diff coverage report using gcovr (v2/PR #661).

    Runs gcovr to collect coverage from .gcda files, then cross-references
    with git diff to show uncovered changed lines.

    Args:
        build_dir: Build directory with .gcda/.gcno files.
        commitish: Git ref to diff against (e.g. "origin/dev").
        context_lines: Context lines for display.

    Returns:
        True if successful.
    """
    repo_root = get_xahaud_root()

    # Parse diff
    diff_hunks = parse_diff_hunks(commitish, repo_root)
    if not diff_hunks:
        logger.info(f"No changes found since {commitish}")
        return True

    source_hunks = {
        k: v
        for k, v in diff_hunks.items()
        if k.endswith(SOURCE_EXTENSIONS)
        and not any(k.startswith(p) for p in SKIP_PREFIXES)
    }
    logger.info(f"Found changes in {len(source_hunks)} source files since {commitish}")

    if not source_hunks:
        logger.info("No source file changes to analyze")
        return True

    # Run gcovr filtered to changed files
    json_report = _run_gcovr_json(
        build_dir,
        repo_root,
        filter_files=list(source_hunks.keys()),
    )
    if json_report is None:
        return False

    # Parse and cross-reference
    line_coverage = _parse_gcovr_line_coverage(json_report)
    summary = compute_diff_coverage(diff_hunks, line_coverage, repo_root)

    # Display
    display_diff_coverage(summary, repo_root, context_lines)

    return True
