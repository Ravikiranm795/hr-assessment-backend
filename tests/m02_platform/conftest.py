"""Fixtures for the M2 platform tests: cheap hashing, per-test engine, shared actors and helpers.

The application engine is cached by ``core.db._engine`` (``lru_cache``); asyncpg connections cannot
cross event loops and every test has its own loop, so the cache is reset around each test.
Feature-flag defaults live in a catalog table that module cleanup keeps, so they are restored too.
"""

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core import db as core_db
from app.core.config import settings
from tests.conftest import truncate_all
from tests.factories import auth_headers, make_user


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(settings, "argon2_time_cost", 1)
    monkeypatch.setattr(settings, "argon2_memory_kib", 64)
    monkeypatch.setattr(settings, "argon2_parallelism", 1)


@pytest_asyncio.fixture(autouse=True)
async def fresh_app_engine(db_engine):
    core_db._engine.cache_clear()
    yield
    await core_db.get_engine().dispose()
    core_db._engine.cache_clear()
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE feature_flags SET default_enabled = true"))
    # Counts assume an empty database, so drop the rows each test left behind.
    truncate_all(db_engine.url.render_as_string(hide_password=False))


@pytest.fixture
def hdr(db_session):
    """``await hdr(user)``: commit pending rows and return that user's auth headers."""

    async def _hdr(user):
        await db_session.commit()
        return await auth_headers(db_session, user.role, None, user=user)

    return _hdr


@pytest_asyncio.fixture
async def sa_h(db_session, hdr):
    return await hdr(await make_user(db_session, "super_admin"))


@pytest.fixture
def scalar(db_session):
    async def _scalar(sql, **params):
        return (await db_session.execute(text(sql), params)).scalar_one()

    return _scalar


@pytest.fixture
def audit_actions(db_session):
    async def _actions(org_id):
        rows = await db_session.execute(
            text("select action from audit_logs where org_id = :o"), {"o": org_id}
        )
        return [r[0] for r in rows]

    return _actions
