"""`x-inspect-net` — inspect live Xahau/XRPL networks (no local node needed)."""

from __future__ import annotations

import json
from pathlib import Path

import click
import requests
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text

from xahaud_scripts.inspect_net import amendments as amd
from xahaud_scripts.inspect_net import zombies as zmb
from xahaud_scripts.inspect_net.crawl import Crawler, CrawlStats, parse_seed
from xahaud_scripts.inspect_net.networks import (
    AMENDMENT_NETWORKS,
    NETWORKS,
)
from xahaud_scripts.utils.paths import get_xahaud_root

console = Console()
err = Console(stderr=True)

# status bucket -> (label, rich style)
_STATUS_STYLE: dict[str, tuple[str, str]] = {
    amd.STATUS_ENABLED: ("ENABLED", "bold green"),
    amd.STATUS_MAJORITY: ("majority", "yellow"),
    amd.STATUS_PENDING: ("pending", "cyan"),
    amd.STATUS_VETOED: ("vetoed", "red"),
    amd.STATUS_OBSOLETE: ("obsolete", "dim"),
    amd.STATUS_UNSUPPORTED: ("unsupported", "magenta"),
    amd.STATUS_ABSENT: ("—", "dim"),
}


def _status_text(status: str) -> Text:
    label, style = _STATUS_STYLE.get(status, (status, ""))
    return Text(label, style=style)


def _cell(a: amd.Amendment | None) -> Text:
    """Compact, informative cell for the compare table.

    Prefers a real number over the opaque status word: majority -> activation
    date, active voting -> yes-votes/validators, else the status label.
    """
    if a is None:
        return Text("—", style="dim")
    status = a.status()
    if status == amd.STATUS_MAJORITY:
        eta = a.activation_eta()
        return Text(f"→{eta:%b %d}" if eta else "majority", style="yellow")
    if status == amd.STATUS_PENDING and a.vote_fraction:
        passing = a.threshold is not None and (a.count or 0) >= a.threshold
        return Text(f"{a.count}/{a.validations}", style="green" if passing else "cyan")
    return _status_text(status)


@click.group()
def main() -> None:
    """Inspect live Xahau/XRPL networks: amendment status and version mix."""


# --------------------------------------------------------------------------- #
#  amendments
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--net",
    type=click.Choice(AMENDMENT_NETWORKS),
    default=None,
    help="Single network (omit to compare mainnet vs testnet).",
)
@click.option("--url", default=None, help="Custom JSON-RPC endpoint (single net).")
@click.option("--check", metavar="NAME", default=None, help="Highlight one amendment.")
@click.option(
    "--pending", is_flag=True, help="Single-net view: list only not-enabled amendments."
)
@click.option(
    "--diff-only", is_flag=True, help="Compare view: show only amendments that differ."
)
@click.option(
    "--samples",
    type=int,
    default=1,
    show_default=True,
    help="Query each endpoint N times to cross-reference load-balanced backends "
    "and flag node-local veto/vote fields.",
)
@click.option("--timeout", type=float, default=25.0, show_default=True)
@click.option("--json", "json_path", metavar="PATH", help="Dump raw features to JSON.")
def amendments(
    net: str | None,
    url: str | None,
    check: str | None,
    pending: bool,
    diff_only: bool,
    samples: int,
    timeout: float,
    json_path: str | None,
) -> None:
    """Show enabled/pending/vetoed amendments; diff mainnet vs testnet.

    The cross-network delta is computed from ``enabled`` (network truth — every
    synced node agrees). Veto/vote tallies are the queried node's view; on a
    load-balanced public endpoint they can vary, so use --samples N to detect
    that (distinct backends are reported and node-local fields get a ~ marker).
    """
    if url:
        targets = {"custom": url}
    elif net:
        targets = {net: NETWORKS[net].rpc_url}
    else:
        targets = {n: NETWORKS[n].rpc_url for n in AMENDMENT_NETWORKS}

    fetched: dict[str, amd.NetworkAmendments] = {}
    for name, endpoint in targets.items():
        try:
            fetched[name] = amd.fetch_sampled(
                endpoint, timeout, samples, want_seq=len(targets) == 1 or samples > 1
            )
        except requests.RequestException as exc:
            err.print(f"[red]{name}[/red] ({endpoint}): request failed: {exc}")

    if not fetched:
        raise click.ClickException("no networks reachable")

    if len(fetched) == 1:
        ((name, data),) = fetched.items()
        _render_single(name, targets[name], data, check, pending)
    else:
        _render_compare(targets, fetched, check, diff_only)

    if json_path:
        raw = {
            name: {
                "url": targets[name],
                "ledger_seq": data.ledger_seq,
                "samples": data.samples,
                "backend_nodes": data.nodes,
                "builds": data.builds,
                "enabled_unstable": sorted(data.enabled_unstable),
                "nodeview_varied": sorted(data.nodeview_varied),
                "amendments": [vars(a) for a in data.amendments],
            }
            for name, data in fetched.items()
        }
        Path(json_path).write_text(json.dumps(raw, indent=2))
        err.print(f"  wrote raw data -> {json_path}")


