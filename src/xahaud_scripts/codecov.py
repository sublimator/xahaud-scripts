#!/usr/bin/env python3
"""Codecov integration for xahaud-scripts.

Pulls per-PR coverage data from the Codecov public API and turns it into
something an agent can act on:

  x-codecov pull       # one-line patch coverage summary
  x-codecov gap        # how many more line-hits to clear the target
  x-codecov diff-lines # uncovered diff lines as a flat list
  x-codecov suggest    # contiguous-line clusters ranked by leverage

Public-repo only for v1 — no auth.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import click
import requests
from rich.console import Console
from rich.table import Table

from xahaud_scripts.utils.logging import make_logger, setup_logging

logger = make_logger(__name__)
console = Console()

API_BASE = "https://api.codecov.io/api/v2/github"
CACHE_DIR = Path.home() / ".cache" / "x-codecov"
DEFAULT_TTL_SECONDS = 5 * 60  # 5 min: Codecov can update mid-CI


# ── Repo discovery ───────────────────────────────────────────────────


def _detect_owner_repo() -> tuple[str, str] | None:
    """Parse owner/repo from `git config --get remote.origin.url`."""
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None

    url = result.stdout.strip()
    # git@github.com:owner/repo.git  or  https://github.com/owner/repo(.git)?
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        return None
    return m.group(1), m.group(2)


# ── HTTP + cache ─────────────────────────────────────────────────────


def _cache_path(owner: str, repo: str, pr_num: int) -> Path:
    return CACHE_DIR / f"{owner}__{repo}__pr{pr_num}.json"


def _fetch_compare(owner: str, repo: str, pr_num: int, refresh: bool = False) -> dict:
    """Fetch the Codecov compare endpoint for a PR (with optional cache)."""
    cache_file = _cache_path(owner, repo, pr_num)
    if not refresh and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < DEFAULT_TTL_SECONDS:
            logger.debug(f"Using cached compare ({int(age)}s old): {cache_file}")
            return json.loads(cache_file.read_text())

    url = f"{API_BASE}/{owner}/repos/{repo}/compare?pullid={pr_num}"
    logger.debug(f"GET {url}")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data))
    logger.debug(f"Cached: {cache_file}")
    return data


def _fetch_pull(owner: str, repo: str, pr_num: int) -> dict:
    """Fetch the small per-PR summary endpoint (uncached, cheap)."""
    url = f"{API_BASE}/{owner}/repos/{repo}/pulls/{pr_num}"
    logger.debug(f"GET {url}")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


# ── Patch-totals + uncovered-line extraction ─────────────────────────


@dataclass
class PatchTotals:
    hits: int
    misses: int
    partials: int
    coverage_pct: float
    target_pct: float | None  # head_totals.coverage when present

    @property
    def total_diff_lines(self) -> int:
        return self.hits + self.misses + self.partials


def _patch_totals_from_compare(data: dict) -> PatchTotals:
    """Aggregate patch totals across files from the compare response."""
    hits = misses = partials = 0
    for f in data.get("files", []):
        t = (f.get("totals") or {}).get("patch") or {}
        hits += int(t.get("hits") or 0)
        misses += int(t.get("misses") or 0)
        partials += int(t.get("partials") or 0)

    diff_total = hits + misses + partials
    coverage_pct = (hits / diff_total * 100) if diff_total else 100.0
    # Compare endpoint nests under totals.head; pulls endpoint uses head_totals.
    totals_root = data.get("totals") or {}
    head_section = (
        (totals_root.get("head") if isinstance(totals_root, dict) else None)
        or data.get("head_totals")
        or {}
    )
    target_pct = head_section.get("coverage")
    return PatchTotals(
        hits=hits,
        misses=misses,
        partials=partials,
        coverage_pct=coverage_pct,
        target_pct=float(target_pct) if target_pct is not None else None,
    )


def _uncovered_lines(data: dict) -> list[tuple[str, int, str]]:
    """Return [(filepath, line_no, source_line)] for added-and-uncovered lines."""
    out: list[tuple[str, int, str]] = []
    for f in data.get("files", []):
        path = (f.get("name") or {}).get("head")
        if not path:
            continue
        for ln in f.get("lines") or []:
            if not (ln.get("is_diff") and ln.get("added")):
                continue
            cov = (ln.get("coverage") or {}).get("head")
            # 0 = miss; treat partial (list/dict/non-int) as miss too — keeps
            # things conservative and matches what the user wants to fix.
            if cov == 0 or (cov is not None and not isinstance(cov, int)):
                num = (ln.get("number") or {}).get("head")
                value = ln.get("value") or ""
                if num is not None:
                    out.append((path, int(num), value.rstrip()))
    out.sort()
    return out


# ── Clustering ───────────────────────────────────────────────────────


@dataclass
class Cluster:
    filepath: str
    start: int
    end: int
    lines: list[tuple[int, str]]  # (line_no, source)

    @property
    def size(self) -> int:
        return len(self.lines)


def _cluster(lines: list[tuple[str, int, str]], gap: int = 2) -> list[Cluster]:
    """Group contiguous uncovered lines per file (gap-tolerant)."""
    clusters: list[Cluster] = []
    cur_path: str | None = None
    cur_lines: list[tuple[int, str]] = []

    def flush() -> None:
        if cur_path and cur_lines:
            clusters.append(
                Cluster(
                    filepath=cur_path,
                    start=cur_lines[0][0],
                    end=cur_lines[-1][0],
                    lines=list(cur_lines),
                )
            )

    for path, num, value in lines:
        if cur_path == path and cur_lines and num - cur_lines[-1][0] <= gap:
            cur_lines.append((num, value))
        else:
            flush()
            cur_path = path
            cur_lines = [(num, value)]
    flush()
    return clusters


# ── Resolution helpers (PR + owner/repo) ─────────────────────────────


def _resolve(pr_num: int, owner: str | None, repo: str | None) -> tuple[str, str]:
    if owner and repo:
        return owner, repo
    detected = _detect_owner_repo()
    if not detected:
        raise click.ClickException(
            "Could not detect owner/repo. Pass --owner and --repo, or run "
            "from a checkout whose origin remote points at GitHub."
        )
    return detected


# ── CLI ──────────────────────────────────────────────────────────────


_owner_opt = click.option("--owner", default=None, help="GitHub owner (auto-detected).")
_repo_opt = click.option("--repo", default=None, help="GitHub repo (auto-detected).")
_refresh_opt = click.option(
    "--refresh", is_flag=True, help="Bypass the cache and re-fetch from Codecov."
)
_log_opt = click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    default=None,
    help="Log level (default: info on tty, warning otherwise).",
)
_quiet_opt = click.option(
    "-q",
    "--quiet",
    is_flag=True,
    help="Suppress the init log line; same as --log-level error.",
)
_format_opt = click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text).",
)


def _init_logging(log_level: str | None, quiet: bool) -> None:
    """Pick a sensible default: info on tty, warning otherwise. --quiet → error."""
    if quiet:
        level = "error"
    elif log_level is not None:
        level = log_level
    else:
        level = "info" if sys.stdout.isatty() else "warning"
    setup_logging(level, logger)


@click.group()
def cli() -> None:
    """Query Codecov for patch coverage on a PR. Public repos only (no auth)."""


@cli.command("pull")
@click.argument("pr_num", type=int)
@_owner_opt
@_repo_opt
@_format_opt
@_quiet_opt
@_log_opt
def pull(
    pr_num: int,
    owner: str | None,
    repo: str | None,
    output_format: str,
    quiet: bool,
    log_level: str | None,
) -> None:
    """One-liner summary for the PR (matches what Codecov bot reports)."""
    _init_logging(log_level, quiet)
    owner, repo = _resolve(pr_num, owner, repo)
    summary = _fetch_pull(owner, repo, pr_num)
    patch = summary.get("patch") or {}
    head_totals = summary.get("head_totals") or {}
    base_totals = summary.get("base_totals") or {}

    pct = patch.get("coverage")
    head = head_totals.get("coverage")
    base = base_totals.get("coverage")

    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "owner": owner,
                    "repo": repo,
                    "pr": pr_num,
                    "patch_coverage": float(pct) if pct is not None else None,
                    "hits": patch.get("hits", 0),
                    "misses": patch.get("misses", 0),
                    "partials": patch.get("partials", 0),
                    "head_total": float(head) if head is not None else None,
                    "base_total": float(base) if base is not None else None,
                },
            )
        )
        return

    pct_str = f"{float(pct):.2f}%" if pct is not None else "—"
    console.print(
        f"[bold]{owner}/{repo} #{pr_num}[/bold]   "
        f"patch coverage: [cyan]{pct_str}[/cyan]   "
        f"hits/misses/partials: "
        f"{patch.get('hits', 0)}/{patch.get('misses', 0)}/{patch.get('partials', 0)}"
    )
    if head is not None:
        delta = float(head) - float(base) if base is not None else None
        delta_str = f"  Δ {delta:+.2f}" if delta is not None else ""
        console.print(
            f"  head total: [cyan]{float(head):.2f}%[/cyan]"
            + (f"   base total: {float(base):.2f}%" if base is not None else "")
            + delta_str
        )


@cli.command("gap")
@click.argument("pr_num", type=int)
@_owner_opt
@_repo_opt
@_refresh_opt
@click.option(
    "--target",
    type=float,
    default=None,
    help="Target coverage % (default: head_totals from Codecov).",
)
@_format_opt
@_quiet_opt
@_log_opt
def gap(
    pr_num: int,
    owner: str | None,
    repo: str | None,
    refresh: bool,
    target: float | None,
    output_format: str,
    quiet: bool,
    log_level: str | None,
) -> None:
    """How many more line-hits to clear the target patch-coverage threshold."""
    _init_logging(log_level, quiet)
    owner, repo = _resolve(pr_num, owner, repo)
    data = _fetch_compare(owner, repo, pr_num, refresh=refresh)
    totals = _patch_totals_from_compare(data)
    target_pct = target if target is not None else totals.target_pct

    if target_pct is None:
        raise click.ClickException(
            "No target coverage available (head_totals missing). "
            "Pass --target explicitly."
        )

    if totals.total_diff_lines == 0:
        if output_format == "json":
            click.echo(json.dumps({"diff_lines": 0, "needed_hits": 0}))
        else:
            console.print("[yellow]No diff lines counted.[/yellow]")
        return

    # Solve: (hits + extra) / total >= target  →  extra >= total*target - hits
    extra = max(
        0,
        int(
            (target_pct / 100.0 * totals.total_diff_lines)
            - totals.hits
            + 0.999  # round up
        ),
    )

    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "patch_coverage": totals.coverage_pct,
                    "hits": totals.hits,
                    "total_diff_lines": totals.total_diff_lines,
                    "target_pct": target_pct,
                    "needed_hits": extra,
                }
            )
        )
        return

    console.print(
        f"patch: [cyan]{totals.coverage_pct:.2f}%[/cyan] "
        f"({totals.hits}/{totals.total_diff_lines})   "
        f"target: [yellow]{target_pct:.2f}%[/yellow]   "
        + (
            "[green]already at target[/green]"
            if extra == 0
            else f"need [bold]{extra}[/bold] more hit(s)"
        )
    )


@cli.command("diff-lines")
@click.argument("pr_num", type=int)
@_owner_opt
@_repo_opt
@_refresh_opt
@click.option("--file", "filepath", default=None, help="Filter to a single file.")
@_format_opt
@_quiet_opt
@_log_opt
def diff_lines(
    pr_num: int,
    owner: str | None,
    repo: str | None,
    refresh: bool,
    filepath: str | None,
    output_format: str,
    quiet: bool,
    log_level: str | None,
) -> None:
    """List uncovered added-by-PR lines."""
    _init_logging(log_level, quiet)
    owner, repo = _resolve(pr_num, owner, repo)
    data = _fetch_compare(owner, repo, pr_num, refresh=refresh)
    lines = _uncovered_lines(data)

    if filepath:
        lines = [item for item in lines if item[0] == filepath]

    if output_format == "json":
        click.echo(
            json.dumps(
                [
                    {"file": p, "line": n, "source": s.lstrip("+").rstrip()}
                    for p, n, s in lines
                ]
            )
        )
        return

    if not lines:
        console.print("[green]No uncovered diff lines.[/green]")
        return

    # Group by file: filename header, indented "L<num>: source" rows below.
    # Long file paths no longer get table-wrapped mid-string.
    current_file: str | None = None
    for path, num, src in lines:
        if path != current_file:
            if current_file is not None:
                console.print()
            console.print(f"[bold]{path}[/bold]")
            current_file = path
        console.print(f"  [cyan]L{num}[/cyan]  {src.lstrip('+').rstrip()}")


@cli.command("suggest")
@click.argument("pr_num", type=int)
@_owner_opt
@_repo_opt
@_refresh_opt
@click.option(
    "--gap-tolerance",
    "--gap",
    "gap_tolerance",
    type=int,
    default=2,
    help="Max line distance for clustering (default: 2). Alias: --gap.",
)
@click.option(
    "--target",
    type=float,
    default=None,
    help="Target coverage % (default: head_totals from Codecov).",
)
@click.option(
    "--top",
    type=int,
    default=8,
    help="Show top N clusters (default: 8).",
)
@_format_opt
@_quiet_opt
@_log_opt
def suggest(
    pr_num: int,
    owner: str | None,
    repo: str | None,
    refresh: bool,
    gap_tolerance: int,
    target: float | None,
    top: int,
    output_format: str,
    quiet: bool,
    log_level: str | None,
) -> None:
    """Rank uncovered-line clusters by leverage; suggest the cheapest combo to target."""
    _init_logging(log_level, quiet)
    owner, repo = _resolve(pr_num, owner, repo)
    data = _fetch_compare(owner, repo, pr_num, refresh=refresh)
    totals = _patch_totals_from_compare(data)
    target_pct = target if target is not None else totals.target_pct

    uncovered = _uncovered_lines(data)
    clusters = _cluster(uncovered, gap=gap_tolerance)
    clusters.sort(key=lambda c: c.size, reverse=True)

    def _cluster_loc(c: Cluster) -> str:
        return (
            f"{c.filepath}:{c.start}"
            if c.start == c.end
            else f"{c.filepath}:{c.start}-{c.end}"
        )

    # Greedy biggest-first cheapest-combo (used by both text + json output)
    needed_hits = 0
    chosen: list[Cluster] = []
    accum = 0
    if target_pct is not None and totals.total_diff_lines > 0:
        needed_hits = max(
            0,
            int((target_pct / 100.0 * totals.total_diff_lines) - totals.hits + 0.999),
        )
        if needed_hits > 0:
            for c in clusters:
                if accum >= needed_hits:
                    break
                chosen.append(c)
                accum += c.size

    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "patch_coverage": totals.coverage_pct,
                    "hits": totals.hits,
                    "total_diff_lines": totals.total_diff_lines,
                    "target_pct": target_pct,
                    "needed_hits": needed_hits,
                    "gap_tolerance": gap_tolerance,
                    "clusters": [
                        {
                            "filepath": c.filepath,
                            "start": c.start,
                            "end": c.end,
                            "size": c.size,
                            "projected_pct": (
                                (totals.hits + c.size) / totals.total_diff_lines * 100
                                if totals.total_diff_lines
                                else None
                            ),
                        }
                        for c in clusters[:top]
                    ],
                    "combo": {
                        "clusters": [
                            {
                                "filepath": c.filepath,
                                "start": c.start,
                                "end": c.end,
                                "size": c.size,
                            }
                            for c in chosen
                        ],
                        "size": accum,
                        "clears_target": (
                            target_pct is not None and accum >= needed_hits
                        ),
                        "projected_pct": (
                            (totals.hits + accum) / totals.total_diff_lines * 100
                            if totals.total_diff_lines
                            else None
                        ),
                    },
                }
            )
        )
        return

    # ── Text output ────────────────────────────────────────────────
    if not clusters:
        console.print("[green]No uncovered diff lines — nothing to suggest.[/green]")
        return
    if totals.total_diff_lines == 0:
        console.print("[yellow]No diff lines counted.[/yellow]")
        return

    target_str = f"{target_pct:.2f}%" if target_pct is not None else "—"
    console.print(
        f"Patch: [cyan]{totals.hits}/{totals.total_diff_lines}[/cyan] = "
        f"[cyan]{totals.coverage_pct:.2f}%[/cyan]   "
        f"target: [yellow]{target_str}[/yellow]"
    )
    console.print()

    table = Table(title=f"Top clusters by leverage (gap≤{gap_tolerance})")
    table.add_column("#", justify="right")
    table.add_column("location")
    table.add_column("lines", justify="right")
    table.add_column("if covered →", justify="right")
    for i, c in enumerate(clusters[:top], start=1):
        new_pct = (totals.hits + c.size) / totals.total_diff_lines * 100
        table.add_row(str(i), _cluster_loc(c), str(c.size), f"{new_pct:.2f}%")
    console.print(table)

    if target_pct is None:
        return
    if needed_hits <= 0:
        console.print("[green]Already at target — no combo needed.[/green]")
        return
    if accum < needed_hits:
        console.print(
            f"[red]Even covering all {len(clusters)} clusters "
            f"({sum(c.size for c in clusters)} lines) wouldn't clear the "
            f"{target_pct:.2f}% target.[/red]"
        )
        return

    final_pct = (totals.hits + accum) / totals.total_diff_lines * 100
    console.print(
        f"\nCheapest combo to clear target ({len(chosen)} cluster(s), {accum} lines):"
    )
    if len(chosen) <= 3:
        # Inline (short combos read fine on one line).
        joined = ", ".join(_cluster_loc(c) for c in chosen)
        console.print(f"  [bold]{joined}[/bold]")
    else:
        for c in chosen:
            console.print(f"  • [bold]{_cluster_loc(c)}[/bold] ({c.size} lines)")
    console.print(f"  → [green]{final_pct:.2f}%[/green]")


if __name__ == "__main__":
    cli()
