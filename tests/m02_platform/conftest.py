"""Fixtures for the M2 platform tests: cheap password hashing and a per-test engine.

The application engine is cached by ``core.db._engine`` (``lru_cache``); asyncpg connections
cannot cross event loops and every test has its own loop, so the cache is reset around each test.
"""

import pytest
import pytest_asyncio

from app.core import db as core_db
from app.core.config import settings


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