def _render_single(
    name: str,
    url: str,
    data: amd.NetworkAmendments,
    check: str | None,
    pending_only: bool,
) -> None:
    enabled = [a for a in data.amendments if a.enabled]
    not_enabled = [a for a in data.amendments if not a.enabled]
    head = (
        f"[bold]{name.upper()}[/bold]  [dim]{url}[/dim]\n"
        f"amendments: {len(data.amendments)} total · "
        f"[green]{len(enabled)} enabled[/green] · "
        f"[yellow]{len(not_enabled)} not enabled[/yellow]"
    )
    if data.ledger_seq:
        head += f"\nvalidated ledger: {data.ledger_seq}"
    console.print(Panel(head, expand=False))
    _print_backends({name: data})

    rows = not_enabled if pending_only else data.amendments
    table = Table(show_header=True, header_style="bold")
    table.add_column("Amendment")
    table.add_column("Status")
    table.add_column("Detail", style="dim")
    for a in rows:
        label = a.name + (" ~" if a.name in data.nodeview_varied else "")
        name_text = Text(label)
        if check and a.name.lower() == check.lower():
            name_text.stylize("bold reverse")
        table.add_row(name_text, _status_text(a.status()), a.vote_detail())
    console.print(table)
    if data.nodeview_varied:
        console.print(
            f"[yellow]~[/yellow] [dim]{len(data.nodeview_varied)} amendment(s) had "
            "veto/vote fields that varied across backends (node-local).[/dim]"
        )

    if check:
        _print_check_focus({name: data}, check)


def _render_compare(
    targets: dict[str, str],
    fetched: dict[str, amd.NetworkAmendments],
    check: str | None,
    diff_only: bool,
) -> None:
    nets = list(fetched)
    by_net = {name: data.by_name() for name, data in fetched.items()}
    names = sorted(
        {a.name for data in fetched.values() for a in data.amendments},
        key=str.lower,
    )
    # An amendment whose veto/vote view was unstable on any sampled endpoint.
    varied = {n for data in fetched.values() for n in data.nodeview_varied}

    # Canonical delta is on `enabled` (network truth), NOT the status label
    # (which folds in node-local veto/vote and can flap between backends).
    def differs(amendment: str) -> bool:
        return len({fetched[n].enabled_of(amendment) for n in nets}) > 1

    console.print(
        Panel("[bold]AMENDMENT STATUS[/bold]  " + " vs ".join(nets), expand=False)
    )
    _print_backends(fetched)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Amendment")
    for n in nets:
        table.add_column(n, justify="center")

    # Differing amendments first, then the rest alphabetically.
    ordered = sorted(names, key=lambda n: (not differs(n), n.lower()))
    for amendment in ordered:
        if diff_only and not differs(amendment):
            continue
        label = amendment + (" ~" if amendment in varied else "")
        name_text = Text(label)
        if check and amendment.lower() == check.lower():
            name_text.stylize("bold reverse")
        elif differs(amendment):
            name_text.stylize("bold")
        cells = [_cell(by_net[n].get(amendment)) for n in nets]
        table.add_row(name_text, *cells)

    console.print(table)
    n_diff = sum(1 for a in names if differs(a))
    console.print(
        f"[bold]{n_diff}[/bold] of {len(names)} amendments differ by [bold]enabled[/bold] "
        "(network truth)."
    )
    console.print(
        "[dim]Cells: ENABLED=live · →date=majority, activates then (2wk hold) · "
        "N/M=yes-votes/validators · vetoed/pending are the queried node's view "
        "(only ENABLED is network-wide).[/dim]"
    )
    if varied:
        console.print(
            f"[yellow]~[/yellow] [dim]{len(varied)} amendment(s) had veto/vote fields "
            "that varied across backends (node-local, not a network difference).[/dim]"
        )

    if check:
        _print_check_focus(fetched, check)


