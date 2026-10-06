"""Async database engine setup."""

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.models import Base

log = logging.getLogger(__name__)

# Arbitrary constant; serialises schema creation across gateway replicas.
SCHEMA_LOCK_ID = 7_340_001


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_schema(engine: AsyncEngine, attempts: int = 30, delay: float = 2.0) -> None:
    """Create tables, waiting for the database to come up first.

    Several replicas start at once, so on Postgres the CREATE statements run
    under an advisory lock; concurrent CREATE TABLE IF NOT EXISTS can still
    collide on the system catalogs.
    """
    for attempt in range(1, attempts + 1):
        try:
            async with engine.begin() as conn:
                if conn.dialect.name == "postgresql":
                    await conn.execute(text(f"SELECT pg_advisory_xact_lock({SCHEMA_LOCK_ID})"))
                await conn.run_sync(Base.metadata.create_all)
            return
        except (OSError, DBAPIError) as exc:  # refused, DNS not ready, still starting up, ...
            if attempt == attempts:
                raise
            log.warning("database not reachable (%s), retrying in %.0fs", exc, delay)
            await asyncio.sleep(delay)
