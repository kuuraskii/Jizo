"""
JIZO - Part 3 database engine and session factory.

Owner: Aayush (P3).

Async SQLAlchemy 2.0 over asyncpg. Two rules that matter more than the code:

1. **The driver suffix decides everything.** `postgresql+asyncpg://` builds
   an async engine; a plain `postgresql://` URL raises at connect time.
   `.env.example` therefore ships the `+asyncpg` form.

2. **Nothing here changes P1.** The scorer stays a pure function with no
   database access (`edgecases/README.md` invariant 1). Persistence sits
   downstream of `score_run`, never inside it.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .secrets import get_config

#: Module-level singletons, created lazily so importing this file never
#: requires a database or a populated `.env`. Tests and Alembic both rely
#: on being able to import `backend.db` with no side effects.
_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None
#: The event loop the engine was created on, so we can tell when it is stale.
_engine_loop = None


def _running_loop():
    """The current event loop, or None when called outside one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def get_engine(database_url: Optional[str] = None) -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use.

    **The engine is bound to the event loop that created it.** asyncpg
    connections cannot be reused from a different loop, so a second
    `asyncio.run()` in the same process would otherwise fail with
    "Event loop is closed" and make `/health` report a healthy database as
    unreachable. We therefore track the creating loop and rebuild the engine
    when the loop changes.

    A long-lived server (FastAPI) has one loop and pays nothing for this.
    Scripts and tests that call `asyncio.run()` repeatedly are the ones that
    would break without it.
    """
    global _engine, _sessionmaker, _engine_loop

    loop = _running_loop()
    if _engine is not None and loop is not None and _engine_loop is not loop:
        # The pool belongs to a dead loop; drop it. It cannot be disposed
        # here (that needs `await`), and the old loop is gone anyway.
        _engine = None
        _sessionmaker = None

    if _engine is None:
        url = database_url or get_config().database_url
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set. Copy .env.example to .env, or set "
                "the variable in your shell."
            )
        # Guard against the most common setup mistake: a sync URL handed to
        # an async engine. Failing here with a clear message beats a
        # confusing driver error from asyncpg later.
        if "+asyncpg" not in url:
            raise RuntimeError(
                f"DATABASE_URL must use the async driver: {url!r} is missing "
                "'+asyncpg'. Expected postgresql+asyncpg://..."
            )

        _engine = create_async_engine(
            url,
            echo=False,
            # Reclaim idle connections so a long-lived server does not
            # accumulate them across many drills.
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        _engine_loop = loop

    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the session factory bound to the engine.

    `get_engine()` is called first and unconditionally: it is the function
    that detects a stale event loop and resets this cache. Skipping the call
    (the previous behaviour, guarded by `if _sessionmaker is None`) meant the
    loop check in `get_engine` never ran on the fast path, so a second
    `asyncio.run()` kept handing out a session factory bound to a dead loop.
    """
    global _sessionmaker

    engine = get_engine()

    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-style dependency: yields a session, always closes it.

    P4 owns the routes, so this is shaped as a dependency they can declare
    with `Depends(get_session)` rather than forcing a context manager on
    them.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            # Roll back so a failed request cannot leave a half-written
            # drill run committed.
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close every pooled connection. Call on shutdown."""
    global _engine, _sessionmaker, _engine_loop

    if _engine is not None:
        await _engine.dispose()

    _engine = None
    _sessionmaker = None
    _engine_loop = None