def _print_backends(fetched: dict[str, amd.NetworkAmendments]) -> None:
    """Report distinct backend nodes hit per network (load-balancing) + warnings."""
    multi = any(d.samples > 1 for d in fetched.values())
    if not multi:
        return
    for name, data in fetched.items():
        nodes = ", ".join(n[:12] + "…" for n in data.nodes) or "?"
        builds = ", ".join(data.builds) or "?"
        console.print(
            f"  [bold]{name}[/bold]: {data.samples} samples · "
            f"{len(data.nodes)} backend node(s) [dim]({nodes})[/dim] · build {builds}"
        )
        if data.enabled_unstable:
            console.print(
                f"    [red]⚠ enabled varied across backends[/red] for "
                f"{', '.join(sorted(data.enabled_unstable))} "
                "[dim](an out-of-sync / amendment-blocked node)[/dim]"
            )


def _print_check_focus(fetched: dict[str, amd.NetworkAmendments], check: str) -> None:
    console.print(f"\n[bold]Focus:[/bold] {check}")
    for name, data in fetched.items():
        a = data.by_name().get(check)
        if a is None:
            # Case-insensitive fallback.
            a = next(
                (x for x in data.amendments if x.name.lower() == check.lower()), None
            )
        if a is None:
            console.print(f"  {name:<8} [dim]not present in this build[/dim]")
        else:
            live = "[green]LIVE[/green]" if a.enabled else "[yellow]not live[/yellow]"
            detail = a.vote_detail() or "supported"
            console.print(f"  {name:<8} {live}  [dim]{detail}[/dim]")
    console.print()


# --------------------------------------------------------------------------- #
#  crawl
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--network",
    type=click.Choice(sorted(NETWORKS)),
    default="mainnet",
    show_default=True,
    help="Preset seed hubs.",
)
@click.option(
    "--seeds", multiple=True, metavar="HOST[:PORT]", help="Override seed nodes."
)
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    default=None,
    help="Default peer port for hidden peers.",
)
@click.option("--concurrency", type=int, default=64, show_default=True)
@click.option("--timeout", type=float, default=10.0, show_default=True)
@click.option("--max-nodes", type=int, default=5000, show_default=True)
@click.option(
    "--no-probe-default-port",
    is_flag=True,
    help="Don't guess the default port for peers that hide it.",
)
@click.option("--json", "json_path", metavar="PATH", help="Dump raw node data to JSON.")
@click.option("--quiet", is_flag=True, help="Suppress live progress.")
def crawl(
    network: str,
    seeds: tuple[str, ...],
    port: int | None,
    concurrency: int,
    timeout: float,
    max_nodes: int,
    no_probe_default_port: bool,
    json_path: str | None,
    quiet: bool,
) -> None:
    """Crawl the peer overlay and report software-version composition."""
    net = NETWORKS[network]
    default_port = port or net.peer_port
    raw_seeds = list(seeds) if seeds else list(net.seeds)
    seed_endpoints = [parse_seed(s, default_port) for s in raw_seeds]

    crawler = Crawler(
        default_port=default_port,
        concurrency=concurrency,
        timeout=timeout,
        max_nodes=max_nodes,
        probe_default_port=not no_probe_default_port,
    )

    err.print(
        f"crawling [bold]{network}[/bold] overlay "
        f"(default port {default_port}) from {len(seed_endpoints)} seed(s)…"
    )
    try:
        if quiet:
            crawler.crawl(seed_endpoints)
        else:
            with Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                console=err,
                transient=True,
            ) as progress:
                task = progress.add_task("starting…", total=None)

                def update(s: CrawlStats) -> None:
                    progress.update(
                        task,
                        description=(
                            f"queried {s.queried}  "
                            f"[green]ok {s.reachable}[/green]  "
                            f"[red]fail {s.unreachable}[/red]  "
                            f"in-flight {s.in_flight}  "
                            f"nodes {s.nodes}"
                        ),
                    )

                crawler.crawl(seed_endpoints, on_progress=update)
    except KeyboardInterrupt:
        err.print("\n[yellow]interrupted — reporting partial results[/yellow]")

    _render_crawl(network, crawler)
    if json_path:
        _dump_crawl_json(crawler, json_path)


