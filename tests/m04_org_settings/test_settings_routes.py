"""Profile, security, retention and invite settings (spec 6/M4)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.m04_org_settings.conftest import audit_rows, row

API = "/api/v1/org"


async def test_profile_get_and_patch(http, tenant, db_session):
    got = (await http.get(f"{API}/profile", headers=tenant.headers)).json()
    assert got["name"] == tenant.org.name and got["locale"] == "en" and got["timezone"] == "UTC"

    r = await http.patch(
        f"{API}/profile",
        headers=tenant.headers,
        json={"name": "  Acme Hiring  ", "timezone": "Asia/Kolkata"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Acme Hiring" and r.json()["timezone"] == "Asia/Kolkata"
    saved = await row(db_session, "SELECT name, timezone FROM organizations WHERE id = :i", i=tenant.org.id)
    assert saved == {"name": "Acme Hiring", "timezone": "Asia/Kolkata"}
    assert len(await audit_rows(db_session, tenant.org.id, "SETTINGS_UPDATED")) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "x" * 201},
        {"timezone": "Mars/Base"},
        {"locale": "klingon-xx-yy"},
        {"slug": "x"},
    ],
)
async def test_profile_rejects_bad_input(http, tenant, body):
    r = await http.patch(f"{API}/profile", headers=tenant.headers, json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_security_settings_validation(http, tenant, db_session):
    base = f"{API}/settings/security"
    got = (await http.get(base, headers=tenant.headers)).json()
    assert got["session_timeout_minutes"] == 30
    assert got["password_policy"] == {"min_length": 12, "history": 5, "blocklist": True}
    assert got["totp_required_for_admins"] is False and got["allowed_google_domains"] == []

    ok = await http.patch(
        base,
        headers=tenant.headers,
        json={
            "allowed_google_domains": ["Example.COM", "example.com", " b.io "],
            "session_timeout_minutes": 45,
            "password_policy": {"min_length": 14, "history": 8},
            "totp_required_for_admins": True,
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["allowed_google_domains"] == ["example.com", "b.io"]  # lower-cased, deduped, trimmed
    assert body["session_timeout_minutes"] == 45 and body["totp_required_for_admins"] is True
    assert body["password_policy"] == {"min_length": 14, "history": 8, "blocklist": True}

    # partial update leaves everything else alone
    await http.patch(base, headers=tenant.headers, json={"session_timeout_minutes": 60})
    again = (await http.get(base, headers=tenant.headers)).json()
    assert (
        again["allowed_google_domains"] == ["example.com", "b.io"] and again["session_timeout_minutes"] == 60
    )

    audits = await audit_rows(db_session, tenant.org.id, "SETTINGS_UPDATED")
    assert len(audits) == 2
    assert audits[0]["after"]["pw_policy"]["min_length"] == 14  # not redacted: key is not `password*`
    assert audits[0]["before"]["session_timeout_minutes"] == 30


@pytest.mark.parametrize(
    "body",
    [
        {"allowed_google_domains": ["not a domain"]},
        {"allowed_google_domains": ["https://example.com"]},
        {"allowed_google_domains": ["a@example.com"]},
        {"allowed_google_domains": ["localhost"]},
        {"allowed_google_domains": [f"d{i}.com" for i in range(51)]},
        {"session_timeout_minutes": 4},
        {"session_timeout_minutes": 481},
        {"password_policy": {"min_length": 11}},
        {"password_policy": {"min_length": 129}},
        {"password_policy": {"history": 4}},
        {"password_policy": {"blocklist": False}},
        {"password_policy": {"min_length": 12, "evil": 1}},
        {"totp_required_for_admins": "maybe"},
    ],
)
async def test_security_settings_rejects_weak_or_malformed(http, tenant, body):
    r = await http.patch(f"{API}/settings/security", headers=tenant.headers, json=body)
    assert r.status_code == 422, body
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "days,status", [(6, 422), (7, 200), (90, 200), (730, 200), (731, 422), (0, 422), (-5, 422)]
)
async def test_retention_range_7_to_730(http, tenant, db_session, days, status):
    r = await http.patch(f"{API}/settings/retention", headers=tenant.headers, json={"retention_days": days})
    assert r.status_code == status
    if status == 200:
        assert r.json() == {"retention_days": days}
        saved = await row(
            db_session, "SELECT retention_days FROM organizations WHERE id = :i", i=tenant.org.id
        )
        assert saved["retention_days"] == days
    else:
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
        assert r.json()["error"]["details"]["fields"][0]["field"].endswith("retention_days")


async def test_retention_requires_integer(http, tenant):
    for bad in ("ninety", 90.5, None, True):
        r = await http.patch(
            f"{API}/settings/retention", headers=tenant.headers, json={"retention_days": bad}
        )
        assert r.status_code == 422, bad
    r = await http.patch(f"{API}/settings/retention", headers=tenant.headers, json={})
    assert r.status_code == 422


async def test_retention_get_default(http, tenant):
    assert (await http.get(f"{API}/settings/retention", headers=tenant.headers)).json() == {
        "retention_days": 90
    }


@pytest.mark.parametrize("hours,status", [(0, 422), (1, 200), (72, 200), (720, 200), (721, 422)])
async def test_invite_default_expiry_range(http, tenant, hours, status):
    r = await http.patch(
        f"{API}/settings/invites", headers=tenant.headers, json={"invite_default_expiry_hours": hours}
    )
    assert r.status_code == status
    if status == 200:
        assert r.json() == {"invite_default_expiry_hours": hours}


async def test_settings_never_touch_another_org(http, tenant, other_tenant, db_session):
    await http.patch(f"{API}/settings/retention", headers=tenant.headers, json={"retention_days": 30})
    other = (await http.get(f"{API}/settings/retention", headers=other_tenant.headers)).json()
    assert other == {"retention_days": 90}
    assert isinstance(db_session, AsyncSession)
