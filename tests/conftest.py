"""Shared pytest harness.

Tests never touch the dev database. The compose ``test-db`` server is shared by every
agent and run, so each pytest session works in its own database ``test_<hex>`` on that
server: ``DATABASE_URL`` is pointed at it before the app is imported, the
``migrated_db`` fixture creates it and runs ``alembic upgrade head`` on first use, and
drops it when the session ends. Tests that never ask for a database fixture never
create it. Module-owned test directories add their own fixtures in their own
``conftest.py``; this file stays generic.

RLS note: these fixtures connect as the server superuser, which bypasses row-level
security. To check RLS, open a transaction and run ``SET LOCAL ROLE app_rw`` (see
``tests/core/test_rls.py``); the policies then apply exactly as for the application role.
"""

import os
import uuid

from sqlalchemy.engine import make_url

_BASE_TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@test-db:5432/hr_assessment_test",
)
TEST_DB_NAME = f"test_{uuid.uuid4().hex}"
TEST_DATABASE_URL = make_url(_BASE_TEST_URL).set(database=TEST_DB_NAME).render_as_string(hide_password=False)
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("TOKEN_PEPPER", "test-pepper-not-a-secret")

from collections.abc import AsyncIterator, Callable, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.testing import configure_test_secrets  # noqa: E402
from app.main import create_app  # noqa: E402

# JWT and field-encryption keys for every test, installed before anything signs or encrypts.
configure_test_secrets(settings)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Catalog and bookkeeping tables survive the per-module cleanup.
_KEEP_TABLES = {"alembic_version", "feature_flags", "gadget_catalog"}


def _libpq_dsn(url: str, database: str | None = None) -> str:
    """Plain ``postgresql://`` DSN for psycopg (drops the SQLAlchemy driver suffix)."""
    parsed = make_url(url).set(drivername="postgresql")
    if database is not None:
        parsed = parsed.set(database=database)
    return parsed.render_as_string(hide_password=False)


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[str]:
    """Create this session's private database, migrate it to head, drop it at the end."""
    admin_dsn = _libpq_dsn(_BASE_TEST_URL)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    try:
        command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
        yield TEST_DATABASE_URL
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')


def truncate_all(database_url: str) -> None:
    """Remove every row except the catalogs.

    Runs with ``session_replication_role = replica`` (superuser only) so the append-only
    audit trigger and the protected-account trigger do not block the cleanup.
    """
    with psycopg.connect(_libpq_dsn(database_url), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
        ).fetchall()
        tables = sorted(name for (name,) in rows if name not in _KEEP_TABLES)
        if tables:
            conn.execute("SET session_replication_role = replica")
            conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")


@pytest.fixture(scope="module")
def clean_db_per_module(migrated_db: str) -> Iterator[str]:
    """Each test module starts from catalogs only and leaves nothing behind."""
    yield migrated_db
    truncate_all(migrated_db)


@pytest.fixture(scope="session")
def test_database_url(migrated_db: str) -> str:
    return migrated_db


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def db_engine(clean_db_per_module: str) -> AsyncIterator[AsyncEngine]:
    """Engine on this session's migrated test database (superuser, bypasses RLS)."""
    engine = create_async_engine(clean_db_per_module)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """ORM session for arranging data with ``tests.factories``. Commit explicitly."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.fixture
def make_token() -> Callable[..., str]:
    """``make_token(role, org_id, user_id, token_version=0, sid=None) -> str``."""
    from tests import factories

    return factories.make_token