def _render_crawl(network: str, crawler: Crawler) -> None:
    total = len(crawler.nodes)
    with_ver = sum(1 for n in crawler.nodes.values() if n.version)
    head = (
        f"[bold]{network.upper()} NETWORK VERSION COMPOSITION[/bold]\n"
        f"endpoints queried: {len(crawler.visited)} "
        f"([green]reachable {crawler.ok}[/green], [red]unreachable {crawler.failed}[/red])\n"
        f"unique nodes: {total} · with version: {with_ver} · "
        f"contactable: {crawler.contactable}"
    )
    console.print(Panel(head, expand=False))

    if total == 0:
        console.print("[yellow]no nodes discovered[/yellow]")
        return

    counts = crawler.version_counts()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Version")
    table.add_column("Nodes", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("", ratio=1)
    for ver, count in counts.most_common():
        pct = 100.0 * count / total
        bar = Text("█" * round(pct / 2.5), style="cyan")
        style = "dim" if ver == "(unknown)" else ""
        table.add_row(Text(ver, style=style), str(count), f"{pct:5.1f}%", bar)
    console.print(table)

    rollup = crawler.release_rollup()
    if len(rollup) > 1:
        rt = Table(
            show_header=True, header_style="bold", title="By release (newest first)"
        )
        rt.add_column("Release")
        rt.add_column("Nodes", justify="right")
        rt.add_column("Share", justify="right")
        for i, (date, count) in enumerate(rollup):
            pct = 100.0 * count / total
            style = "bold green" if i == 0 else ""
            rt.add_row(Text(date, style=style), str(count), f"{pct:5.1f}%")
        console.print(rt)


def _dump_crawl_json(crawler: Crawler, path: str) -> None:
    out = {
        "summary": {
            "endpoints_queried": len(crawler.visited),
            "reachable": crawler.ok,
            "unreachable": crawler.failed,
            "unique_nodes": len(crawler.nodes),
        },
        "version_counts": dict(crawler.version_counts()),
        "nodes": [
            {
                "public_key": n.public_key,
                "version": n.version,
                "endpoints": [f"{h}:{p}" for h, p in sorted(n.endpoints)],
                "has_endpoint": n.has_endpoint,
            }
            for n in sorted(
                crawler.nodes.values(),
                key=lambda n: (n.version or "", n.public_key),
            )
        ],
    }
    Path(path).write_text(json.dumps(out, indent=2))
    err.print(f"  wrote raw node data -> {path}")


# --------------------------------------------------------------------------- #
#  zombies
# --------------------------------------------------------------------------- #


@main.command("zombies")
@click.option(
    "--network",
    type=click.Choice(AMENDMENT_NETWORKS),
    default="mainnet",
    show_default=True,
    help="Network to inspect.",
)
@click.option("--repo", type=click.Path(path_type=Path), help="xahaud checkout.")
@click.option(
    "--seeds", multiple=True, metavar="HOST[:PORT]", help="Override crawl seeds."
)
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    default=None,
    help="Default peer port for hidden peers.",
)
@click.option("--concurrency", type=click.IntRange(1), default=64, show_default=True)
@click.option(
    "--timeout", type=click.FloatRange(min=0.1), default=10.0, show_default=True
)
@click.option("--samples", type=click.IntRange(1), default=3, show_default=True)
@click.option("--max-nodes", type=click.IntRange(1), default=5000, show_default=True)
@click.option(
    "--no-probe-default-port",
    is_flag=True,
    help="Don't guess the default port for peers that hide it.",
)
@click.option(
    "--ref",
    "ref_map",
    multiple=True,
    metavar="VERSION=REF",
    help="Override version-to-git-ref mapping; repeatable.",
)
@click.option("--json", "json_path", metavar="PATH", help="Dump raw report to JSON.")
@click.option(
    "--include-nodes",
    is_flag=True,
    help="When dumping JSON, include per-peer public key/version/status rows.",
)
@click.option("--quiet", is_flag=True, help="Suppress crawl progress.")
def zombies(
    network: str,
    repo: Path | None,
    seeds: tuple[str, ...],
    port: int | None,
    concurrency: int,
    timeout: float,
    samples: int,
    max_nodes: int,
    no_probe_default_port: bool,
    ref_map: tuple[str, ...],
    json_path: str | None,
    include_nodes: bool,
    quiet: bool,
) -> None:
    """Find visible versions missing currently-enabled network amendments.

    A row marked INCOMPATIBLE means the matching source tag does not register or
    support at least one amendment that is already enabled on the network.
    UNKNOWN means the visible version could not be mapped to a local git ref.
    """
    try:
        repo_path = repo.expanduser().resolve() if repo else Path(get_xahaud_root())
    except Exception as exc:
        raise click.ClickException(
            "could not find a xahaud checkout; pass --repo /path/to/xahaud"
        ) from exc

    net = NETWORKS[network]
    default_port = port or net.peer_port
    seed_endpoints = [
        parse_seed(s, default_port) for s in (list(seeds) if seeds else list(net.seeds))
    ]
    explicit_refs = _parse_ref_map(ref_map)

    err.print(f"reading [bold]{network}[/bold] enabled amendments…")
    try:
        net_amendments = amd.fetch_sampled(
            net.rpc_url, timeout, samples=samples, want_seq=True
        )
    except requests.RequestException as exc:
        raise click.ClickException(f"amendment query failed: {exc}") from exc
    enabled = zmb.enabled_amendments(net_amendments)

    crawler = Crawler(
        default_port=default_port,
        concurrency=concurrency,
        timeout=timeout,
        max_nodes=max_nodes,
        probe_default_port=not no_probe_default_port,
    )
    err.print(
        f"crawling [bold]{network}[/bold] overlay "
        f"(default port {default_port}) from {len(seed_endpoints)} seed(s)…"
    )
    try:
        if quiet:
            crawler.crawl(seed_endpoints)
        else:
            with Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                console=err,
                transient=True,
            ) as progress:
                task = progress.add_task("starting…", total=None)

                def update(s: CrawlStats) -> None:
                    progress.update(
                        task,
                        description=(
                            f"queried {s.queried}  "
                            f"[green]ok {s.reachable}[/green]  "
                            f"[red]fail {s.unreachable}[/red]  "
                            f"in-flight {s.in_flight}  "
                            f"nodes {s.nodes}"
                        ),
                    )

                crawler.crawl(seed_endpoints, on_progress=update)
    except KeyboardInterrupt:
        err.print("\n[yellow]interrupted — reporting partial results[/yellow]")

    version_counts = zmb.visible_version_counts(
        n.version for n in crawler.nodes.values()
    )
    reports = zmb.analyze_versions(
        repo=repo_path,
        version_counts=version_counts,
        enabled=enabled,
        explicit_refs=explicit_refs,
        sampled_rpc_url=net.rpc_url,
    )
    _render_zombies(
        network=network,
        repo=repo_path,
        sampled_rpc_url=net.rpc_url,
        amendments=net_amendments,
        enabled=enabled,
        crawler=crawler,
        reports=reports,
    )
    if json_path:
        _dump_zombies_json(
            path=json_path,
            network=network,
            repo=repo_path,
            sampled_rpc_url=net.rpc_url,
            amendments=net_amendments,
            enabled=enabled,
            crawler=crawler,
            reports=reports,
            include_nodes=include_nodes,
        )


