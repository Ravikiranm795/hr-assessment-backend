"""Refresh rotation, forced password change, password history, TOTP, Google, permissions."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.modules.m01_auth import totp
from app.modules.m01_auth.google import GoogleIdentity, set_google_verifier
from tests import factories
from tests.m01_auth.conftest import (
    BASE,
    PASSWORD,
    World,
    audit_count,
    bearer,
    login,
    refresh_cookie,
    refresh_with,
    user_col,
)


async def test_refresh_rotation_and_reuse_revokes_family(client: AsyncClient, world: World):
    first = await login(client, world.hr.email)
    cookie_a = refresh_cookie(first)

    second = await refresh_with(client, cookie_a)
    assert second.status_code == 200 and second.json()["access_token"]
    cookie_b = refresh_cookie(second)
    assert cookie_b != cookie_a

    reused = await refresh_with(client, cookie_a)  # an already-rotated token comes back
    assert reused.status_code == 401 and reused.json()["error"]["code"] == "UNAUTHENTICATED"
    assert "max-age=0" in "".join(reused.headers.get_list("set-cookie")).lower()

    # The whole family is dead, including the newest token.
    assert (await refresh_with(client, cookie_b)).status_code == 401
    assert (await refresh_with(client, None)).status_code == 401


async def test_must_change_password_blocks_other_endpoints(client: AsyncClient, db_session: AsyncSession):
    org = await factories.make_org(db_session)
    user = await factories.make_user(
        db_session, "org_admin", org, password=PASSWORD, must_change_password=True
    )
    await db_session.commit()

    response = await login(client, user.email)
    assert response.json()["must_change_password"] is True
    headers = bearer(response)

    assert (await client.get(f"{BASE}/me", headers=headers)).status_code == 200
    blocked = await client.post(f"{BASE}/logout-all", headers=headers)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "MUST_CHANGE_PASSWORD"

    weak = await client.post(
        f"{BASE}/change-password",
        headers=headers,
        json={"current_password": PASSWORD, "new_password": "short"},
    )
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "PASSWORD_POLICY_FAILED"
    assert "too_short" in weak.json()["error"]["details"]["violations"]

    changed = await client.post(
        f"{BASE}/change-password",
        headers=headers,
        json={"current_password": PASSWORD, "new_password": "a-brand-new-passphrase-1"},
    )
    assert changed.status_code == 200 and changed.json()["must_change_password"] is False
    assert (await client.get(f"{BASE}/me", headers=headers)).status_code == 401  # old token died (tv bump)
    assert (await client.post(f"{BASE}/logout-all", headers=bearer(changed))).status_code == 200
    assert await audit_count(db_session, "AUTH_PASSWORD_CHANGED", user.id) == 1


async def test_password_history_blocks_last_5(
    client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "rate_limit_login", "1000/minute")  # 12 changes in a row
    passwords = [PASSWORD] + [f"passphrase-number-{n}-long" for n in range(1, 6)]
    headers = bearer(await login(client, world.admin.email))

    async def change(old: str, new: str):
        return await client.post(
            f"{BASE}/change-password", headers=headers, json={"current_password": old, "new_password": new}
        )

    for old, new in zip(passwords, passwords[1:], strict=False):
        response = await change(old, new)
        assert response.status_code == 200
        headers = bearer(response)

    for reused in passwords[1:]:  # current password and the 4 before it
        blocked = await change(passwords[-1], reused)
        assert blocked.status_code == 422
        assert "reused_password" in blocked.json()["error"]["details"]["violations"]
    assert (await change(passwords[-1], passwords[0])).status_code == 200  # 6th most recent is free again


async def test_logout_all_bumps_token_version(client: AsyncClient, world: World, db_session: AsyncSession):
    one, two = await login(client, world.hr.email), await login(client, world.hr.email)
    before = await user_col(db_session, "token_version", world.hr.id)

    assert (await client.post(f"{BASE}/logout-all", headers=bearer(one))).status_code == 200

    assert await user_col(db_session, "token_version", world.hr.id) == before + 1
    for response in (one, two):
        assert (await client.get(f"{BASE}/me", headers=bearer(response))).status_code == 401
    assert (await refresh_with(client, refresh_cookie(two))).status_code == 401
    assert await audit_count(db_session, "AUTH_SIGNED_OUT_EVERYWHERE", world.hr.id) == 1


async def test_totp_setup_verify_login(client: AsyncClient, world: World, db_session: AsyncSession):
    headers = bearer(await login(client, world.admin.email))
    setup = await client.post(f"{BASE}/totp/setup", headers=headers)
    secret = setup.json()["secret"]
    assert setup.status_code == 200 and setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert await user_col(db_session, "totp_secret_enc", world.admin.id) != secret

    bad = await client.post(f"{BASE}/totp/verify", headers=headers, json={"code": "000000"})
    assert bad.status_code == 401
    verified = await client.post(f"{BASE}/totp/verify", headers=headers, json={"code": totp.code_at(secret)})
    assert verified.json()["enabled"] is True
    recovery = verified.json()["recovery_codes"]
    assert len(recovery) == 8 and await audit_count(db_session, "AUTH_TOTP_ENABLED", world.admin.id) == 1

    challenge = await login(client, world.admin.email)
    assert challenge.json()["totp_required"] is True and challenge.json()["access_token"] is None
    token = challenge.json()["totp_token"]

    wrong = await client.post(f"{BASE}/login/totp", json={"totp_token": token, "code": "000000"})
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"
    tampered = await client.post(
        f"{BASE}/login/totp", json={"totp_token": token + "x", "code": totp.code_at(secret)}
    )
    assert tampered.status_code == 401

    done = await client.post(f"{BASE}/login/totp", json={"totp_token": token, "code": totp.code_at(secret)})
    assert done.status_code == 200 and done.json()["access_token"]
    assert (await client.get(f"{BASE}/me", headers=bearer(done))).status_code == 200

    # A recovery code works once.
    once = await client.post(f"{BASE}/login/totp", json={"totp_token": token, "code": recovery[0]})
    twice = await client.post(f"{BASE}/login/totp", json={"totp_token": token, "code": recovery[0]})
    assert once.status_code == 200 and twice.status_code == 401

    off = await client.post(
        f"{BASE}/totp/disable",
        headers=bearer(done),
        json={"password": PASSWORD, "code": totp.code_at(secret)},
    )
    assert (
        off.status_code == 200 and (await login(client, world.admin.email)).json()["totp_required"] is False
    )


class FakeGoogle:
    def __init__(self, identity: GoogleIdentity | None):
        self.identity = identity

    async def verify(self, id_token: str) -> GoogleIdentity:
        if self.identity is None:
            raise AppError("UNAUTHENTICATED")
        return self.identity


async def test_google_requires_existing_active_user_in_allowed_domain(
    client: AsyncClient, db_session: AsyncSession
):
    org = await factories.make_org(db_session, allowed_google_domains=["acme.test"])
    off = await factories.make_org(db_session)  # empty allowed list: Google sign-in is off
    hr = await factories.make_user(db_session, "hr_user", org, email="hr@acme.test")
    gone = await factories.make_user(db_session, "hr_user", org, email="gone@acme.test", status="inactive")
    outsider = await factories.make_user(db_session, "hr_user", org, email="out@elsewhere.test")
    disabled_org = await factories.make_user(db_session, "hr_user", off, email="x@off.test")
    await db_session.commit()

    async def google(email: str | None, *, verified: bool = True, sub: str = "sub-1"):
        identity = GoogleIdentity(email, verified, sub) if email else None
        set_google_verifier(FakeGoogle(identity))
        return await client.post(f"{BASE}/google", json={"id_token": "x" * 20})

    for email, verified in [
        ("nobody@acme.test", True),  # no auto-provisioning
        (gone.email, True),  # inactive
        (outsider.email, True),  # domain not in the allowed list
        (disabled_org.email, True),  # empty list
        (hr.email, False),  # unverified email
    ]:
        denied = await google(email, verified=verified)
        assert denied.status_code == 401 and denied.json()["error"]["code"] == "INVALID_CREDENTIALS", email
    assert (await google(None)).status_code == 401  # invalid token

    ok = await google(hr.email)
    assert ok.status_code == 200 and ok.json()["user"]["id"] == str(hr.id)
    assert await user_col(db_session, "google_sub", hr.id) == "sub-1"
    assert (await google(hr.email, sub="other-sub")).status_code == 401  # a different Google account


_ANY, _ADMINS = ("hr", "admin", "sa"), ("admin", "sa")


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("GET", "/me", _ANY),
        ("POST", "/logout-all", _ANY),
        ("POST", "/totp/setup", _ADMINS),  # spec 6/M1: OA and SA only
    ],
)
async def test_permission_matrix(client: AsyncClient, world: World, method: str, path: str, allowed: tuple):
    users = {"hr": world.hr, "admin": world.admin, "sa": world.sa}
    anonymous = await client.request(method, BASE + path, json={"code": "123456"})
    assert anonymous.status_code == 401
    for key, user in users.items():
        headers = bearer(await login(client, user.email))
        response = await client.request(method, BASE + path, headers=headers, json={"code": "123456"})
        if key in allowed:
            assert response.status_code not in (401, 403), (key, path)
        else:
            assert response.status_code == 403 and response.json()["error"]["code"] == "FORBIDDEN", (
                key,
                path,
            )
