"""Show build and test run statistics from the runs database."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import click
import sqlalchemy as sa
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from xahaud_scripts.utils.runs_db import (
    DB_PATH,
    Run,
    RunEvent,
    _ensure_tables,
    _session,
)


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


def _event(events: list[RunEvent], phase: str, event: str) -> RunEvent | None:
    """Find a specific event in a run's event list."""
    for e in events:
        if e.phase == phase and e.event == event:
            return e
    return None


def _build_display(
    *,
    since: datetime | None,
    days: int | None,
    branch: str | None,
    recent: int | None,
) -> Group | Text:
    """Query the database and build a Rich renderable."""
    from sqlalchemy import func

    with _session() as session:
        # Count all matching runs (for summary)
        count_q = select(func.count(Run.id))
        if since:
            count_q = count_q.where(Run.started_at >= since)
        if branch:
            count_q = count_q.where(Run.git_branch == branch)
        total_matching = session.execute(count_q).scalar() or 0

        # Fetch recent runs with events eagerly loaded
        limit = recent or 15
        rq = (
            select(Run)
            .options(selectinload(Run.events))
            .order_by(Run.started_at.desc())
            .limit(limit)
        )
        if since:
            rq = rq.where(Run.started_at >= since)
        if branch:
            rq = rq.where(Run.git_branch == branch)

        runs = list(session.execute(rq).scalars())

        if not runs:
            label = (
                "today"
                if since and days is None
                else f"last {days} days"
                if days
                else "all time"
            )
            return Text(f"No runs recorded ({label}).")

        # Build table
        table = Table(title="Recent Runs (newest first)")
        table.add_column("Time", style="dim")
        table.add_column("Branch", style="cyan")
        table.add_column("Test Suite")
        table.add_column("Build", justify="right")
        table.add_column("Test", justify="right")
        table.add_column("Total", justify="right")
        table.add_column("Result", justify="center")

        # Aggregates
        total_runs = 0
        total_build = 0.0
        total_test = 0.0
        total_time = 0.0
        build_pass = 0
        test_pass = 0
        interrupted_count = 0

        for r in runs:
            total_runs += 1
            evts = r.events

            build_ev = _event(evts, "build", "finished")
            test_ev = _event(evts, "test", "finished")
            run_fin = _event(evts, "run", "finished")
            run_int = _event(evts, "run", "interrupted")

            build_dur = build_ev.duration if build_ev else None
            test_dur = test_ev.duration if test_ev else None
            total_dur = (
                run_fin.duration if run_fin else run_int.duration if run_int else None
            )

            if build_dur is not None:
                total_build += build_dur
            if test_dur is not None:
                total_test += test_dur
            if total_dur is not None:
                total_time += total_dur

            if build_ev and build_ev.success:
                build_pass += 1
            if test_ev and test_ev.exit_code == 0:
                test_pass += 1
            if run_int:
                interrupted_count += 1

            # Determine result display
            if run_int:
                result = "[yellow]INT[/yellow]"
            elif test_ev and test_ev.exit_code == 0:
                result = "[green]PASS[/green]"
            elif test_ev and test_ev.exit_code is not None:
                result = f"[red]FAIL({test_ev.exit_code})[/red]"
            elif build_ev and build_ev.success is False:
                result = "[red]BUILD FAIL[/red]"
            elif r.dry_run:
                result = "[dim]DRY[/dim]"
            elif _event(evts, "run", "started") and not run_fin and not run_int:
                result = "[blue]RUNNING[/blue]"
            else:
                result = "[dim]-[/dim]"

            local_time = r.started_at.replace(tzinfo=UTC).astimezone()
            ts = (
                local_time.strftime("%H:%M")
                if since and days is None
                else local_time.strftime("%m-%d %H:%M")
            )

            table.add_row(
                ts,
                r.git_branch or "-",
                r.test_suite or "-",
                _fmt_duration(build_dur),
                _fmt_duration(test_dur),
                _fmt_duration(total_dur),
                result,
            )

        # Aggregate summary over ALL matching runs (not just displayed)
        from sqlalchemy import and_, case

        base_filter = []
        if since:
            base_filter.append(Run.started_at >= since)
        if branch:
            base_filter.append(Run.git_branch == branch)

        evt_q = (
            select(
                RunEvent.phase,
                RunEvent.event,
                func.sum(RunEvent.duration).label("total_dur"),
                func.count().label("cnt"),
                func.sum(
                    case((RunEvent.success == True, 1), else_=0)  # noqa: E712
                ).label("success_cnt"),
                func.sum(case((RunEvent.exit_code == 0, 1), else_=0)).label(
                    "exit0_cnt"
                ),
            )
            .join(Run)
            .where(and_(*base_filter) if base_filter else sa.true())
            .group_by(RunEvent.phase, RunEvent.event)
        )
        agg = {(r.phase, r.event): r for r in session.execute(evt_q).all()}

        def _agg(phase: str, event: str) -> Any:
            return agg.get((phase, event))

        build_fin = _agg("build", "finished")
        test_fin = _agg("test", "finished")
        run_fin = _agg("run", "finished")
        run_int = _agg("run", "interrupted")

        agg_build_time = build_fin.total_dur if build_fin else 0
        agg_test_time = test_fin.total_dur if test_fin else 0
        agg_total_time = (run_fin.total_dur if run_fin else 0) + (
            run_int.total_dur if run_int else 0
        )
        agg_build_pass = build_fin.success_cnt if build_fin else 0
        agg_test_pass = test_fin.exit0_cnt if test_fin else 0
        agg_interrupted = run_int.cnt if run_int else 0

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

        showing = f" (showing {total_runs})" if total_matching > total_runs else ""
        summary_parts = [
            f"[bold]{label}[/bold]: {total_matching} runs{showing}",
            f"  Build: {_fmt_duration(agg_build_time)} "
            f"({agg_build_pass}/{total_matching} passed)",
            f"  Test:  {_fmt_duration(agg_test_time)} "
            f"({agg_test_pass}/{total_matching} passed)",
            f"  Total: {_fmt_duration(agg_total_time)}",
        ]
        if agg_interrupted:
            summary_parts.append(f"  Interrupted: {agg_interrupted}")

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
