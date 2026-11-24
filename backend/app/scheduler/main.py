"""Scheduler process entry point: ``python -m app.scheduler.main``."""

from __future__ import annotations

import asyncio
import signal
import sys

from app.ai.hub import get_hub
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.migrate import upgrade_database
from app.database.session import dispose_engine, sessionmaker
from app.scheduler.worker import Scheduler, SchedulerLockedError


async def main() -> int:
    configure_logging()
    settings = get_settings()
    settings.ensure_dirs()
    await upgrade_database()
    async with sessionmaker()() as db:
        await get_hub().load(db)
    scheduler = Scheduler()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, scheduler.stop)
    try:
        await scheduler.run_forever()
    except SchedulerLockedError as exc:
        print(f"scheduler not started: {exc}", file=sys.stderr)
        return 2
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
