"""Show build and test run statistics from the runs database."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import click
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text
from sqlalchemy import case, func, select

from xahaud_scripts.utils.runs_db import DB_PATH, Run, _ensure_tables, _session


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m:.0f}m{s:.0f}s"
    h, m = divmod(m, 60)
    return f"{h:.0f}h{m:.0f}m"


def _build_display(
    *,
    since: datetime | None,
    days: int | None,
    branch: str | None,
    recent: int | None,
) -> Group | Text:
    """Query the database and build a Rich renderable."""
    with _session() as session:
        q = select(
            func.count(Run.id).label("count"),
            func.sum(Run.build_duration).label("build_total"),
            func.sum(Run.test_duration).label("test_total"),
            func.sum(Run.total_duration).label("total"),
            func.sum(
                case((Run.build_success == True, 1), else_=0)  # noqa: E712
            ).label("build_pass"),
            func.sum(case((Run.test_exit_code == 0, 1), else_=0)).label("test_pass"),
            func.sum(
                case((Run.interrupted == True, 1), else_=0)  # noqa: E712
            ).label("interrupted"),
        )
        if since:
            q = q.where(Run.started_at >= since)
        if branch:
            q = q.where(Run.git_branch == branch)

        row = session.execute(q).one()

        if row.count == 0:
            label = (
                "today"
                if since and days is None
                else f"last {days} days"
                if days
                else "all time"
            )
            return Text(f"No runs recorded ({label}).")

        # Recent runs table
        limit = recent or 15
        rq = select(Run).order_by(Run.started_at.desc()).limit(limit)
        if since:
            rq = rq.where(Run.started_at >= since)
        if branch:
            rq = rq.where(Run.git_branch == branch)

        runs = list(session.execute(rq).scalars())

        table = Table(title="Recent Runs (newest first)")
        table.add_column("Time", style="dim")
        table.add_column("Branch", style="cyan")
        table.add_column("Test Suite")
        table.add_column("Build", justify="right")
        table.add_column("Test", justify="right")
        table.add_column("Total", justify="right")
        table.add_column("Result", justify="center")

        for r in runs:
            local_time = r.started_at.replace(tzinfo=UTC).astimezone()
            ts = (
                local_time.strftime("%H:%M")
                if since and days is None
                else local_time.strftime("%m-%d %H:%M")
            )

            if r.interrupted:
                result = "[yellow]INT[/yellow]"
            elif r.test_exit_code == 0:
                result = "[green]PASS[/green]"
            elif r.test_exit_code is not None:
                result = f"[red]FAIL({r.test_exit_code})[/red]"
            elif r.build_success is False:
                result = "[red]BUILD FAIL[/red]"
            elif r.dry_run:
                result = "[dim]DRY[/dim]"
            else:
                result = "[dim]-[/dim]"

            table.add_row(
                ts,
                r.git_branch or "-",
                r.test_suite or "-",
                _fmt_duration(r.build_duration),
                _fmt_duration(r.test_duration),
                _fmt_duration(r.total_duration),
                result,
            )

        # Summary
        label = (
            "Today"
            if since and days is None
            else f"Last {days} days"
            if days
            else "All time"
        )
        if branch:
            label += f" (branch: {branch})"

        summary_parts = [
            f"[bold]{label}[/bold]: {row.count} runs",
            f"  Build: {_fmt_duration(row.build_total)} "
            f"({row.build_pass or 0}/{row.count} passed)",
            f"  Test:  {_fmt_duration(row.test_total)} "
            f"({row.test_pass or 0}/{row.count} passed)",
            f"  Total: {_fmt_duration(row.total)}",
        ]
        if row.interrupted:
            summary_parts.append(f"  Interrupted: {row.interrupted}")

        summary = Text.from_markup("\n".join(summary_parts))

        return Group(table, Text(""), summary)


@click.command()
@click.option(
    "--days",
    "-d",
    type=int,
    default=None,
    help="Show stats for the last N days (default: today).",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show all recorded runs.",
)
@click.option(
    "--recent",
    "-n",
    type=int,
    default=None,
    help="Show the N most recent runs.",
)
@click.option(
    "--branch",
    "-b",
    type=str,
    default=None,
    help="Filter by git branch.",
)
@click.option(
    "--watch",
    "-w",
    type=float,
    default=None,
    help="Refresh every N seconds (e.g. -w 5).",
)
def main(
    days: int | None,
    show_all: bool,
    recent: int | None,
    branch: str | None,
    watch: float | None,
) -> None:
    """Show build and test run statistics.

    Examples:
        x-run-stats              # today's stats
        x-run-stats -d 7         # last 7 days
        x-run-stats -n 10        # 10 most recent runs
        x-run-stats --all        # everything
        x-run-stats -b dev       # filter by branch
        x-run-stats -w 5         # live refresh every 5s
    """
    if not DB_PATH.exists():
        click.echo("No runs recorded yet.")
        return

    _ensure_tables()
    console = Console(stderr=True)

    # Determine time filter
    if show_all:
        since = None
    elif days is not None:
        since = datetime.now(UTC) - timedelta(days=days)
    else:
        since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    def render() -> Group | Text:
        return _build_display(since=since, days=days, branch=branch, recent=recent)

    if watch is None:
        console.print()
        console.print(render())
        console.print()
    else:
        with Live(
            render(),
            console=console,
            refresh_per_second=1,
            screen=True,
        ) as live:
            try:
                while True:
                    time.sleep(watch)
                    live.update(render())
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
