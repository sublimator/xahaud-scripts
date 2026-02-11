#!/usr/bin/env python3
"""Build script for xrpld with rich output and coverage support."""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()
VERBOSE = False


def _find_root() -> Path:
    """Find the xrpld repo root (walks up from cwd looking for .git)."""
    cwd = Path.cwd()
    for p in [cwd, *cwd.parents]:
        if (p / ".git").exists():
            return p
    console.print("[bold red]Not inside a git repository[/bold red]")
    sys.exit(1)


def debug(msg: str) -> None:
    if VERBOSE:
        console.print(f"[dim cyan]\\[debug] {msg}[/dim cyan]")


def parse_diff_hunks(commitish: str, root: Path) -> dict[str, list[tuple[int, int]]]:
    """Parse git diff --unified=0 to get changed line ranges per file.

    Returns {filepath: [(start, end), ...]} where start/end are 1-indexed inclusive.
    """
    result = subprocess.run(
        ["git", "diff", "--unified=0", "--diff-filter=ACMR", commitish],
        capture_output=True,
        text=True,
        cwd=root,
    )
    if result.returncode != 0:
        console.print(f"[bold red]git diff failed: {result.stderr.strip()}[/bold red]")
        sys.exit(1)

    files: dict[str, list[tuple[int, int]]] = {}
    current_file = None

    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            debug(f"diff file: {current_file}")
        elif line.startswith("@@") and current_file:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                if count > 0:
                    debug(f"  hunk: L{start}-{start + count - 1} ({count} lines)")
                    files.setdefault(current_file, []).append(
                        (start, start + count - 1)
                    )

    debug(f"parsed {len(files)} files from diff")
    return files


