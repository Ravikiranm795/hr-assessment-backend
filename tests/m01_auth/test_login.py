"""Login, lockout, disabled organizations, default super admin."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m01_auth.cli import create_default_superadmin
from app.modules.m01_auth.router import REFRESH_COOKIE
from tests import factories
from tests.m01_auth.conftest import BASE, PASSWORD, World, audit_count, bearer, login, scalar, user_col


async def test_login_success_sets_refresh_cookie(client: AsyncClient, world: World, db_session: AsyncSession):
    response = await login(client, world.hr.email)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_ttl_min * 60
    assert body["totp_required"] is False and body["must_change_password"] is False
    assert body["user"]["email"] == world.hr.email and body["user"]["role"] == "hr_user"
    assert "password" not in str(body).lower().replace("must_change_password", "")
    assert response.headers["cache-control"] == "no-store"

    cookie = next(h for h in response.headers.get_list("set-cookie") if h.startswith(REFRESH_COOKIE))
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/api/v1/auth" in lowered
    assert REFRESH_COOKIE == settings.refresh_cookie_name

    me = await client.get(f"{BASE}/me", headers=bearer(response))
    assert me.status_code == 200 and me.json()["user"]["id"] == str(world.hr.id)
    assert await audit_count(db_session, "AUTH_LOGIN_SUCCESS", world.hr.id) == 1
    assert await user_col(db_session, "last_login_at", world.hr.id) is not None
    assert (
        await scalar(db_session, "select count(*) from refresh_tokens where user_id = :i", i=world.hr.id) == 1
    )


async def test_wrong_password_generic_error_and_counter(
    client: AsyncClient, world: World, db_session: AsyncSession
):
    wrong = await login(client, world.hr.email, "not-the-password-0")
    unknown = await login(client, "nobody@example.com", "not-the-password-0")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert wrong.json()["error"]["message_key"] == unknown.json()["error"]["message_key"]
    assert wrong.json()["error"]["details"] == unknown.json()["error"]["details"] == {}
    assert "refresh" not in "".join(wrong.headers.get_list("set-cookie"))

    assert await user_col(db_session, "failed_login_count", world.hr.id) == 1
    assert await audit_count(db_session, "AUTH_LOGIN_FAILED", world.hr.id) == 1
    unknown_rows = await scalar(
        db_session,
        "select count(*) from audit_logs where action = 'AUTH_LOGIN_FAILED' and actor_user_id is null",
    )
    assert unknown_rows == 1  # the unknown email is audited too

    assert (await login(client, world.hr.email)).status_code == 200
    assert await user_col(db_session, "failed_login_count", world.hr.id) == 0


async def test_lockout_after_5_failures_for_15_minutes(
    client: AsyncClient, world: World, db_session: AsyncSession
):
    for _ in range(4):
        assert (await login(client, world.hr.email, "wrong-wrong-wrong")).json()["error"]["code"] == (
            "INVALID_CREDENTIALS"
        )
    fifth = await login(client, world.hr.email, "wrong-wrong-wrong")
    assert fifth.status_code == 423 and fifth.json()["error"]["code"] == "ACCOUNT_LOCKED"

    # Even the right password is refused while locked.
    locked = await login(client, world.hr.email)
    assert locked.status_code == 423 and locked.json()["error"]["code"] == "ACCOUNT_LOCKED"

    until = await user_col(db_session, "locked_until", world.hr.id)
    delta = until - datetime.now(UTC)
    assert timedelta(minutes=14) < delta <= timedelta(minutes=15)
    assert await audit_count(db_session, "AUTH_ACCOUNT_LOCKED", world.hr.id) == 1
    assert (
        await scalar(
            db_session,
            "select count(*) from notifications where type = 'account_locked' and user_id = :a",
            a=world.admin.id,
        )
        == 1
    )

    # After the lock window the account works again and the counter starts over.
    await db_session.execute(
        text("update users set locked_until = now() - interval '1 minute' where id = :i"),
        {"i": world.hr.id},
    )
    await db_session.commit()
    assert (await login(client, world.hr.email)).status_code == 200
    assert await user_col(db_session, "failed_login_count", world.hr.id) == 0
    assert await user_col(db_session, "locked_until", world.hr.id) is None


async def test_disabled_org_login_returns_ORG_DISABLED(client: AsyncClient, db_session: AsyncSession):
    org = await factories.make_org(db_session, status="disabled")
    user = await factories.make_user(db_session, "hr_user", org, password=PASSWORD)
    await db_session.commit()

    response = await login(client, user.email)

    assert response.status_code == 403 and response.json()["error"]["code"] == "ORG_DISABLED"
    assert await audit_count(db_session, "AUTH_LOGIN_FAILED", user.id) == 1


async def test_default_sa_login_audited_and_marked(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "default_sa_email", "breakglass@example.com")
    monkeypatch.setattr(settings, "default_sa_name", "Break Glass")
    monkeypatch.setattr(settings, "default_sa_password", "break-glass-password-1")

    assert await create_default_superadmin() == "created"
    assert await create_default_superadmin() == "exists"  # idempotent
    row = (
        await db_session.execute(
            text("select id, org_id, role, must_change_password from users where is_default_super_admin")
        )
    ).all()
    assert len(row) == 1 and row[0].org_id is None and row[0].role == "super_admin"
    assert row[0].must_change_password is False
    user_id = row[0].id

    response = await login(client, "breakglass@example.com", "break-glass-password-1")

    assert response.status_code == 200
    assert response.json()["user"]["is_default_super_admin"] is True
    assert await audit_count(db_session, "AUTH_DEFAULT_SA_LOGIN", user_id) == 1
    assert await audit_count(db_session, "AUTH_LOGIN_SUCCESS", user_id) == 1
    marker = await scalar(
        db_session,
        "select after->>'is_default_super_admin' from audit_logs where action = 'AUTH_DEFAULT_SA_LOGIN'",
    )
    assert marker == "true"

    # The protected row cannot change its password (DB trigger); the API says so cleanly.
    changed = await client.post(
        f"{BASE}/change-password",
        headers=bearer(response),
        json={"current_password": "break-glass-password-1", "new_password": "another-long-password-2"},
    )
    assert changed.status_code == 403 and changed.json()["error"]["code"] == "PROTECTED_ACCOUNT"
    assert (await client.post(f"{BASE}/totp/setup", headers=bearer(response))).json()["error"]["code"] == (
        "PROTECTED_ACCOUNT"
    )
