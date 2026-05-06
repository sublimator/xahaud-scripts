"""LLVM-injected coverage report helpers.

Used by `x-run-tests --coverage-impl llvm-injected`,
`x-coverage-report --coverage-impl llvm-injected`, and
`x-coverage-diff --coverage-impl llvm-injected`. Off-project comparison
path — does not correspond to any rippled-native cmake option. See
`build/cmake.py::cmake_configure` for the build-side instrumentation.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from xahaud_scripts.utils.logging import make_logger
from xahaud_scripts.utils.shell_utils import get_llvm_tool_command

logger = make_logger(__name__)


def _find_rippled_binary(build_dir: str) -> Path | None:
    for name in ("rippled", "rippled.exe"):
        for cand in (Path(build_dir) / name, *Path(build_dir).rglob(name)):
            if cand.is_file() and os.access(cand, os.X_OK):
                return cand
    return None


def do_generate_coverage_report_llvm(build_dir: str) -> bool:
    """Merge .profraw → .profdata, then write llvm-cov reports.

    Outputs into <build-dir>/coverage/:
      - coverage.profdata    (merged binary)
      - coverage.lcov        (lcov-format export, agent/CI-friendly)
      - coverage.json        (llvm-cov export JSON)
      - coverage.html-summary.txt  (one-shot text summary)
    """
    bp = Path(build_dir)
    if not bp.is_dir():
        logger.error(f"Build directory not found: {build_dir}")
        return False

    profraw_files = sorted(bp.rglob("*.profraw"))
    if not profraw_files:
        logger.error(
            f"No .profraw files under {build_dir}. "
            "Run an llvm-injected coverage test pass first."
        )
        return False

    binary = _find_rippled_binary(build_dir)
    if binary is None:
        logger.error(f"Could not find rippled binary under {build_dir}")
        return False

    report_dir = bp / "coverage"
    report_dir.mkdir(exist_ok=True)
    profdata = report_dir / "coverage.profdata"

    profdata_cmd = get_llvm_tool_command("llvm-profdata")
    cov_cmd = get_llvm_tool_command("llvm-cov")

    logger.info(f"Merging {len(profraw_files)} .profraw → {profdata}")
    try:
        subprocess.run(
            [
                *profdata_cmd,
                "merge",
                "-sparse",
                *map(str, profraw_files),
                "-o",
                str(profdata),
            ],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"llvm-profdata merge failed: rc={e.returncode}")
        return False

    base_args = [
        str(binary),
        f"-instr-profile={profdata}",
    ]

    json_report = report_dir / "coverage.json"
    lcov_report = report_dir / "coverage.lcov"
    summary = report_dir / "coverage-summary.txt"

    logger.info(f"Writing JSON export: {json_report}")
    try:
        with open(json_report, "w") as f:
            subprocess.run(
                [*cov_cmd, "export", *base_args, "-format=text"],
                check=True,
                stdout=f,
            )
    except subprocess.CalledProcessError as e:
        logger.error(f"llvm-cov export json failed: rc={e.returncode}")
        return False

    logger.info(f"Writing lcov export: {lcov_report}")
    try:
        with open(lcov_report, "w") as f:
            subprocess.run(
                [*cov_cmd, "export", *base_args, "-format=lcov"],
                check=True,
                stdout=f,
            )
    except subprocess.CalledProcessError as e:
        logger.error(f"llvm-cov export lcov failed: rc={e.returncode}")
        return False

    logger.info(f"Writing summary: {summary}")
    try:
        with open(summary, "w") as f:
            subprocess.run(
                [*cov_cmd, "report", *base_args],
                check=True,
                stdout=f,
            )
    except subprocess.CalledProcessError as e:
        logger.error(f"llvm-cov report failed: rc={e.returncode}")
        return False

    logger.info(f"LLVM coverage report ready: {report_dir}")
    return True


def _ensure_profdata(build_dir: str) -> Path | None:
    """Return path to coverage.profdata, merging from .profraw if needed."""
    bp = Path(build_dir)
    profdata = bp / "coverage" / "coverage.profdata"
    if profdata.is_file():
        return profdata

    profraw_files = sorted(bp.rglob("*.profraw"))
    if not profraw_files:
        logger.error(
            f"No .profraw files under {build_dir}. "
            "Run an llvm-injected coverage test pass first."
        )
        return None

    profdata.parent.mkdir(parents=True, exist_ok=True)
    profdata_cmd = get_llvm_tool_command("llvm-profdata")
    logger.info(f"Merging {len(profraw_files)} .profraw → {profdata}")
    try:
        subprocess.run(
            [
                *profdata_cmd,
                "merge",
                "-sparse",
                *map(str, profraw_files),
                "-o",
                str(profdata),
            ],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"llvm-profdata merge failed: rc={e.returncode}")
        return None
    return profdata


def _llvm_cov_export_data(
    binary: Path, profdata: Path
) -> tuple[
    dict[str, dict[int, int]],
    dict[str, dict[int, list[tuple[int, int, int]]]],
]:
    """Run llvm-cov export and return (line_coverage, branch_coverage).

    line_coverage: {filepath: {line: hits}} — derived from segments. llvm-cov
        segments are [line, col, count, has_count, is_region_entry,
        is_gap_region]. Each segment marks a region boundary; lines inherit
        the most-recent segment's count until the next one.

    branch_coverage: {filepath: {line: [(col, true_count, false_count), ...]}}
        — per-branch detail keyed by start line. Each branch entry from
        llvm-cov is [start_line, start_col, end_line, end_col, true_count,
        false_count, file_id, expanded_id, region_kind]. We keep one tuple
        per branch (preserving column for inline annotation like
        ``Branch (812:13): [T:24, F:0]``); aggregation to (taken, total) is
        derived on demand by the consumer.
    """
    cov_cmd = get_llvm_tool_command("llvm-cov")
    proc = subprocess.run(
        [
            *cov_cmd,
            "export",
            str(binary),
            f"-instr-profile={profdata}",
            "-skip-functions",
            "-format=text",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)

    line_out: dict[str, dict[int, int]] = {}
    branch_out: dict[str, dict[int, list[tuple[int, int, int]]]] = {}
    for entry in data.get("data", []):
        for f in entry.get("files", []):
            path = f.get("filename")
            if not path:
                continue
            line_hits: dict[int, int] = {}
            segments = f.get("segments") or []
            for i, seg in enumerate(segments):
                seg_line, _seg_col, count, has_count, *_ = seg
                if not has_count:
                    continue
                if i + 1 < len(segments):
                    next_line = segments[i + 1][0]
                    next_col = segments[i + 1][1]
                    end_line = next_line - 1 if next_col == 1 else next_line
                else:
                    end_line = seg_line
                for ln in range(seg_line, end_line + 1):
                    if ln in line_hits:
                        line_hits[ln] = max(line_hits[ln], count)
                    else:
                        line_hits[ln] = count
            line_out[path] = line_hits

            line_branches: dict[int, list[tuple[int, int, int]]] = {}
            for br in f.get("branches") or []:
                if len(br) < 6:
                    continue
                start_line = br[0]
                start_col = br[1]
                true_count = br[4]
                false_count = br[5]
                line_branches.setdefault(start_line, []).append(
                    (start_col, true_count, false_count)
                )
            branch_out[path] = line_branches
    return line_out, branch_out


def _llvm_cov_export_lines(binary: Path, profdata: Path) -> dict[str, dict[int, int]]:
    """Backwards-compatible wrapper returning only line coverage."""
    lines, _ = _llvm_cov_export_data(binary, profdata)
    return lines


def do_diff_coverage_report_llvm(
    build_dir: str,
    commitish: str,
    context_lines: int = 3,
) -> bool:
    """LLVM-injected diff coverage: same flow as the gcovr path, but reads
    line coverage from `llvm-cov export` (after merging .profraw)."""
    from xahaud_scripts.utils.coverage_diff import (
        compute_diff_coverage,
        display_diff_coverage,
        parse_diff_hunks,
    )
    from xahaud_scripts.utils.paths import get_xahaud_root

    repo_root = get_xahaud_root()
    diff_hunks = parse_diff_hunks(commitish, repo_root)
    if not diff_hunks:
        logger.info(f"No changes found since {commitish}")
        return True

    profdata = _ensure_profdata(build_dir)
    if profdata is None:
        return False
    binary = _find_rippled_binary(build_dir)
    if binary is None:
        logger.error(f"Could not find rippled binary under {build_dir}")
        return False

    logger.info("Exporting llvm-cov line + branch coverage...")
    try:
        line_coverage, branch_coverage = _llvm_cov_export_data(binary, profdata)
    except subprocess.CalledProcessError as e:
        logger.error(f"llvm-cov export failed: rc={e.returncode}")
        return False

    summary = compute_diff_coverage(
        diff_hunks, line_coverage, repo_root, branch_coverage=branch_coverage
    )
    display_diff_coverage(summary, repo_root, context_lines)
    return True
