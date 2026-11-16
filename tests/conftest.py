"""Shared pytest harness.

Tests never touch the dev database: ``DATABASE_URL`` is pointed at the ``test-db``
compose service before the app is imported. Module-owned test directories add their
own fixtures in their own ``conftest.py``; this file stays generic.
"""

import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@test-db:5432/hr_assessment_test",
)
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("TOKEN_PEPPER", "test-pepper-not-a-secret")

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return TEST_DATABASE_URL


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def db_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Raw engine on the empty test database (no schema yet)."""
    engine = create_async_engine(test_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


# TODO(Task 3): plug in here, without changing the fixtures above.
#   * ``migrated_db`` (session scope): run ``alembic upgrade head`` against
#     ``test_database_url`` once, and make ``db_engine`` depend on it.
#   * ``make_token``: factory returning a signed access token for a given
#     user/org/role, backed by ``app.core.security.create_access_token``.