def show_uncovered_diff(commitish: str, gcovr_json_path: Path, root: Path) -> None:
    """Cross-reference git diff hunks with gcovr JSON to show uncovered changed lines."""
    hunks = parse_diff_hunks(commitish, root)

    with open(gcovr_json_path) as f:
        cov_data = json.load(f)

    # Build coverage lookup: {filepath: {line_no: hit_count}}
    cov_by_file: dict[str, dict[int, int]] = {}
    for file_entry in cov_data.get("files", []):
        fname = file_entry["file"]
        line_hits: dict[int, int] = {}
        for line in file_entry.get("lines", []):
            line_hits[line["line_number"]] = line["count"]
        cov_by_file[fname] = line_hits
        debug(f"coverage: {fname} ({len(line_hits)} executable lines)")

    debug(f"coverage data: {len(cov_by_file)} files")
    debug(f"diff hunks: {len(hunks)} files")

    total_changed = 0
    total_covered = 0

    console.rule("[bold blue]Uncovered Diff Lines")

    for filepath, ranges in sorted(hunks.items()):
        # Skip test files and non-source
        if not filepath.endswith((".cpp", ".h", ".ipp")):
            continue
        if filepath.startswith(("src/test/", "src/tests/", "external/")):
            continue

        line_cov = cov_by_file.get(filepath, {})
        if not line_cov:
            debug(f"  {filepath}: no coverage data (not in gcovr output)")
        else:
            debug(
                f"  {filepath}: {len(line_cov)} executable lines in coverage, "
                f"{len(ranges)} diff hunks"
            )
        uncovered_ranges: list[tuple[int, int]] = []
        file_changed = 0
        file_covered = 0

        for start, end in ranges:
            run_start = None
            for lineno in range(start, end + 1):
                hits = line_cov.get(lineno)
                if hits is None:
                    # Non-executable line (comments, braces, etc.) — don't count
                    debug(f"    L{lineno}: non-executable (skipped)")
                    if run_start is not None:
                        uncovered_ranges.append((run_start, lineno - 1))
                        run_start = None
                    continue
                file_changed += 1
                if hits > 0:
                    file_covered += 1
                    if run_start is not None:
                        uncovered_ranges.append((run_start, lineno - 1))
                        run_start = None
                else:
                    if run_start is None:
                        run_start = lineno
            if run_start is not None:
                uncovered_ranges.append((run_start, end))

        if file_changed == 0:
            continue

        total_changed += file_changed
        total_covered += file_covered

        if uncovered_ranges:
            # Merge ranges within 4 lines of each other into single panels
            merged: list[tuple[int, int]] = [uncovered_ranges[0]]
            for s, e in uncovered_ranges[1:]:
                prev_s, prev_e = merged[-1]
                if s - prev_e <= 4:
                    merged[-1] = (prev_s, e)
                else:
                    merged.append((s, e))

            pct = (file_covered / file_changed * 100) if file_changed else 0
            console.print(
                f"\n[bold]{filepath}[/bold] "
                f"({file_covered}/{file_changed} lines, "
                f"[yellow]{pct:.0f}%[/yellow])"
            )

            # Read source file for code display
            src_path = root / filepath
            src_lines: list[str] = []
            if src_path.exists():
                src_lines = src_path.read_text().splitlines()

            # Collect all uncovered line numbers for highlighting
            uncovered_set: set[int] = set()
            for s, e in uncovered_ranges:
                uncovered_set.update(range(s, e + 1))

            for s, e in merged:
                label = f"L{s}" if s == e else f"L{s}-{e}"
                if src_lines:
                    ctx_start = max(1, s - 1)
                    ctx_end = min(len(src_lines), e + 1)
                    snippet = "\n".join(src_lines[ctx_start - 1 : ctx_end])
                    ext = Path(filepath).suffix.lstrip(".")
                    lang = {"cpp": "cpp", "h": "cpp", "ipp": "cpp"}.get(ext, "text")
                    syn = Syntax(
                        snippet,
                        lang,
                        line_numbers=True,
                        start_line=ctx_start,
                        highlight_lines=uncovered_set
                        & set(range(ctx_start, ctx_end + 1)),
                        theme="monokai",
                    )
                    console.print(
                        Panel(
                            syn,
                            title=f"[red]{label}[/red]",
                            border_style="red",
                            expand=False,
                        )
                    )
                else:
                    console.print(f"  [red]{label}[/red]: uncovered")

    if total_changed > 0:
        pct = total_covered / total_changed * 100
        color = "green" if pct >= 80 else "yellow" if pct >= 50 else "red"
        console.print(
            f"\n[bold]Patch coverage: [{color}]{total_covered}/{total_changed} "
            f"({pct:.1f}%)[/{color}][/bold]"
        )
    else:
        console.print("[yellow]No changed source lines found in diff.[/yellow]")


def run_cmd(
    cmd: list[str], *, cwd: Path | None = None, env: dict | None = None
) -> None:
    """Run a command, streaming output. Raises on failure."""
    merged_env = {**os.environ, **(env or {})}
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd, cwd=cwd, env=merged_env)
    if result.returncode != 0:
        console.print(f"[bold red]Command failed (exit {result.returncode})[/bold red]")
        sys.exit(result.returncode)


