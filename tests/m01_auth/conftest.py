"""Fixtures for the M1 auth tests."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import db as core_db
from app.core.config import settings
from app.core.ratelimit import set_rate_limiter
from app.db import Organization, User
from app.modules.m01_auth.google import set_google_verifier
from app.modules.m01_auth.router import REFRESH_COOKIE
from tests import factories

PASSWORD = "correct-horse-battery-1"
BASE = "/api/v1/auth"


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argon2 at test cost: the suite hashes dozens of passwords."""
    monkeypatch.setattr(settings, "argon2_time_cost", 1)
    monkeypatch.setattr(settings, "argon2_memory_kib", 8192)
    monkeypatch.setattr(settings, "argon2_parallelism", 1)


@pytest_asyncio.fixture(autouse=True)
async def isolated_process_state() -> AsyncIterator[None]:
    """Rate limiter, Google verifier and the engine cache (an async engine is bound to one event loop)."""
    set_rate_limiter(None)
    set_google_verifier(None)
    core_db._engine.cache_clear()
    yield
    await core_db.get_engine().dispose()
    core_db._engine.cache_clear()
    set_rate_limiter(None)
    set_google_verifier(None)


@dataclass
class World:
    org: Organization
    admin: User
    hr: User
    sa: User


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    org = await factories.make_org(db_session)
    admin = await factories.make_user(db_session, "org_admin", org, password=PASSWORD)
    hr = await factories.make_user(db_session, "hr_user", org, password=PASSWORD)
    sa = await factories.make_user(db_session, "super_admin", password=PASSWORD)
    await db_session.commit()
    return World(org=org, admin=admin, hr=hr, sa=sa)


async def login(client: AsyncClient, email: str, password: str = PASSWORD) -> Response:
    return await client.post(f"{BASE}/login", json={"email": email, "password": password})


def bearer(response: Response) -> dict[str, str]:
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def refresh_cookie(response: Response) -> str:
    """Raw refresh token from a response's Set-Cookie header."""
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{REFRESH_COOKIE}="):
            return header.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError("no refresh cookie in response")


async def refresh_with(client: AsyncClient, raw: str | None) -> Response:
    """POST /auth/refresh presenting exactly this cookie value (client jar cleared first)."""
    client.cookies.clear()
    headers = {"Cookie": f"{REFRESH_COOKIE}={raw}"} if raw else {}
    return await client.post(f"{BASE}/refresh", headers=headers)


async def scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar_one_or_none()


async def audit_count(session: AsyncSession, action: str, user_id: Any = None, **filters: Any) -> int:
    sql = "select count(*) from audit_logs where action = :action"
    params: dict[str, Any] = {"action": action}
    if user_id is not None:
        sql += " and actor_user_id = :uid"
        params["uid"] = user_id
    for key, value in filters.items():
        sql += f" and {key} = :{key}"
        params[key] = value
    return await scalar(session, sql, **params)


async def user_col(session: AsyncSession, column: str, user_id: Any) -> Any:
    """One column of one users row, read straight from the database."""
    return await scalar(session, f"select {column} from users where id = :i", i=user_id)
