"""add run_events table and migrate existing data

Revision ID: 002
Revises: 001
Create Date: 2026-03-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("phase", sa.String, nullable=False),  # run, build, test
        sa.Column("event", sa.String, nullable=False),  # started, finished, interrupted
        sa.Column("duration", sa.Float, nullable=True),
        sa.Column("success", sa.Boolean, nullable=True),
        sa.Column("exit_code", sa.Integer, nullable=True),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])

    # Migrate existing runs into events
    bind = op.get_bind()
    runs = bind.execute(sa.text("SELECT * FROM runs")).mappings().all()

    for r in runs:
        run_id = r["id"]
        started_at = r["started_at"]

        # run.started
        bind.execute(
            sa.text(
                "INSERT INTO run_events (run_id, timestamp, phase, event) "
                "VALUES (:run_id, :ts, 'run', 'started')"
            ),
            {"run_id": run_id, "ts": started_at},
        )

        # build events
        if r["build_duration"] is not None:
            bind.execute(
                sa.text(
                    "INSERT INTO run_events (run_id, timestamp, phase, event) "
                    "VALUES (:run_id, :ts, 'build', 'started')"
                ),
                {"run_id": run_id, "ts": started_at},
            )
            bind.execute(
                sa.text(
                    "INSERT INTO run_events "
                    "(run_id, timestamp, phase, event, duration, success) "
                    "VALUES (:run_id, :ts, 'build', 'finished', :dur, :ok)"
                ),
                {
                    "run_id": run_id,
                    "ts": started_at,
                    "dur": r["build_duration"],
                    "ok": r["build_success"],
                },
            )

        # test events
        if r["test_duration"] is not None:
            bind.execute(
                sa.text(
                    "INSERT INTO run_events (run_id, timestamp, phase, event) "
                    "VALUES (:run_id, :ts, 'test', 'started')"
                ),
                {"run_id": run_id, "ts": started_at},
            )
            bind.execute(
                sa.text(
                    "INSERT INTO run_events "
                    "(run_id, timestamp, phase, event, duration, exit_code) "
                    "VALUES (:run_id, :ts, 'test', 'finished', :dur, :ec)"
                ),
                {
                    "run_id": run_id,
                    "ts": started_at,
                    "dur": r["test_duration"],
                    "ec": r["test_exit_code"],
                },
            )

        # run.finished or run.interrupted
        if r["interrupted"]:
            bind.execute(
                sa.text(
                    "INSERT INTO run_events "
                    "(run_id, timestamp, phase, event, duration) "
                    "VALUES (:run_id, :ts, 'run', 'interrupted', :dur)"
                ),
                {"run_id": run_id, "ts": started_at, "dur": r["total_duration"]},
            )
        else:
            bind.execute(
                sa.text(
                    "INSERT INTO run_events "
                    "(run_id, timestamp, phase, event, duration) "
                    "VALUES (:run_id, :ts, 'run', 'finished', :dur)"
                ),
                {"run_id": run_id, "ts": started_at, "dur": r["total_duration"]},
            )


def downgrade() -> None:
    op.drop_index("ix_run_events_run_id")
    op.drop_table("run_events")
