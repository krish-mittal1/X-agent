"""Async database engine/session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle_seconds,
    )


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(settings)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialised; call init_engine() first")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency; commits on success, rolls back on failure."""
    async with session_scope() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def database_reachable() -> tuple[bool, str]:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # pragma: no cover - depends on infrastructure
        return False, f"{type(exc).__name__}: {exc}"


async def vector_schema_status(expected_dimensions: int) -> tuple[bool, str]:
    """Verify pgvector is installed and the embedding column has the expected width."""
    try:
        async with get_engine().connect() as conn:
            ext = await conn.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'"))
            if not ext:
                return False, "pgvector extension is not installed"
            dims = await conn.scalar(
                text(
                    """
                    SELECT atttypmod FROM pg_attribute
                    WHERE attrelid = 'memory_embeddings'::regclass AND attname = 'embedding'
                    """
                )
            )
            # pgvector encodes the declared dimension directly in atttypmod.
            if dims != expected_dimensions:
                return (
                    False,
                    f"memory_embeddings.embedding has dimension {dims}, "
                    f"config expects {expected_dimensions}; migrate and re-embed",
                )
            return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
