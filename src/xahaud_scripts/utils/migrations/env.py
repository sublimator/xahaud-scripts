"""Alembic environment for runs.db migrations."""

from alembic import context

from xahaud_scripts.utils.runs_db import Base, _get_engine

target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = _get_engine()
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
