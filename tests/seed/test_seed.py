"""Demo seed (spec section 12): repeatable reset with the planned counts, and working logins."""

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.db import system_context, tenant_session
from seed import data
from seed.demo import demo_counts, reset_demo, seed_demo


async def test_reset_demo_is_repeatable_and_counts_match_plan(db_engine) -> None:
    first = await seed_demo()
    assert first["status"] == "seeded"
    assert first["counts"] == data.EXPECTED_COUNTS

    again = await seed_demo()  # already present: a no-op
    assert again["status"] == "exists"

    result = await reset_demo()  # wipe + reseed gives the same set
    assert result["wiped"]["organizations"] == 2
    assert await demo_counts() == data.EXPECTED_COUNTS

    async with tenant_session(system_context("test")) as session:
        flagged = (
            await session.execute(text("SELECT count(*) FROM organizations WHERE is_demo"))
        ).scalar_one()
        total = (await session.execute(text("SELECT count(*) FROM organizations"))).scalar_one()
        leftovers = (  # one set only: the wipe removed the first run's rows
            await session.execute(
                text("SELECT count(*) FROM users WHERE email = :e"), {"e": data.PLATFORM_ADMIN.email}
            )
        ).scalar_one()
    assert flagged == total == 2
    assert leftovers == 1


async def test_demo_accounts_can_login(db_engine, client, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed_demo()
    monkeypatch.setattr(settings, "rate_limit_login", "1000/minute")
    for account, role, _scope in data.DEMO_ACCOUNTS:
        response = await client.post(
            "/api/v1/auth/login", json={"email": account.email, "password": data.DEMO_PASSWORD}
        )
        assert response.status_code == 200, (account.email, response.text)
        body = response.json()
        assert body["access_token"] and body["must_change_password"] is False
        assert body["user"]["role"] == role
