"""Programmatic Alembic upgrade (used at start-up, by the setup script and by tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection

from app.core.config import get_settings
from app.database.session import get_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "database" / "migrations"))
    cfg.attributes["url"] = url or get_settings().db_url
    return cfg


def _upgrade(sync_conn: Connection, url: str) -> None:
    cfg = alembic_config(url)
    cfg.attributes["connection"] = sync_conn
    command.upgrade(cfg, "head")


async def upgrade_database() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade, str(engine.url))
