"""Shared test fixtures.

Integration tests run against a real PostgreSQL+pgvector database. The database
is created, reset and migrated once per test session; each test that requests the
`engine` fixture starts from truncated tables. If no database is reachable the
tests are skipped with an explicit reason rather than silently passing.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import Settings
from app.memory import database as db_module
from app.memory.models import Account, Base

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://techx:techx@localhost:55432/techx_test",
)
ADMIN_TOKEN = "test-admin-token"
_APPROVED_TEST_DATABASES = frozenset({"techx_test", "x_agent_test"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _validated_test_database_url(url: str) -> str:
    """Fail closed before destructive test setup touches PostgreSQL.

    Tests recreate the public schema and truncate every mapped table. They may
    only do that to explicitly named disposable databases on loopback; this
    protects developer and production databases even if TEST_DATABASE_URL is
    accidentally inherited from another environment.
    """
    parsed = urlparse(url)
    database = unquote(parsed.path.lstrip("/"))
    if parsed.scheme != "postgresql+asyncpg":
        raise RuntimeError("TEST_DATABASE_URL must use postgresql+asyncpg")
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise RuntimeError("TEST_DATABASE_URL must target a loopback host")
    if database not in _APPROVED_TEST_DATABASES:
        raise RuntimeError(
            "TEST_DATABASE_URL must target an approved disposable database "
            f"({', '.join(sorted(_APPROVED_TEST_DATABASES))})"
        )
    return url


def _connection_parts(url: str) -> dict[str, object]:
    parsed = urlparse(url.replace("+asyncpg", ""))
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "user": parsed.username or "postgres",
        "password": parsed.password or "",
        "database": unquote((parsed.path or "/postgres").lstrip("/")) or "postgres",
    }


def _prepare_database() -> None:
    test_url = _validated_test_database_url(TEST_DATABASE_URL)
    parts = _connection_parts(test_url)
    dbname = str(parts.pop("database"))

    async def _recreate() -> None:
        admin = await asyncpg.connect(**parts, database="postgres")  # type: ignore[arg-type]
        try:
            exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", dbname)
            if not exists:
                await admin.execute(f'CREATE DATABASE "{dbname}"')
        finally:
            await admin.close()
        conn = await asyncpg.connect(**parts, database=dbname)  # type: ignore[arg-type]
        try:
            await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            await conn.execute("CREATE SCHEMA public")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        finally:
            await conn.close()

    try:
        asyncio.run(_recreate())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL+pgvector unavailable for integration tests ({exc})")

    env = os.environ.copy()
    env["DATABASE_URL"] = test_url
    env["APP_ENV"] = "test"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture(scope="session")
def test_database_url() -> str:
    _prepare_database()
    return _validated_test_database_url(TEST_DATABASE_URL)


@pytest.fixture
def settings(test_database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=test_database_url,
        embedding_provider="hashing",
        embedding_dimensions=384,
        llm_provider="disabled",
        admin_api_token=SecretStr(ADMIN_TOKEN),
        log_json=False,
        log_level="WARNING",
        x_publishing_mode="local_draft",
        dry_run=True,
        publishing_enabled=False,
        freshness_half_life_hours=24.0,
        per_host_min_interval_seconds=0.0,
        http_timeout_seconds=5.0,
    )


async def _truncate(engine: AsyncEngine) -> None:
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = db_module.init_engine(settings)
    await _truncate(eng)
    try:
        yield eng
    finally:
        await db_module.dispose_engine()


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with db_module.session_scope() as session:
        yield session


@pytest.fixture
async def account_factory(engine: AsyncEngine):
    async def _create(handle: str = "techbrief", x_user_id: str | None = "1001") -> Account:
        async with db_module.session_scope() as session:
            account = Account(handle=handle, x_user_id=x_user_id, display_name=handle)
            session.add(account)
            await session.flush()
            await session.commit()
            return account

    return _create


@pytest.fixture
async def account(account_factory) -> Account:  # type: ignore[no-untyped-def]
    return await account_factory()


@pytest.fixture
async def client(settings: Settings, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app(settings, start_scheduler=False)
    app.state.settings = settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}
