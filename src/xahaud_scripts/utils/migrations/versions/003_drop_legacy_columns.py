"""drop legacy mutable columns from runs

Revision ID: 003
Revises: 002
Create Date: 2026-03-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_COLUMNS = [
    "build_duration",
    "test_duration",
    "total_duration",
    "build_success",
    "test_exit_code",
    "interrupted",
]


def upgrade() -> None:
    # SQLite doesn't support DROP COLUMN before 3.35.0, so recreate the table
    bind = op.get_bind()

    # Check SQLite version
    sqlite_version: str = bind.execute(sa.text("SELECT sqlite_version()")).scalar()  # type: ignore[assignment]
    major, minor, _ = (int(x) for x in sqlite_version.split("."))

    if major > 3 or (major == 3 and minor >= 35):
        for col in _LEGACY_COLUMNS:
            op.drop_column("runs", col)
    else:
        # Fallback: recreate table without legacy columns
        op.create_table(
            "runs_new",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("started_at", sa.DateTime, nullable=False),
            sa.Column("worktree", sa.String, nullable=False),
            sa.Column("git_branch", sa.String, nullable=True),
            sa.Column("git_sha", sa.String, nullable=True),
            sa.Column("target", sa.String, nullable=False, server_default="rippled"),
            sa.Column(
                "build_type", sa.String, nullable=False, server_default="Release"
            ),
            sa.Column("test_suite", sa.String, nullable=True),
            sa.Column("times", sa.Integer, nullable=False, server_default="1"),
            sa.Column(
                "coverage", sa.Boolean, nullable=False, server_default=sa.false()
            ),
            sa.Column("ccache", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("unity", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("dry_run", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("cli_args", sa.Text, nullable=True),
        )
        kept = (
            "id, started_at, worktree, git_branch, git_sha, target, build_type, "
            "test_suite, times, coverage, ccache, unity, dry_run, cli_args"
        )
        bind.execute(sa.text(f"INSERT INTO runs_new ({kept}) SELECT {kept} FROM runs"))
        op.drop_table("runs")
        op.rename_table("runs_new", "runs")


def downgrade() -> None:
    # Re-add legacy columns (data is lost)
    op.add_column("runs", sa.Column("build_duration", sa.Float, nullable=True))
    op.add_column("runs", sa.Column("test_duration", sa.Float, nullable=True))
    op.add_column(
        "runs",
        sa.Column("total_duration", sa.Float, nullable=False, server_default="0"),
    )
    op.add_column("runs", sa.Column("build_success", sa.Boolean, nullable=True))
    op.add_column("runs", sa.Column("test_exit_code", sa.Integer, nullable=True))
    op.add_column(
        "runs",
        sa.Column("interrupted", sa.Boolean, nullable=False, server_default=sa.false()),
    )
