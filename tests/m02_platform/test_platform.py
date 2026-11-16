"""M2 platform administration (SA only): spec 6/M2, 4.3, 4.4, 2.2 (A-02, A-04, A-05, A-11..A-14)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core import flags, security
from app.core.branding_text import STARTER_THEME
from app.db import AiUsageLog
from app.main import create_app
from tests.factories import (
    auth_headers,
    make_attempt,
    make_invite,
    make_org,
    make_role,
    make_user,
)

BASE = "/api/v1/platform"


async def headers_for(session, user):
    await session.commit()
    return await auth_headers(session, user.role, None, user=user)


async def sa_headers(session):
    return await headers_for(session, await make_user(session, "super_admin"))


async def scalar(session, sql, **params):
    return (await session.execute(text(sql), params)).scalar_one()


async def audit_actions(session, org_id=None):
    sql = "select action from audit_logs"
    params = {}
    if org_id is not None:
        sql += " where org_id = :o"
        params = {"o": org_id}
    return [row[0] for row in (await session.execute(text(sql), params)).all()]


# --------------------------------------------------------------------------- organizations


async def test_create_org_seeds_flags_theme_and_returns_temp_password_once(client, db_session):
    h = await sa_headers(db_session)
    existing = await make_org(db_session)
    await db_session.commit()

    r = await client.put(f"{BASE}/features/defaults", json={"defaults": {"reports_chat": False}}, headers=h)
    assert r.status_code == 200

    r = await client.post(
        f"{BASE}/orgs",
        json={"name": "Acme Corp", "admin": {"name": "Ada Admin", "email": "Ada@Acme.example.com "}},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    temp = body["temporary_password"]
    assert len(temp) >= 12
    assert body["admin"]["role"] == "org_admin"
    assert body["admin"]["email"] == "ada@acme.example.com"
    assert body["admin"]["must_change_password"] is True
    assert body["organization"]["status"] == "active"
    org_id = body["organization"]["id"]

    # flags seeded from the defaults, one explicit row per catalog key
    got = dict(
        (
            await db_session.execute(
                text("select flag_key, enabled from org_feature_flags where org_id = :o"), {"o": org_id}
            )
        ).all()
    )
    assert set(got) == set(flags.FLAG_KEYS)
    assert got["reports_chat"] is False and got["team"] is True

    # the MRK starter theme is copied into the org's own theme row
    theme = (
        await db_session.execute(
            text(
                "select scope, primary_color, accent_color, background_style, font_key "
                "from branding_themes where org_id = :o"
            ),
            {"o": org_id},
        )
    ).one()
    assert tuple(theme) == (
        "org",
        STARTER_THEME["primary"],
        STARTER_THEME["accent"],
        STARTER_THEME["background_style"],
        STARTER_THEME["font_key"],
    )

    # first org admin: Argon2id hash of the temp password, forced password change
    admin_row = (
        await db_session.execute(
            text(
                "select password_hash, must_change_password, role, org_id from users where email = 'ada@acme.example.com'"
            )
        )
    ).one()
    assert admin_row.password_hash.startswith("$argon2id$")
    assert security.verify_password(admin_row.password_hash, temp)
    assert admin_row.must_change_password is True and admin_row.role == "org_admin"
    assert str(admin_row.org_id) == org_id

    # shown once: no read endpoint, audit row or flag listing carries it again
    for path in ("/orgs", f"/orgs/{org_id}", f"/orgs/{org_id}/users", f"/orgs/{org_id}/features"):
        again = await client.get(BASE + path, headers=h)
        assert again.status_code == 200
        assert temp not in again.text and "temporary_password" not in again.text
    leaked = await scalar(
        db_session,
        "select count(*) from audit_logs where coalesce(after::text, '') like :p or coalesce(before::text, '') like :p",
        p=f"%{temp}%",
    )
    assert leaked == 0

    actions = await audit_actions(db_session, uuid.UUID(org_id))
    assert "ORG_CREATED" in actions and "ORG_ADMIN_CREATED" in actions

    # changing the default flag set later never touches organizations that already exist
    other = await client.get(f"{BASE}/orgs/{existing.id}/features", headers=h)
    assert other.json()["flags"]["reports_chat"]["effective"] is True


async def test_create_org_conflicts_and_validation(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session, slug="taken")
    taken = await make_user(db_session, "hr_user", org, email="used@example.com")
    await db_session.commit()
    payload = {"name": "Dup", "slug": "taken", "admin": {"name": "A", "email": "a@dup.example.com"}}
    r = await client.post(f"{BASE}/orgs", json=payload, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONFLICT"
    payload = {"name": "Dup2", "admin": {"name": "A", "email": taken.email}}
    r = await client.post(f"{BASE}/orgs", json=payload, headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["field"] == "email"
    # the failed attempt left no organization behind
    assert await scalar(db_session, "select count(*) from organizations where name = 'Dup2'") == 0
    r = await client.post(
        f"{BASE}/orgs", json={"name": " ", "admin": {"name": "A", "email": "nope"}}, headers=h
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_list_get_patch_org_and_pagination(client, db_session):
    h = await sa_headers(db_session)
    orgs = [await make_org(db_session, name=f"Zeta {i}") for i in range(3)]
    await make_org(db_session, name="Other", status="disabled")
    await db_session.commit()

    r = await client.get(f"{BASE}/orgs", params={"q": "zeta", "page_size": 2, "sort": "name:desc"}, headers=h)
    page = r.json()
    assert r.status_code == 200 and page["total"] == 3 and len(page["items"]) == 2
    assert [o["name"] for o in page["items"]] == ["Zeta 2", "Zeta 1"]
    r = await client.get(f"{BASE}/orgs", params={"status": "disabled"}, headers=h)
    assert [o["name"] for o in r.json()["items"]] == ["Other"]
    assert (await client.get(f"{BASE}/orgs", params={"page_size": 1000}, headers=h)).status_code == 422
    assert (await client.get(f"{BASE}/orgs", params={"sort": "password:asc"}, headers=h)).status_code == 422

    r = await client.patch(f"{BASE}/orgs/{orgs[0].id}", json={"name": "Renamed", "is_demo": True}, headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and r.json()["is_demo"] is True
    assert (await client.get(f"{BASE}/orgs/{orgs[0].id}", headers=h)).json()["name"] == "Renamed"
    assert (await client.get(f"{BASE}/orgs/{uuid.uuid4()}", headers=h)).status_code == 404
    assert "ORG_UPDATED" in await audit_actions(db_session, orgs[0].id)


async def test_disable_enable_org_blocks_its_users(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    oa = await make_user(db_session, "org_admin", org)
    oa_h = await headers_for(db_session, oa)

    assert (await client.get("/api/v1/team/users", headers=oa_h)).status_code == 200
    r = await client.post(f"{BASE}/orgs/{org.id}/disable", json={"reason": "contract paused"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    blocked = await client.get("/api/v1/team/users", headers=oa_h)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "ORG_DISABLED"
    again = await client.post(f"{BASE}/orgs/{org.id}/disable", json={}, headers=h)
    assert again.status_code == 409 and again.json()["error"]["code"] == "CONFLICT"

    r = await client.post(f"{BASE}/orgs/{org.id}/enable", json={}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert (await client.get("/api/v1/team/users", headers=oa_h)).status_code == 200
    actions = await audit_actions(db_session, org.id)
    assert "ORG_DISABLED" in actions and "ORG_ENABLED" in actions


async def test_overview_counts_without_candidate_content(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    oa = await make_user(db_session, "org_admin", org)
    hr = await make_user(db_session, "hr_user", org)
    await make_user(db_session, "hr_user", org, status="inactive")
    role = await make_role(db_session, org, hr)
    invite = await make_invite(db_session, org, role)
    await make_attempt(db_session, invite, status="submitted")
    db_session.add(
        AiUsageLog(org_id=org.id, user_id=hr.id, feature="question_gen", provider="p", model="m", status="ok")
    )
    await db_session.commit()
    del oa

    r = await client.get(f"{BASE}/orgs/{org.id}/overview", headers=h)
    assert r.status_code == 200
    body = r.json()
    # make_invite creates an assessment owned by one more HR user.
    assert body["users"] == {"total": 4, "active": 3, "inactive": 1, "org_admins": 1, "hr_users": 3}
    assert body["open_roles"] == 1 and body["roles_total"] == 1
    assert body["invites"]["total"] == 1 and body["invites"]["submitted"] == 1
    assert body["attempts"] == {"total": 1, "submitted": 1}
    assert body["ai_usage"]["calls_this_month"] == 1
    assert "candidate" not in r.text.lower()
    assert (await client.get(f"{BASE}/orgs/{uuid.uuid4()}/overview", headers=h)).status_code == 404


async def test_org_users_and_roles_are_read_only_lists_with_owner_inactive(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    other = await make_org(db_session)
    hr = await make_user(db_session, "hr_user", org)
    gone = await make_user(db_session, "hr_user", org, status="inactive")
    await make_user(db_session, "hr_user", other)
    await make_role(db_session, org, hr, title="Active owner")
    await make_role(db_session, org, gone, title="Orphaned")
    await make_role(db_session, other, None, title="Foreign")
    await db_session.commit()

    users = (await client.get(f"{BASE}/orgs/{org.id}/users", headers=h)).json()
    assert users["total"] == 2
    assert all("password_hash" not in u and u["org_id"] == str(org.id) for u in users["items"])
    roles = (await client.get(f"{BASE}/orgs/{org.id}/roles", headers=h)).json()
    marks = {r["title"]: r["owner_inactive"] for r in roles["items"]}
    assert marks == {"Active owner": False, "Orphaned": True}
    for path in ("users", "roles"):
        assert (await client.get(f"{BASE}/orgs/{uuid.uuid4()}/{path}", headers=h)).status_code == 404


# --------------------------------------------------------------------------- org admins


async def test_create_admin_returns_temp_password_once_and_rejects_duplicates(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    await make_user(db_session, "org_admin", org, email="first@example.com")
    await db_session.commit()

    r = await client.post(
        f"{BASE}/orgs/{org.id}/admins", json={"name": "Second", "email": "second@example.com"}, headers=h
    )
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["role"] == "org_admin" and body["user"]["org_id"] == str(org.id)
    assert security.verify_password(
        await scalar(db_session, "select password_hash from users where email = 'second@example.com'"),
        body["temporary_password"],
    )
    dup = await client.post(
        f"{BASE}/orgs/{org.id}/admins", json={"name": "Again", "email": "SECOND@example.com"}, headers=h
    )
    assert dup.status_code == 409
    assert (
        await client.post(
            f"{BASE}/orgs/{uuid.uuid4()}/admins", json={"name": "X", "email": "x@example.com"}, headers=h
        )
    ).status_code == 404
    assert "ORG_ADMIN_CREATED" in await audit_actions(db_session, org.id)


async def test_last_admin_deactivation_blocked_unless_sa_confirms(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    oa = await make_user(db_session, "org_admin", org)
    oa_h = await headers_for(db_session, oa)

    r = await client.post(f"{BASE}/users/{oa.id}/deactivate", json={}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "LAST_ADMIN_PROTECTED"
    assert await scalar(db_session, "select status from users where id = :i", i=oa.id) == "active"

    r = await client.post(
        f"{BASE}/users/{oa.id}/deactivate",
        json={"confirm_last_admin": True, "reason": "contract ended"},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["status"] == "inactive"
    # the deactivated admin's token dies with the token-version bump
    assert (await client.get("/api/v1/team/users", headers=oa_h)).status_code == 401
    reason = await scalar(db_session, "select reason from audit_logs where action = 'ORG_ADMIN_DEACTIVATED'")
    assert reason == "contract ended"

    # with a second active admin the confirmation is not needed; reactivation round-trips
    second = await make_user(db_session, "org_admin", org)
    await db_session.commit()
    r = await client.post(f"{BASE}/users/{oa.id}/reactivate", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "active"
    r = await client.post(f"{BASE}/users/{second.id}/deactivate", json={}, headers=h)
    assert r.status_code == 200
    again = await client.post(f"{BASE}/users/{second.id}/reactivate", headers=h)
    assert again.status_code == 200
    conflict = await client.post(f"{BASE}/users/{second.id}/reactivate", headers=h)
    assert conflict.status_code == 409
    actions = await audit_actions(db_session, org.id)
    assert "ORG_ADMIN_REACTIVATED" in actions


async def test_platform_user_endpoints_only_target_org_admins(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    hr = await make_user(db_session, "hr_user", org)
    other_sa = await make_user(db_session, "super_admin")
    await db_session.commit()
    for target in (hr, other_sa):
        for action in ("deactivate", "reactivate", "reset-password", "unlock"):
            r = await client.post(f"{BASE}/users/{target.id}/{action}", json={}, headers=h)
            assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", (target.role, action)
    assert (
        await client.post(f"{BASE}/users/{uuid.uuid4()}/deactivate", json={}, headers=h)
    ).status_code == 404


async def test_default_sa_untargetable_by_any_endpoint(client, db_session):
    default_sa = await make_user(db_session, "super_admin", is_default_super_admin=True)
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    oa = await make_user(db_session, "org_admin", org)
    oa_h = await headers_for(db_session, oa)

    for action in ("deactivate", "reactivate", "reset-password", "unlock"):
        r = await client.post(f"{BASE}/users/{default_sa.id}/{action}", json={}, headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "PROTECTED_ACCOUNT", action

    team = "/api/v1/team/users"
    attempts = [
        ("get", f"{team}/{default_sa.id}", None),
        ("patch", f"{team}/{default_sa.id}", {"name": "Hacked"}),
        ("post", f"{team}/{default_sa.id}/deactivate", {}),
        ("post", f"{team}/{default_sa.id}/reactivate", None),
        ("post", f"{team}/{default_sa.id}/reset-password", None),
        ("post", f"{team}/{default_sa.id}/unlock", None),
        ("post", f"{team}/{default_sa.id}/sign-out-everywhere", None),
    ]
    for method, path, body in attempts:
        kwargs = {"json": body} if body is not None else {}
        r = await client.request(method.upper(), path, headers=oa_h, **kwargs)
        assert r.status_code == 404, (method, path)

    # not listed anywhere an org admin or the platform lists users
    assert default_sa.email not in (await client.get(team, headers=oa_h)).text
    assert default_sa.email not in (await client.get(f"{BASE}/orgs/{org.id}/users", headers=h)).text
    row = (
        await db_session.execute(text("select status, name from users where id = :i"), {"i": default_sa.id})
    ).one()
    assert row.status == "active" and row.name != "Hacked"


async def test_sa_reset_password_and_unlock_admin(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    oa = await make_user(db_session, "org_admin", org, password="Old-password-123!")
    oa.failed_login_count = 5
    oa.locked_until = datetime.now(UTC) + timedelta(minutes=15)
    oa_h = await headers_for(db_session, oa)

    r = await client.post(f"{BASE}/users/{oa.id}/unlock", headers=h)
    assert r.status_code == 200 and r.json()["is_locked"] is False
    row = (
        await db_session.execute(
            text("select failed_login_count, locked_until from users where id = :i"), {"i": oa.id}
        )
    ).one()
    assert row.failed_login_count == 0 and row.locked_until is None

    r = await client.post(f"{BASE}/users/{oa.id}/reset-password", headers=h)
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    row = (
        await db_session.execute(
            text("select password_hash, must_change_password, token_version from users where id = :i"),
            {"i": oa.id},
        )
    ).one()
    assert security.verify_password(row.password_hash, temp) and row.must_change_password is True
    assert row.token_version == 1
    assert (await client.get("/api/v1/team/users", headers=oa_h)).status_code == 401
    actions = await audit_actions(db_session, org.id)
    assert "AUTH_ACCOUNT_UNLOCKED" in actions and "AUTH_TEMP_PASSWORD_ISSUED" in actions
    assert temp not in str(
        await scalar(db_session, "select string_agg(coalesce(after::text,''), ' ') from audit_logs")
    )


# --------------------------------------------------------------------------- feature flags


async def test_flag_catalog_and_org_flag_bulk_set_audited(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    await db_session.commit()

    cat = (await client.get(f"{BASE}/features/catalog", headers=h)).json()
    keys = [f["key"] for f in cat["items"]]
    assert set(keys) == set(flags.FLAG_KEYS)
    assert next(f for f in cat["items"] if f["key"] == "watermark")["visible_to_org"] is False
    assert next(f for f in cat["items"] if f["key"] == "invites")["depends_on"] == ["roles", "assessments"]

    r = await client.put(
        f"{BASE}/orgs/{org.id}/features",
        json={"flags": {"watermark": False, "roles": False}, "reason": "demo"},
        headers=h,
    )
    assert r.status_code == 200
    flags_out = r.json()["flags"]
    assert flags_out["watermark"]["enabled"] is False and flags_out["roles"]["enabled"] is False
    # dependency: invites stays enabled itself but is not effective without roles
    assert flags_out["invites"]["enabled"] is True and flags_out["invites"]["effective"] is False
    changed = (
        await db_session.execute(
            text(
                "select entity_id, reason, actor_name_snapshot from audit_logs where action = 'FEATURE_FLAG_CHANGED' order by entity_id"
            )
        )
    ).all()
    assert [c.entity_id for c in changed] == ["roles", "watermark"] and {c.reason for c in changed} == {
        "demo"
    }
    assert all(c.actor_name_snapshot for c in changed)

    # unchanged values write no audit rows; unknown keys are rejected
    n = len(changed)
    await client.put(f"{BASE}/orgs/{org.id}/features", json={"flags": {"watermark": False}}, headers=h)
    assert (
        await scalar(db_session, "select count(*) from audit_logs where action = 'FEATURE_FLAG_CHANGED'") == n
    )
    bad = await client.put(
        f"{BASE}/orgs/{org.id}/features", json={"flags": {"no_such_flag": True}}, headers=h
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "VALIDATION_ERROR"
    assert (await client.get(f"{BASE}/orgs/{uuid.uuid4()}/features", headers=h)).status_code == 404


async def test_feature_defaults_apply_to_new_orgs_only(client, db_session):
    h = await sa_headers(db_session)
    legacy = await make_org(db_session)  # no override rows: would follow the catalog default
    await db_session.commit()
    r = await client.put(
        f"{BASE}/features/defaults", json={"defaults": {"audit_export": False}, "reason": "phase"}, headers=h
    )
    assert r.status_code == 200 and r.json()["defaults"]["audit_export"] is False
    assert (await client.get(f"{BASE}/features/defaults", headers=h)).json()["defaults"][
        "audit_export"
    ] is False
    assert (await client.get(f"{BASE}/orgs/{legacy.id}/features", headers=h)).json()["flags"]["audit_export"][
        "effective"
    ] is True
    assert (
        await scalar(
            db_session,
            "select count(*) from audit_logs where action = 'FEATURE_DEFAULTS_CHANGED' and org_id is null",
        )
        == 1
    )
    bad = await client.put(f"{BASE}/features/defaults", json={"defaults": {"nope": True}}, headers=h)
    assert bad.status_code == 422
    await client.put(f"{BASE}/features/defaults", json={"defaults": {"audit_export": True}}, headers=h)


async def test_flag_off_returns_FEATURE_DISABLED_and_data_kept(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    oa = await make_user(db_session, "org_admin", org)
    hr = await make_user(db_session, "hr_user", org, email="kept@example.com")
    oa_h = await headers_for(db_session, oa)
    users_before = await scalar(db_session, "select count(*) from users where org_id = :o", o=org.id)

    assert (await client.get("/api/v1/team/users", headers=oa_h)).status_code == 200
    r = await client.put(
        f"{BASE}/orgs/{org.id}/features", json={"flags": {"team": False}, "reason": "billing"}, headers=h
    )
    assert r.status_code == 200 and r.json()["flags"]["team"]["effective"] is False

    for method, path in (
        ("GET", "/api/v1/team/users"),
        ("POST", f"/api/v1/team/users/{hr.id}/unlock"),
        ("GET", f"/api/v1/team/users/{hr.id}"),
    ):
        blocked = await client.request(method, path, headers=oa_h)
        assert blocked.status_code == 403, path
        assert blocked.json()["error"]["code"] == "FEATURE_DISABLED"
        assert blocked.json()["error"]["details"] == {"flag": "team"}
    # data is never deleted
    assert await scalar(db_session, "select count(*) from users where org_id = :o", o=org.id) == users_before
    assert await scalar(db_session, "select count(*) from users where email = 'kept@example.com'") == 1
    # the SA is not flag-gated and still reads the org's users
    assert (await client.get(f"{BASE}/orgs/{org.id}/users", headers=h)).json()["total"] == users_before

    await client.put(f"{BASE}/orgs/{org.id}/features", json={"flags": {"team": True}}, headers=h)
    back = await client.get("/api/v1/team/users", headers=oa_h)
    assert back.status_code == 200 and back.json()["total"] == users_before
    # an org admin cannot flip flags
    forbidden = await client.put(
        f"{BASE}/orgs/{org.id}/features", json={"flags": {"team": True}}, headers=oa_h
    )
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "FORBIDDEN"


# --------------------------------------------------------------------------- misuse flags


async def test_misuse_flag_raise_requires_comment_and_notifies_oa(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    oa1 = await make_user(db_session, "org_admin", org)
    oa2 = await make_user(db_session, "org_admin", org)
    await make_user(db_session, "org_admin", org, status="inactive")
    hr = await make_user(db_session, "hr_user", org)
    foreign_hr = await make_user(db_session, "hr_user", await make_org(db_session))
    await db_session.commit()
    url = f"{BASE}/orgs/{org.id}/misuse-flags"

    for body in ({}, {"comment": ""}, {"comment": "   "}):
        r = await client.post(url, json=body, headers=h)
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR", body
    assert await scalar(db_session, "select count(*) from misuse_flags") == 0

    r = await client.post(
        url, json={"comment": "Shared candidate links publicly", "user_id": str(hr.id)}, headers=h
    )
    assert r.status_code == 201, r.text
    flag = r.json()
    assert flag["status"] == "open" and flag["comment"] == "Shared candidate links publicly"
    assert flag["user_id"] == str(hr.id)

    notes = (
        await db_session.execute(
            text("select user_id, type, payload, entity_id from notifications where org_id = :o"),
            {"o": org.id},
        )
    ).all()
    assert {n.user_id for n in notes} == {oa1.id, oa2.id}  # active org admins only
    assert all(n.type == "misuse_flag_raised" and str(n.entity_id) == flag["id"] for n in notes)
    assert notes[0].payload["comment"] == "Shared candidate links publicly"
    audit = (
        await db_session.execute(
            text("select reason, actor_name_snapshot from audit_logs where action = 'MISUSE_FLAG_RAISED'")
        )
    ).one()
    assert audit.reason == "Shared candidate links publicly"

    # a user from another organization is a 404, not a flag
    r = await client.post(url, json={"comment": "x", "user_id": str(foreign_hr.id)}, headers=h)
    assert r.status_code == 404
    listed = (await client.get(url, headers=h)).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == flag["id"]
    assert (await client.get(url, params={"status": "cleared"}, headers=h)).json()["total"] == 0


async def test_misuse_clear_requires_closing_note(client, db_session):
    h = await sa_headers(db_session)
    org = await make_org(db_session)
    oa = await make_user(db_session, "org_admin", org)
    oa_h = await headers_for(db_session, oa)
    created = await client.post(
        f"{BASE}/orgs/{org.id}/misuse-flags", json={"comment": "Suspicious export volume"}, headers=h
    )
    fid = created.json()["id"]

    for body in ({}, {"closing_note": ""}, {"closing_note": "  "}):
        r = await client.post(f"{BASE}/misuse-flags/{fid}/clear", json=body, headers=h)
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR", body
    assert (
        await scalar(db_session, "select status from misuse_flags where id = :i", i=uuid.UUID(fid)) == "open"
    )
    # the org admin cannot clear their own flag
    assert (
        await client.post(f"{BASE}/misuse-flags/{fid}/clear", json={"closing_note": "ok"}, headers=oa_h)
    ).status_code == 403

    r = await client.post(
        f"{BASE}/misuse-flags/{fid}/clear", json={"closing_note": "Resolved by email on 17 Nov"}, headers=h
    )
    assert r.status_code == 200
    out = r.json()
    assert (
        out["status"] == "cleared"
        and out["closing_note"] == "Resolved by email on 17 Nov"
        and out["cleared_at"]
    )
    types = [
        row[0]
        for row in (
            await db_session.execute(
                text("select type from notifications where user_id = :u order by created_at"), {"u": oa.id}
            )
        ).all()
    ]
    assert types == ["misuse_flag_raised", "misuse_flag_cleared"]
    assert (
        await scalar(db_session, "select reason from audit_logs where action = 'MISUSE_FLAG_CLEARED'")
        == "Resolved by email on 17 Nov"
    )
    again = await client.post(f"{BASE}/misuse-flags/{fid}/clear", json={"closing_note": "again"}, headers=h)
    assert again.status_code == 409
    assert (
        await client.post(f"{BASE}/misuse-flags/{uuid.uuid4()}/clear", json={"closing_note": "x"}, headers=h)
    ).status_code == 404


# --------------------------------------------------------------------------- branding, usage


async def test_platform_branding_isolated_from_org_theme(client, db_session):
    h = await sa_headers(db_session)
    r = await client.post(
        f"{BASE}/orgs",
        json={"name": "Iso Org", "admin": {"name": "A", "email": "iso@org.example.com"}},
        headers=h,
    )
    org_id = r.json()["organization"]["id"]

    first = (await client.get(f"{BASE}/branding", headers=h)).json()
    assert first["scope"] == "platform" and first["primary_color"] == STARTER_THEME["primary"]

    new = {
        "primary_color": "#112233",
        "accent_color": "#AA3300",
        "background_style": "plain_white",
        "font_key": "lato",
    }
    r = await client.put(f"{BASE}/branding", json=new, headers=h)
    assert r.status_code == 200 and r.json()["primary_color"] == "#112233" and r.json()["scope"] == "platform"

    org_theme = (
        await db_session.execute(
            text(
                "select primary_color, accent_color, background_style, font_key from branding_themes where org_id = :o"
            ),
            {"o": org_id},
        )
    ).one()
    assert tuple(org_theme) == (
        STARTER_THEME["primary"],
        STARTER_THEME["accent"],
        STARTER_THEME["background_style"],
        STARTER_THEME["font_key"],
    )
    assert (
        await scalar(
            db_session, "select count(*) from branding_themes where scope = 'platform' and org_id is null"
        )
        == 1
    )

    # and the other way round: an org theme edit never reaches the platform theme
    await db_session.execute(
        text("update branding_themes set primary_color = '#FF0000' where org_id = :o"), {"o": org_id}
    )
    await db_session.commit()
    assert (await client.get(f"{BASE}/branding", headers=h)).json()["primary_color"] == "#112233"

    for bad in (
        {**new, "primary_color": "red"},
        {**new, "font_key": "comic_sans"},
        {**new, "background_style": "neon"},
    ):
        assert (await client.put(f"{BASE}/branding", json=bad, headers=h)).status_code == 422

    r = await client.post(f"{BASE}/branding/reset", headers=h)
    assert r.status_code == 200 and r.json()["primary_color"] == STARTER_THEME["primary"]
    assert (
        await scalar(
            db_session, "select primary_color from branding_themes where org_id = :o", o=uuid.UUID(org_id)
        )
        == "#FF0000"
    )
    assert (
        await scalar(
            db_session,
            "select count(*) from audit_logs where action = 'PLATFORM_BRANDING_UPDATED' and org_id is null",
        )
        == 2
    )


async def test_usage_counts_ai_calls_per_org(client, db_session):
    h = await sa_headers(db_session)
    busy = await make_org(db_session, name="Busy")
    quiet = await make_org(db_session, name="Quiet")
    user = await make_user(db_session, "hr_user", busy)
    for feature in ("question_gen", "question_gen", "scoring"):
        db_session.add(
            AiUsageLog(
                org_id=busy.id,
                user_id=user.id,
                feature=feature,
                provider="p",
                model="m",
                status="ok",
                tokens_in=10,
                tokens_out=5,
            )
        )
    await db_session.commit()
    r = await client.get(f"{BASE}/usage", headers=h)
    assert r.status_code == 200
    items = {i["org_name"]: i for i in r.json()["items"]}
    assert items["Busy"]["calls"] == 3 and items["Busy"]["tokens_in"] == 30
    assert items["Busy"]["by_feature"] == {"question_gen": 2, "scoring": 1}
    assert items["Quiet"]["calls"] == 0 and quiet.name == "Quiet"
    assert (await client.get(f"{BASE}/usage", params={"month": "2020-01"}, headers=h)).json()["items"][0][
        "calls"
    ] == 0
    assert (await client.get(f"{BASE}/usage", params={"month": "bad"}, headers=h)).status_code == 422


# --------------------------------------------------------------------------- permissions


PLATFORM_ROUTES = [
    ("GET", "/orgs"),
    ("POST", "/orgs"),
    ("GET", "/orgs/{org}"),
    ("PATCH", "/orgs/{org}"),
    ("POST", "/orgs/{org}/disable"),
    ("POST", "/orgs/{org}/enable"),
    ("GET", "/orgs/{org}/overview"),
    ("GET", "/orgs/{org}/users"),
    ("GET", "/orgs/{org}/roles"),
    ("POST", "/orgs/{org}/admins"),
    ("POST", "/users/{user}/deactivate"),
    ("POST", "/users/{user}/reactivate"),
    ("POST", "/users/{user}/reset-password"),
    ("POST", "/users/{user}/unlock"),
    ("GET", "/features/catalog"),
    ("GET", "/orgs/{org}/features"),
    ("PUT", "/orgs/{org}/features"),
    ("GET", "/features/defaults"),
    ("PUT", "/features/defaults"),
    ("POST", "/orgs/{org}/misuse-flags"),
    ("GET", "/orgs/{org}/misuse-flags"),
    ("POST", "/misuse-flags/{flag}/clear"),
    ("GET", "/branding"),
    ("PUT", "/branding"),
    ("POST", "/branding/reset"),
    ("GET", "/usage"),
]


@pytest.mark.parametrize(("method", "path"), PLATFORM_ROUTES)
async def test_platform_permission_matrix(client, db_session, method, path):
    """Spec 4.3: every platform capability is super admin only."""
    org = await make_org(db_session)
    oa = await make_user(db_session, "org_admin", org)
    hr = await make_user(db_session, "hr_user", org)
    sa = await make_user(db_session, "super_admin")
    await db_session.commit()
    url = BASE + path.format(org=uuid.uuid4(), user=uuid.uuid4(), flag=uuid.uuid4())

    anon = await client.request(method, url, json={})
    assert anon.status_code == 401
    for user in (oa, hr):
        denied = await client.request(method, url, json={}, headers=await headers_for(db_session, user))
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "FORBIDDEN", (
            user.role,
            method,
            path,
        )
    allowed = await client.request(method, url, json={}, headers=await headers_for(db_session, sa))
    assert allowed.status_code not in (401, 403), (method, path, allowed.text)


async def test_openapi_declares_roles_and_flags_on_every_route():
    schema = create_app().openapi()
    platform = {p: ops for p, ops in schema["paths"].items() if p.startswith("/api/v1/platform")}
    team = {p: ops for p, ops in schema["paths"].items() if p.startswith("/api/v1/team")}
    assert platform and team
    for ops in platform.values():
        for op in ops.values():
            assert op["x-roles"] == ["super_admin"] and op["x-required-flag"] is None
            assert op["summary"] and op["description"] and "403" in op["responses"]
    for ops in team.values():
        for op in ops.values():
            assert op["x-roles"] == ["org_admin"] and op["x-required-flag"] == "team"
            assert op["summary"] and op["description"] and "403" in op["responses"]
