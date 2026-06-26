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
from xahaud_scripts.inspect_net.crawl import Crawler, CrawlStats, parse_seed
from xahaud_scripts.inspect_net.networks import (
    AMENDMENT_NETWORKS,
    NETWORKS,
)

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
@click.option("--timeout", type=float, default=25.0, show_default=True)
@click.option("--json", "json_path", metavar="PATH", help="Dump raw features to JSON.")
def amendments(
    net: str | None,
    url: str | None,
    check: str | None,
    pending: bool,
    diff_only: bool,
    timeout: float,
    json_path: str | None,
) -> None:
    """Show enabled/pending/vetoed amendments; diff mainnet vs testnet."""
    if url:
        targets = {"custom": url}
    elif net:
        targets = {net: NETWORKS[net].rpc_url}
    else:
        targets = {n: NETWORKS[n].rpc_url for n in AMENDMENT_NETWORKS}

    fetched: dict[str, amd.NetworkAmendments] = {}
    for name, endpoint in targets.items():
        try:
            fetched[name] = amd.fetch(endpoint, timeout, want_seq=len(targets) == 1)
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

    rows = not_enabled if pending_only else data.amendments
    table = Table(show_header=True, header_style="bold")
    table.add_column("Amendment")
    table.add_column("Status")
    table.add_column("Detail", style="dim")
    for a in rows:
        name_text = Text(a.name)
        if check and a.name.lower() == check.lower():
            name_text.stylize("bold reverse")
        table.add_row(name_text, _status_text(a.status()), a.vote_detail())
    console.print(table)

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

    def status_of(net: str, amendment: str) -> str:
        a = by_net[net].get(amendment)
        return a.status() if a else amd.STATUS_ABSENT

    def differs(amendment: str) -> bool:
        return len({status_of(n, amendment) for n in nets}) > 1

    console.print(
        Panel(
            "[bold]AMENDMENT STATUS[/bold]  " + " vs ".join(nets),
            expand=False,
        )
    )
    table = Table(show_header=True, header_style="bold")
    table.add_column("Amendment")
    for n in nets:
        table.add_column(n, justify="center")

    # Differing amendments first, then the rest alphabetically.
    ordered = sorted(names, key=lambda n: (not differs(n), n.lower()))
    for amendment in ordered:
        if diff_only and not differs(amendment):
            continue
        name_text = Text(amendment)
        if check and amendment.lower() == check.lower():
            name_text.stylize("bold reverse")
        elif differs(amendment):
            name_text.stylize("bold")
        cells = [_status_text(status_of(n, amendment)) for n in nets]
        table.add_row(name_text, *cells)

    console.print(table)
    n_diff = sum(1 for a in names if differs(a))
    console.print(
        f"[dim]{n_diff} of {len(names)} amendments differ across networks[/dim]"
    )

    if check:
        _print_check_focus(fetched, check)


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
    "--port", type=int, default=None, help="Default peer port for hidden peers."
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


if __name__ == "__main__":
    main()