def _parse_ref_map(items: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise click.ClickException(f"--ref must be VERSION=REF, got: {item}")
        version, _, ref = item.partition("=")
        if not version.strip() or not ref.strip():
            raise click.ClickException(f"--ref must be VERSION=REF, got: {item}")
        out[zmb.visible_version_key(version.strip())] = ref.strip()
    return out


def _render_zombies(
    *,
    network: str,
    repo: Path,
    sampled_rpc_url: str,
    amendments: amd.NetworkAmendments,
    enabled: tuple[zmb.EnabledAmendment, ...],
    crawler: Crawler,
    reports: list[zmb.VersionCompatibility],
) -> None:
    total_nodes = sum(r.nodes for r in reports)
    incompatible_nodes = sum(r.nodes for r in reports if r.incompatible)
    unknown_nodes = sum(r.nodes for r in reports if r.error)
    ok_nodes = total_nodes - incompatible_nodes - unknown_nodes

    head = (
        f"[bold]{network.upper()} ZOMBIE CHECK[/bold]\n"
        f"enabled amendments: {len(enabled)} · "
        f"ledger: {amendments.ledger_seq or '?'} · repo: {repo}\n"
        f"unique crawled nodes: {len(crawler.nodes)} · "
        f"[green]ok {ok_nodes}[/green] · "
        f"[red]incompatible {incompatible_nodes}[/red] · "
        f"[yellow]unknown {unknown_nodes}[/yellow]"
    )
    console.print(Panel(head, expand=False))

    _print_backends({network: amendments})
    if amendments.enabled_unstable:
        console.print(
            "[bold yellow]classification provisional:[/bold yellow] sampled "
            "backends disagreed on enabled amendment state; inspect those "
            "backends before treating INCOMPATIBLE rows as operator evidence."
        )

    if not reports:
        console.print("[yellow]no versions discovered[/yellow]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Version")
    table.add_column("Nodes", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("Ref")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Blocking enabled amendments")
    for report in reports:
        pct = 100.0 * report.nodes / total_nodes if total_nodes else 0.0
        if report.status == "ok":
            status = Text("OK", style="green")
            detail = ""
        elif report.status == "incompatible":
            status = Text("INCOMPATIBLE", style="bold red")
            detail = _summarize_missing(report)
        else:
            status = Text("UNKNOWN", style="yellow")
            detail = report.error or ""
        table.add_row(
            report.version,
            str(report.nodes),
            f"{pct:5.1f}%",
            report.ref or "-",
            _source_link(report),
            status,
            detail,
        )
    console.print(table)
    _print_zombie_evidence(reports)
    console.print(
        "[dim]Decision rule: only currently ENABLED amendments are compatibility "
        "requirements. Vetoed/pending/majority vote fields are not used here "
        "because they are not active ledger semantics yet.[/dim]"
    )
    console.print(
        "[dim]INCOMPATIBLE means the local source tag is missing or marks "
        "unsupported an enabled amendment. Such nodes may still appear in crawls "
        "while following/acquiring validated ledgers; confirm server_state and "
        "complete_ledgers before making an operator claim.[/dim]"
    )
    console.print(
        f"[dim]Live amendment state was sampled from {sampled_rpc_url}; "
        f"{zmb.PUBLIC_DEFINITIONS_URL} is a human-readable reference.[/dim]"
    )


def _summarize_missing(report: zmb.VersionCompatibility, limit: int = 5) -> str:
    missing = list(report.missing_enabled)
    unsupported = [f"{name} (unsupported)" for name in report.unsupported_enabled]
    all_bad = missing + unsupported
    shown = ", ".join(all_bad[:limit])
    extra = len(all_bad) - limit
    if extra > 0:
        shown += f", +{extra} more"
    return shown


def _source_link(report: zmb.VersionCompatibility) -> Text | str:
    if not report.parsed:
        return "-"
    label = report.parsed.source_path.rsplit("/", 1)[-1]
    if report.commit:
        label = f"{label}@{report.commit[:8]}"
    if report.source_url:
        return Text(label, style=f"link {report.source_url}")
    return label


def _print_zombie_evidence(
    reports: list[zmb.VersionCompatibility], *, per_version: int = 3
) -> None:
    rows: list[tuple[zmb.VersionCompatibility, zmb.AmendmentEvidence]] = []
    for report in reports:
        rows.extend((report, evidence) for evidence in report.evidence[:per_version])
    if not rows:
        return

    table = Table(
        show_header=True,
        header_style="bold",
        title="Evidence",
    )
    table.add_column("Version")
    table.add_column("Amendment")
    table.add_column("Hash")
    table.add_column("Issue")
    table.add_column("Links")
    for report, evidence in rows:
        links = Text()
        if evidence.sampled_rpc_url:
            links.append("rpc", style=f"link {evidence.sampled_rpc_url}")
        else:
            links.append("rpc")
        if evidence.evidence_url:
            links.append(" · ")
            links.append(
                "file" if evidence.issue == "missing" else "line",
                style=f"link {evidence.evidence_url}",
            )
        table.add_row(
            report.version,
            evidence.name,
            evidence.amendment_id[:12],
            evidence.issue,
            links,
        )
    console.print(table)
    console.print(
        f"[dim]Evidence is capped at {per_version} amendment(s) per incompatible "
        "version; JSON output includes the full evidence list. Missing rows link "
        "the searched source file; unsupported rows link the declaration line.[/dim]"
    )


def _dump_zombies_json(
    *,
    path: str,
    network: str,
    repo: Path,
    sampled_rpc_url: str,
    amendments: amd.NetworkAmendments,
    enabled: tuple[zmb.EnabledAmendment, ...],
    crawler: Crawler,
    reports: list[zmb.VersionCompatibility],
    include_nodes: bool = False,
) -> None:
    reports_by_version = {r.version: r for r in reports}
    out = {
        "network": network,
        "repo": str(repo),
        "sampled_rpc_url": sampled_rpc_url,
        "public_definitions_url": zmb.PUBLIC_DEFINITIONS_URL,
        "ledger_seq": amendments.ledger_seq,
        "enabled_unstable": sorted(amendments.enabled_unstable),
        "enabled_amendments": [
            {"name": a.name, "amendment_id": a.amendment_id} for a in enabled
        ],
        "crawl": {
            "endpoints_queried": len(crawler.visited),
            "reachable": crawler.ok,
            "unreachable": crawler.failed,
            "unique_nodes": len(crawler.nodes),
            "version_counts": dict(
                zmb.visible_version_counts(n.version for n in crawler.nodes.values())
            ),
        },
        "versions": [r.as_dict() for r in reports],
    }
    if include_nodes:
        out["nodes"] = [
            _zombie_node_json(node, reports_by_version)
            for node in sorted(
                crawler.nodes.values(),
                key=lambda n: (n.version or "", n.public_key),
            )
        ]
    Path(path).write_text(json.dumps(out, indent=2))
    err.print(f"  wrote zombie report -> {path}")


def _zombie_node_json(
    node,
    reports_by_version: dict[str, zmb.VersionCompatibility],
) -> dict:
    version_key = zmb.visible_version_key(node.version)
    report = reports_by_version.get(version_key)
    return {
        "public_key": node.public_key,
        "version": node.version,
        "version_key": version_key,
        "status": report.status if report else "unknown",
        "ref": report.ref if report else None,
        "missing_enabled_count": len(report.missing_enabled) if report else 0,
        "unsupported_enabled_count": len(report.unsupported_enabled) if report else 0,
        "has_endpoint": node.has_endpoint,
        "endpoints": [f"{host}:{port}" for host, port in sorted(node.endpoints)],
    }


if __name__ == "__main__":
    main()
