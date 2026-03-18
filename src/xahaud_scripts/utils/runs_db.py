"""SQLite database for recording build and test run timings.

Stores every x-run-tests invocation as a run with append-only events
for each phase (build started/finished, test started/finished, etc).

Database location: ~/.xahaud-scripts/runs.db
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command as alembic_cmd
from alembic.config import Config as AlembicConfig
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
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
    relationship,
    sessionmaker,
)

DB_DIR = Path.home() / ".xahaud-scripts"
DB_PATH = DB_DIR / "runs.db"

_ALEMBIC_INI = Path(__file__).parent / "alembic.ini"


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

    # Flags
    coverage: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ccache: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unity: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Raw CLI args for full reproducibility
    cli_args: Mapped[str | None] = mapped_column(Text, nullable=True)

    events: Mapped[list[RunEvent]] = relationship(back_populates="run")


class RunEvent(Base):
    """An immutable event in a run's lifecycle."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)  # run, build, test
    event: Mapped[str] = mapped_column(
        String, nullable=False
    )  # started, finished, interrupted
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    run: Mapped[Run] = relationship(back_populates="events")


def _get_engine():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{DB_PATH}", echo=False)


def _ensure_tables():
    """Run alembic migrations to ensure schema is up to date."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    cfg = AlembicConfig(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{DB_PATH}")
    alembic_cmd.upgrade(cfg, "head")
    return _get_engine()


@contextmanager
def _session() -> Iterator[Session]:
    engine = _get_engine()
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
    """Records a build+test run to the database with immutable events.

    Events are appended as each phase starts/finishes, so even if the
    process is killed, partial data is preserved.

    Usage::

        recorder = RunRecorder(worktree="/path/to/xahaud", ...)

        recorder.build_started()
        success = build_rippled(...)
        recorder.build_finished(success)

        recorder.test_started()
        exit_code = run_rippled(...)
        recorder.test_finished(exit_code)

        recorder.finish()
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

        self._run_id: int | None = None

        # Insert the run row and started event immediately
        try:
            branch, sha = _git_info(self._worktree)
            with _session() as session:
                run = Run(
                    started_at=self._started_at,
                    worktree=self._worktree,
                    git_branch=branch,
                    git_sha=sha,
                    target=self._target,
                    build_type=self._build_type,
                    test_suite=self._test_suite,
                    times=self._times,
                    coverage=self._coverage,
                    ccache=self._ccache,
                    unity=self._unity,
                    dry_run=self._dry_run,
                    cli_args=self._cli_args,
                )
                session.add(run)
                session.flush()
                self._run_id = run.id
                session.add(
                    RunEvent(
                        run_id=run.id,
                        timestamp=self._started_at,
                        phase="run",
                        event="started",
                    )
                )
        except Exception:
            pass

    def _emit(self, phase: str, event: str, **extra: Any) -> None:
        """Append an immutable event."""
        if self._run_id is None:
            return
        try:
            with _session() as session:
                session.add(
                    RunEvent(
                        run_id=self._run_id,
                        timestamp=datetime.now(UTC),
                        phase=phase,
                        event=event,
                        **extra,
                    )
                )
        except Exception:
            pass

    def build_started(self) -> None:
        self._build_start = time.monotonic()
        self._emit("build", "started")

    def build_finished(self, success: bool) -> None:
        if self._build_start is not None:
            self._build_duration = time.monotonic() - self._build_start
        self._build_success = success
        self._emit("build", "finished", duration=self._build_duration, success=success)

    def test_started(self) -> None:
        self._test_start = time.monotonic()
        self._emit("test", "started")

    def test_finished(self, exit_code: int) -> None:
        if self._test_start is not None:
            self._test_duration = time.monotonic() - self._test_start
        self._test_exit_code = exit_code
        self._emit(
            "test", "finished", duration=self._test_duration, exit_code=exit_code
        )

    def interrupted(self) -> None:
        self._interrupted = True
        if self._build_start is not None and self._build_duration is None:
            self._build_duration = time.monotonic() - self._build_start
            self._build_success = False
        if self._test_start is not None and self._test_duration is None:
            self._test_duration = time.monotonic() - self._test_start
        self._emit(
            "run",
            "interrupted",
            duration=time.monotonic() - self._start_mono,
        )

    def save(self) -> None:
        """Emit the final run.finished event."""
        if self._interrupted:
            return  # interrupted() already emitted
        total = time.monotonic() - self._start_mono
        self._emit("run", "finished", duration=total)
