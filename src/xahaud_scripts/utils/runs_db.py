"""SQLite database for recording build and test run timings.

Stores every x-run-tests invocation with build/test durations,
worktree, target, test suite, exit codes, and flags.

Database location: ~/.xahaud-scripts/runs.db
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

DB_DIR = Path.home() / ".xahaud-scripts"
DB_PATH = DB_DIR / "runs.db"


class Base(DeclarativeBase):
    pass


class Run(Base):
    """A single x-run-tests invocation."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    worktree: Mapped[str] = mapped_column(String, nullable=False)
    git_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    git_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    target: Mapped[str] = mapped_column(String, nullable=False, default="rippled")
    build_type: Mapped[str] = mapped_column(String, nullable=False, default="Release")
    test_suite: Mapped[str | None] = mapped_column(String, nullable=True)
    times: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Durations in seconds (None = skipped)
    build_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    test_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_duration: Mapped[float] = mapped_column(Float, nullable=False)

    # Outcomes
    build_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    test_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Flags
    coverage: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ccache: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unity: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Interrupted by Ctrl+C
    interrupted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Raw CLI args for full reproducibility
    cli_args: Mapped[str | None] = mapped_column(Text, nullable=True)


def _get_engine():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{DB_PATH}", echo=False)


def _ensure_tables():
    engine = _get_engine()
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def _session() -> Iterator[Session]:
    engine = _ensure_tables()
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _git_info(xahaud_root: str) -> tuple[str | None, str | None]:
    """Get current branch and short SHA."""
    import contextlib
    import subprocess

    branch = sha = None
    with contextlib.suppress(Exception):
        branch = (
            subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=xahaud_root,
            ).stdout.strip()
            or None
        )
    with contextlib.suppress(Exception):
        sha = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=xahaud_root,
            ).stdout.strip()
            or None
        )
    return branch, sha


class RunRecorder:
    """Context manager that records a build+test run to the database.

    Usage::

        recorder = RunRecorder(
            worktree="/path/to/xahaud",
            target="rippled",
            build_type="Release",
            test_suite="ripple.app.Import",
            times=2,
            coverage=False,
            ccache=True,
            unity=False,
            dry_run=False,
            cli_args="--ccache -- ripple.app.Import",
        )

        # Build phase
        recorder.build_started()
        success = build_rippled(...)
        recorder.build_finished(success)

        # Test phase
        recorder.test_started()
        exit_code = run_rippled(...)
        recorder.test_finished(exit_code)

        # Save
        recorder.save()
    """

    def __init__(self, **kwargs: Any) -> None:
        self._worktree = kwargs["worktree"]
        self._target = kwargs.get("target", "rippled")
        self._build_type = kwargs.get("build_type", "Release")
        self._test_suite = kwargs.get("test_suite")
        self._times = kwargs.get("times", 1)
        self._coverage = kwargs.get("coverage", False)
        self._ccache = kwargs.get("ccache", False)
        self._unity = kwargs.get("unity", False)
        self._dry_run = kwargs.get("dry_run", False)
        self._cli_args = kwargs.get("cli_args")

        self._started_at = datetime.now(UTC)
        self._start_mono = time.monotonic()

        self._build_start: float | None = None
        self._build_duration: float | None = None
        self._build_success: bool | None = None

        self._test_start: float | None = None
        self._test_duration: float | None = None
        self._test_exit_code: int | None = None
        self._interrupted = False

    def build_started(self) -> None:
        self._build_start = time.monotonic()

    def build_finished(self, success: bool) -> None:
        if self._build_start is not None:
            self._build_duration = time.monotonic() - self._build_start
        self._build_success = success

    def test_started(self) -> None:
        self._test_start = time.monotonic()

    def test_finished(self, exit_code: int) -> None:
        if self._test_start is not None:
            self._test_duration = time.monotonic() - self._test_start
        self._test_exit_code = exit_code

    def interrupted(self) -> None:
        self._interrupted = True
        # Capture partial durations
        if self._build_start is not None and self._build_duration is None:
            self._build_duration = time.monotonic() - self._build_start
            self._build_success = False
        if self._test_start is not None and self._test_duration is None:
            self._test_duration = time.monotonic() - self._test_start

    def save(self) -> None:
        total = time.monotonic() - self._start_mono
        branch, sha = _git_info(self._worktree)

        run = Run(
            started_at=self._started_at,
            worktree=self._worktree,
            git_branch=branch,
            git_sha=sha,
            target=self._target,
            build_type=self._build_type,
            test_suite=self._test_suite,
            times=self._times,
            build_duration=self._build_duration,
            test_duration=self._test_duration,
            total_duration=total,
            build_success=self._build_success,
            test_exit_code=self._test_exit_code,
            coverage=self._coverage,
            ccache=self._ccache,
            unity=self._unity,
            dry_run=self._dry_run,
            interrupted=self._interrupted,
            cli_args=self._cli_args,
        )

        try:
            with _session() as session:
                session.add(run)
        except Exception:
            # Never let db errors break the build workflow
            pass