@click.command()
@click.option("--coverage", is_flag=True, help="Enable coverage (forces Debug build).")
@click.option(
    "--debug/--release",
    "is_debug",
    default=False,
    help="Build type (default: Release).",
)
@click.option("--ccache", is_flag=True, help="Enable ccache compiler launcher.")
@click.option(
    "--test",
    "test_patterns",
    multiple=True,
    help="Test pattern (repeatable, e.g. --test xrpl.rpc.RPCSub).",
)
@click.option(
    "--cover-file",
    multiple=True,
    help="Filter coverage to specific source files (can repeat).",
)
@click.option(
    "--cover-diff",
    default=None,
    help="Filter coverage to files changed since commitish (e.g. origin/develop).",
)
@click.option(
    "--cover-show-uncovered-diff",
    "uncovered_diff",
    is_flag=True,
    help="Show uncovered lines in diff (uses --cover-diff commitish, default origin/develop).",
)
@click.option("--conan", is_flag=True, help="Run conan install (skipped by default).")
@click.option("--skip-test", is_flag=True, help="Skip test execution.")
@click.option("--clean", is_flag=True, help="Remove build dir before starting.")
@click.option(
    "--jobs",
    type=int,
    default=multiprocessing.cpu_count(),
    help="Parallel jobs.",
)
@click.option(
    "--build-dir",
    type=click.Path(),
    default="build",
    help="Build directory.",
)
@click.option("-v", "--verbose", is_flag=True, help="Verbose/debug logging.")
def main(
    coverage: bool,
    is_debug: bool,
    ccache: bool,
    test_patterns: tuple[str, ...],
    cover_file: tuple[str, ...],
    cover_diff: str | None,
    uncovered_diff: bool,
    conan: bool,
    skip_test: bool,
    clean: bool,
    jobs: int,
    build_dir: str,
    verbose: bool,
) -> None:
    """Build xrpld with optional coverage support."""
    global VERBOSE  # noqa: PLW0603
    VERBOSE = verbose
    root = _find_root()
    build_path = root / build_dir
    build_type = "Debug" if (is_debug or coverage) else "Release"

    console.print(
        Panel(
            f"[bold]build_type=[cyan]{build_type}[/cyan]  "
            f"coverage=[cyan]{coverage}[/cyan]  "
            f"ccache=[cyan]{ccache}[/cyan]  "
            f"jobs=[cyan]{jobs}[/cyan]  "
            f"build_dir=[cyan]{build_dir}[/cyan][/bold]",
            title="xrpld build",
        )
    )

    # Clean
    if clean and build_path.exists():
        console.print("[yellow]Cleaning build directory...[/yellow]")
        shutil.rmtree(build_path)

    build_path.mkdir(exist_ok=True)

    # ── Conan ──
    if conan:
        console.rule("[bold blue]Conan Install")
        run_cmd(["conan", "--version"])
        run_cmd(
            [
                "conan",
                "install",
                "--output-folder",
                ".",
                "--build",
                "missing",
                "-r",
                "conancenter",
                "-r",
                "xrplf",
                "--options:host",
                "&:tests=True",
                "--options:host",
                "&:xrpld=True",
                "--settings:all",
                f"build_type={build_type}",
                "--conf",
                "tools.build:cxxflags="
                "['-Wno-missing-template-arg-list-after-template-kw']",
                "..",
            ],
            cwd=build_path,
            env={"CONAN_CPU_COUNT": "4"},
        )

    # ── CMake Configure ──
    console.rule("[bold blue]CMake Configure")
    cmake_args = [
        "-Dassert=TRUE",
        "-Dwerr=TRUE",
        "-Dtests=TRUE",
        "-Dxrpld=TRUE",
    ]

    # Find conan toolchain file — conan layout() may nest it arbitrarily
    # (e.g. build/generators/, build/build/generators/, or directly in build/)
    toolchain_matches = sorted(build_path.rglob("conan_toolchain.cmake"))
    if toolchain_matches:
        toolchain_file = toolchain_matches[0]
        debug(f"Found toolchain: {toolchain_file}")
        cmake_args.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}")
    else:
        console.print(
            "[bold yellow]Warning: conan_toolchain.cmake not found — "
            "run with --conan first if dependencies are needed[/bold yellow]"
        )

    if coverage:
        cmake_args += [
            "-Dcoverage=ON",
            "-Dcoverage_format=html-details",
            "-DCMAKE_C_FLAGS=-O0",
            "-DCMAKE_CXX_FLAGS=-O0",
        ]

    if ccache:
        cmake_args += [
            "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
            "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
        ]

    run_cmd(
        [
            "cmake",
            "-G",
            "Ninja",
            f"-DCMAKE_BUILD_TYPE={build_type}",
            *cmake_args,
            "-B",
            str(build_path),
            ".",
        ],
        cwd=root,
    )

    # ── Build ──
    console.rule("[bold blue]Build")
    run_cmd(
        [
            "cmake",
            "--build",
            str(build_path),
            "--config",
            build_type,
            "--parallel",
            str(jobs),
        ],
        cwd=root,
    )

    # ── Clear stale coverage data ──
    if coverage and test_patterns:
        gcda_files = list(build_path.rglob("*.gcda"))
        if gcda_files:
            console.print(f"[yellow]Clearing {len(gcda_files)} .gcda files...[/yellow]")
            for f in gcda_files:
                f.unlink()

    # ── Test ──
    if not skip_test:
        console.rule("[bold blue]Tests")
        if test_patterns:
            for i, pat in enumerate(test_patterns):
                console.print(f"[bold]Run {i + 1}/{len(test_patterns)}:[/bold] {pat}")
                run_cmd(
                    [
                        str(build_path / "xrpld"),
                        "--unittest",
                        pat,
                        "--unittest-jobs",
                        str(jobs),
                    ],
                    cwd=root,
                )
        else:
            run_cmd(
                [
                    str(build_path / "xrpld"),
                    "--unittest",
                    "--unittest-jobs",
                    str(jobs),
                ],
                cwd=root,
            )

    # ── Coverage Report ──
    if coverage:
        console.rule("[bold blue]Coverage Report")
        if not shutil.which("gcovr"):
            console.print("[bold red]gcovr not found on PATH[/bold red]")
            sys.exit(1)

        # Detect gcov tool (match CodeCoverage.cmake logic)
        gcov_tool = "gcov"
        try:
            result = subprocess.run(
                ["xcrun", "-f", "llvm-cov"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                gcov_tool = f"{result.stdout.strip()} gcov"
        except FileNotFoundError:
            pass

        report_dir = build_path / "coverage"
        report_dir.mkdir(exist_ok=True)
        report = report_dir / "index.html"

        gcovr_cmd = [
            "gcovr",
            "--gcov-executable",
            gcov_tool,
            "--gcov-ignore-parse-errors=negative_hits.warn_once_per_file",
            "-r",
            str(root),
            "--exclude-throw-branches",
            "--exclude-noncode-lines",
            "--exclude-unreachable-branches",
            "-s",
            "-j",
            str(jobs),
            "-e",
            "src/test",
            "-e",
            "src/tests",
            "-e",
            "include/xrpl/beast/test",
            "-e",
            "include/xrpl/beast/unit_test",
            f"--object-directory={build_path}",
        ]

        # Resolve files to filter
        filter_files = list(cover_file)
        if cover_diff:
            result = subprocess.run(
                [
                    "git",
                    "diff",
                    "--name-only",
                    "--diff-filter=ACMR",
                    cover_diff,
                ],
                capture_output=True,
                text=True,
                cwd=root,
            )
            if result.returncode != 0:
                console.print(
                    f"[bold red]git diff failed: {result.stderr.strip()}[/bold red]"
                )
                sys.exit(1)
            diff_files = [
                f
                for f in result.stdout.strip().splitlines()
                if f.endswith((".cpp", ".h", ".ipp"))
                and not f.startswith(("src/test/", "src/tests/", "external/"))
            ]
            if not diff_files:
                console.print(
                    f"[yellow]No source files changed since {cover_diff}[/yellow]"
                )
            else:
                console.print(f"[bold]Covering {len(diff_files)} changed files:[/bold]")
                for df in diff_files:
                    console.print(f"  [dim]{df}[/dim]")
                filter_files.extend(diff_files)

        if filter_files:
            for ff in filter_files:
                gcovr_cmd += ["--filter", ff]

        json_report = report_dir / "coverage.json"
        gcovr_cmd += [
            "--html-details",
            str(report),
            "--json",
            str(json_report),
        ]

        run_cmd(gcovr_cmd, cwd=root)

        if report.exists():
            console.print(f"\n[bold green]Coverage report:[/bold green] {report}")
        else:
            console.print(
                "[yellow]Coverage report not found at expected path.[/yellow]"
            )

        if uncovered_diff and json_report.exists():
            show_uncovered_diff(cover_diff or "origin/develop", json_report, root)
        elif uncovered_diff:
            console.print(
                "[yellow]JSON coverage data not found, "
                "cannot show uncovered diff.[/yellow]"
            )

    console.print("\n[bold green]Done![/bold green]")


if __name__ == "__main__":
    main()
