"""Seed tests: per-test engine and rate limiter, local storage in tmp_path (snapshots are real files)."""

from collections.abc import AsyncIterator

import pytest_asyncio

from app.core import db as core_db
from app.core.ratelimit import set_rate_limiter
from app.core.storage import set_storage
from app.core.storage.local import LocalStorage


@pytest_asyncio.fixture(autouse=True)
async def _isolation(tmp_path) -> AsyncIterator[None]:
    core_db._engine.cache_clear()
    set_rate_limiter(None)
    set_storage(LocalStorage(root=str(tmp_path / "storage"), base_url="http://api.test"))
    yield
    set_storage(None)
    set_rate_limiter(None)
    await core_db.get_engine().dispose()
    core_db._engine.cache_clear()
