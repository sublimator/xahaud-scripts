"""baseline: existing runs table

Revision ID: 001
Revises: None
Create Date: 2026-03-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only create if it doesn't exist (existing databases already have it)
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "runs" not in inspector.get_table_names():
        op.create_table(
            "runs",
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
            sa.Column("build_duration", sa.Float, nullable=True),
            sa.Column("test_duration", sa.Float, nullable=True),
            sa.Column("total_duration", sa.Float, nullable=False),
            sa.Column("build_success", sa.Boolean, nullable=True),
            sa.Column("test_exit_code", sa.Integer, nullable=True),
            sa.Column(
                "coverage", sa.Boolean, nullable=False, server_default=sa.false()
            ),
            sa.Column("ccache", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("unity", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("dry_run", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column(
                "interrupted", sa.Boolean, nullable=False, server_default=sa.false()
            ),
            sa.Column("cli_args", sa.Text, nullable=True),
        )


def downgrade() -> None:
    op.drop_table("runs")
