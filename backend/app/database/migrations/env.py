"""Alembic environment (async SQLite, batch mode for ALTER TABLE support)."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from app.core.config import get_settings
from app.database import models  # noqa: F401  (register models)
from app.database.base import Base
from app.database.session import make_engine

config = context.config
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # FTS5 virtual table and its shadow tables are managed by raw SQL in 0001
    return not (type_ == "table" and name and name.startswith("knowledge_fts"))


def _url() -> str:
    return config.attributes.get("url") or get_settings().db_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True,
                      include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True,
                      include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    engine = make_engine(_url())
    async with engine.begin() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


_conn = config.attributes.get("connection")
if context.is_offline_mode():
    run_migrations_offline()
elif _conn is not None:  # programmatic upgrade from a running event loop (sync connection)
    _do_run(_conn)
else:
    asyncio.run(run_async())
