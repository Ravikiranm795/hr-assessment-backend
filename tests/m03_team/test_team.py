"""M3 team management (OA): spec 6/M3, 4.3, 2.2 (A-02, A-04, A-06, A-07), D-05, RF-4."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.modules.m03_team.service import UserAdmin
from sqlalchemy import text

from app.core import security
from tests.factories import auth_headers, make_org, make_role, make_user

TEAM = "/api/v1/team/users"


async def headers_for(session, user):
    await session.commit()
    return await auth_headers(session, user.role, None, user=user)


async def scalar(session, sql, **params):
    return (await session.execute(text(sql), params)).scalar_one()


async def row(session, user_id):
    sql = "select status, token_version, role, name, email, must_change_password, password_hash from users where id = :i"
    return (await session.execute(text(sql), {"i": user_id})).one()


async def actions(session, org_id):
    return [r[0] for r in (await session.execute(text("select action from audit_logs where org_id = :o"), {"o": org_id})).all()]


async def world(session):
    org = await make_org(session)
    oa = await make_user(session, "org_admin", org)
    return org, oa, await headers_for(session, oa)


# --------------------------------------------------------------------------- golden path


async def test_create_list_read_update_hr_user(client, db_session):
    org, oa, h = await world(db_session)

    r = await client.post(TEAM, json={"name": "  Hana HR ", "email": "Hana@Example.com", "locale": "en"}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    user, temp = body["user"], body["temporary_password"]
    assert user["role"] == "hr_user" and user["org_id"] == str(org.id) and user["name"] == "Hana HR"
    assert user["email"] == "hana@example.com" and user["must_change_password"] is True and user["status"] == "active"
    stored = await row(db_session, uuid.UUID(user["id"]))
    assert stored.password_hash.startswith("$argon2id$") and security.verify_password(stored.password_hash, temp)

    # the temp password is never returned again nor written to the audit log
    for r2 in (await client.get(TEAM, headers=h), await client.get(f"{TEAM}/{user['id']}", headers=h)):
        assert temp not in r2.text and "password" not in r2.text
    leaked = await scalar(db_session, "select count(*) from audit_logs where coalesce(after::text,'') like :p", p=f"%{temp}%")
    assert leaked == 0
    assert "USER_CREATED" in await actions(db_session, org.id)

    # list: search, status filter, last login
    other = await make_user(db_session, "hr_user", org, name="Inactive Ivan", status="inactive", last_login_at=datetime.now(UTC))
    await db_session.commit()
    page = (await client.get(TEAM, headers=h)).json()
    assert page["total"] == 3 and {u["role"] for u in page["items"]} == {"org_admin", "hr_user"}
    assert [u["name"] for u in (await client.get(TEAM, params={"q": "hana"}, headers=h)).json()["items"]] == ["Hana HR"]
    inactive = (await client.get(TEAM, params={"status": "inactive"}, headers=h)).json()["items"]
    assert [u["id"] for u in inactive] == [str(other.id)] and inactive[0]["last_login_at"]
    assert (await client.get(TEAM, params={"status": "bogus"}, headers=h)).status_code == 422
    assert (await client.get(TEAM, params={"page_size": 1000}, headers=h)).status_code == 422

    # update name and locale; email is editable only before the first login
    uid = user["id"]
    r = await client.patch(f"{TEAM}/{uid}", json={"name": "Hana Renamed", "locale": "de", "email": "hana.new@example.com"}, headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Hana Renamed" and r.json()["email"] == "hana.new@example.com"
    await db_session.execute(text("update users set last_login_at = now() where id = :i"), {"i": uuid.UUID(uid)})
    await db_session.commit()
    r = await client.patch(f"{TEAM}/{uid}", json={"email": "again@example.com"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"][0] == {"field": "email", "code": "not_editable_after_login"}
    same = await client.patch(f"{TEAM}/{uid}", json={"email": "hana.new@example.com", "name": "Same Email"}, headers=h)
    assert same.status_code == 200
    assert "USER_UPDATED" in await actions(db_session, org.id)

    dup = await client.post(TEAM, json={"name": "Dup", "email": "HANA.NEW@example.com"}, headers=h)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "CONFLICT"
    bad = await client.post(TEAM, json={"name": "x", "email": "not-an-email"}, headers=h)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "VALIDATION_ERROR"
    assert (await client.patch(f"{TEAM}/{uid}", json={"name": " "}, headers=h)).status_code == 422


async def test_account_controls_kill_sessions_and_issue_temp_passwords(client, db_session):
    org, oa, h = await world(db_session)
    hr = await make_user(db_session, "hr_user", org, password="Original-pass-123!")
    hr.failed_login_count = 5
    hr.locked_until = datetime.now(UTC) + timedelta(minutes=15)
    hr_h = await headers_for(db_session, hr)
    uid = hr.id

    ok = await client.get(TEAM, headers=hr_h)
    assert ok.status_code == 403  # HR is not allowed to manage the team

    r = await client.post(f"{TEAM}/{uid}/unlock", headers=h)
    assert r.status_code == 200 and r.json()["is_locked"] is False
    assert await scalar(db_session, "select failed_login_count from users where id = :i", i=uid) == 0

    r = await client.post(f"{TEAM}/{uid}/reset-password", headers=h)
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    stored = await row(db_session, uid)
    assert security.verify_password(stored.password_hash, temp) and stored.must_change_password is True
    assert stored.token_version == 1

    r = await client.post(f"{TEAM}/{uid}/sign-out-everywhere", headers=h)
    assert r.status_code == 200
    assert (await row(db_session, uid)).token_version == 2
    names = await actions(db_session, org.id)
    for expected in ("AUTH_ACCOUNT_UNLOCKED", "AUTH_TEMP_PASSWORD_ISSUED", "AUTH_SIGNED_OUT_EVERYWHERE"):
        assert expected in names
    assert temp not in str(await scalar(db_session, "select string_agg(coalesce(after::text,''), ' ') from audit_logs"))


async def test_deactivate_reactivate_never_deletes_and_kills_tokens(client, db_session):
    org, oa, h = await world(db_session)
    hr = await make_user(db_session, "hr_user", org)
    hr_h = await headers_for(db_session, hr)
    await db_session.execute(
        text("insert into refresh_tokens (user_id, family_id, token_hash, expires_at) values (:u, :f, 'h1', now() + interval '1 day')"),
        {"u": hr.id, "f": uuid.uuid4()},
    )
    await db_session.commit()

    r = await client.post(f"{TEAM}/{hr.id}/deactivate", json={"reason": "left the company"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "inactive"
    stored = await row(db_session, hr.id)
    assert stored.status == "inactive" and stored.token_version == 1  # never deleted, tokens die
    assert await scalar(db_session, "select count(*) from refresh_tokens where user_id = :u and revoked_at is not null", u=hr.id) == 1
    assert (await client.get(TEAM, headers=hr_h)).status_code == 401
    assert await scalar(db_session, "select reason from audit_logs where action = 'USER_DEACTIVATED'") == "left the company"
    twice = await client.post(f"{TEAM}/{hr.id}/deactivate", json={}, headers=h)
    assert twice.status_code == 409

    r = await client.post(f"{TEAM}/{hr.id}/reactivate", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert (await client.post(f"{TEAM}/{hr.id}/reactivate", headers=h)).status_code == 409
    assert await scalar(db_session, "select count(*) from users where id = :i", i=hr.id) == 1
    assert {"USER_DEACTIVATED", "USER_REACTIVATED"} <= set(await actions(db_session, org.id))


# --------------------------------------------------------------------------- admin-only rules


async def test_oa_cannot_create_or_modify_oa_ADMIN_ONLY_ACTION(client, db_session):
    org, oa, h = await world(db_session)
    peer = await make_user(db_session, "org_admin", org)
    hr = await make_user(db_session, "hr_user", org)
    await db_session.commit()

    def code(r):
        return r.json()["error"]["code"]

    r = await client.post(TEAM, json={"name": "Boss", "email": "boss@example.com", "role": "org_admin"}, headers=h)
    assert r.status_code == 403 and code(r) == "ADMIN_ONLY_ACTION"
    r = await client.post(TEAM, json={"name": "Root", "email": "root@example.com", "role": "super_admin"}, headers=h)
    assert r.status_code == 403 and code(r) == "ADMIN_ONLY_ACTION"
    assert await scalar(db_session, "select count(*) from users where email in ('boss@example.com','root@example.com')") == 0
    r = await client.post(TEAM, json={"name": "Odd", "email": "odd@example.com", "role": "wizard"}, headers=h)
    assert r.status_code == 422

    # promotion of an HR user is an admin-only action too
    r = await client.patch(f"{TEAM}/{hr.id}", json={"role": "org_admin"}, headers=h)
    assert r.status_code == 403 and code(r) == "ADMIN_ONLY_ACTION"
    assert (await row(db_session, hr.id)).role == "hr_user"

    # no modification of any org admin, including the caller's own record
    for target in (peer, oa):
        calls = [
            ("PATCH", f"{TEAM}/{target.id}", {"name": "Changed"}),
            ("POST", f"{TEAM}/{target.id}/deactivate", {}),
            ("POST", f"{TEAM}/{target.id}/reactivate", None),
            ("POST", f"{TEAM}/{target.id}/reset-password", None),
            ("POST", f"{TEAM}/{target.id}/unlock", None),
            ("POST", f"{TEAM}/{target.id}/sign-out-everywhere", None),
        ]
        for method, path, body in calls:
            kwargs = {"json": body} if body is not None else {}
            r = await client.request(method, path, headers=h, **kwargs)
            assert r.status_code == 403 and code(r) == "ADMIN_ONLY_ACTION", (method, path)
    stored = await row(db_session, peer.id)
    assert stored.status == "active" and stored.name != "Changed" and stored.token_version == 0
    # reading an org admin is fine
    assert (await client.get(f"{TEAM}/{peer.id}", headers=h)).status_code == 200


async def test_last_active_admin_guard_service_interface(db_session):
    org = await make_org(db_session)
    only = await make_user(db_session, "org_admin", org)
    hr = await make_user(db_session, "hr_user", org)
    await db_session.commit()
    guard = UserAdmin().last_active_admin_guard

    with pytest.raises(Exception) as exc:  # AppError
        await guard(db_session, org.id, only.id)
    assert exc.value.code == "LAST_ADMIN_PROTECTED"
    await guard(db_session, org.id, only.id, confirmed_by_sa=True)  # SA confirmation lifts the block
    await guard(db_session, org.id, hr.id)  # HR users are never the last admin

    await make_user(db_session, "org_admin", org)
    await db_session.commit()
    await guard(db_session, org.id, only.id)  # a second active admin exists

    inactive_peer_org = await make_org(db_session)
    solo = await make_user(db_session, "org_admin", inactive_peer_org)
    await make_user(db_session, "org_admin", inactive_peer_org, status="inactive")
    await db_session.commit()
    with pytest.raises(Exception) as exc:
        await guard(db_session, inactive_peer_org.id, solo.id)  # inactive admins do not count
    assert exc.value.code == "LAST_ADMIN_PROTECTED"


# --------------------------------------------------------------------------- roles of deactivated users


async def test_deactivate_marks_owned_roles_owner_inactive(client, db_session):
    org, oa, h = await world(db_session)
    hr = await make_user(db_session, "hr_user", org)
    r1 = await make_role(db_session, org, hr, title="Backend Engineer")
    r2 = await make_role(db_session, org, hr, title="Data Analyst", status="closed")
    await make_role(db_session, org, oa, title="Not mine")
    await db_session.commit()

    before = (await client.get(f"{TEAM}/{hr.id}", headers=h)).json()
    assert {r["title"]: r["owner_inactive"] for r in before["owned_roles"]} == {"Backend Engineer": False, "Data Analyst": False}

    r = await client.post(f"{TEAM}/{hr.id}/deactivate", json={}, headers=h)
    assert r.status_code == 200
    out = r.json()
    assert out["reassigned_roles"] == 0
    assert {x["id"] for x in out["owned_roles"]} == {str(r1.id), str(r2.id)}
    assert all(x["owner_inactive"] is True for x in out["owned_roles"])
    # roles stay visible and still point at the deactivated owner until reassigned
    owners = (await db_session.execute(text("select owner_id from open_roles where id in (:a, :b)"), {"a": r1.id, "b": r2.id})).all()
    assert {o[0] for o in owners} == {hr.id}
    assert (await client.get(f"{TEAM}/{hr.id}", headers=h)).json()["owned_roles"][0]["owner_inactive"] is True

    # reactivation clears the marker without touching the roles
    back = (await client.post(f"{TEAM}/{hr.id}/reactivate", headers=h)).json()
    assert all(x["owner_inactive"] is False for x in back["owned_roles"])


async def test_deactivate_reassigns_roles_when_asked(client, db_session):
    org, oa, h = await world(db_session)
    hr = await make_user(db_session, "hr_user", org)
    heir = await make_user(db_session, "hr_user", org)
    role = await make_role(db_session, org, hr)
    await db_session.commit()

    r = await client.post(f"{TEAM}/{hr.id}/deactivate", json={"reassign_roles_to": str(heir.id)}, headers=h)
    assert r.status_code == 200 and r.json()["reassigned_roles"] == 1 and r.json()["owned_roles"] == []
    assert await scalar(db_session, "select owner_id from open_roles where id = :i", i=role.id) == heir.id
    assert "ROLE_REASSIGNED" in await actions(db_session, org.id)

    # not to oneself, not to an inactive user
    solo = await make_user(db_session, "hr_user", org)
    gone = await make_user(db_session, "hr_user", org, status="inactive")
    await db_session.commit()
    for target in (solo.id, gone.id):
        bad = await client.post(f"{TEAM}/{solo.id}/deactivate", json={"reassign_roles_to": str(target)}, headers=h)
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "VALIDATION_ERROR"
    assert (await row(db_session, solo.id)).status == "active"


async def test_reassign_roles_to_user_in_other_org_is_404(client, db_session):
    """RF-4: a foreign id in the body is a 404 and nothing changes."""
    org, oa, h = await world(db_session)
    hr = await make_user(db_session, "hr_user", org)
    role = await make_role(db_session, org, hr)
    foreign = await make_user(db_session, "hr_user", await make_org(db_session))
    await db_session.commit()

    for target in (foreign.id, uuid.uuid4()):
        r = await client.post(f"{TEAM}/{hr.id}/deactivate", json={"reassign_roles_to": str(target)}, headers=h)
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert (await row(db_session, hr.id)).status == "active"
    assert await scalar(db_session, "select owner_id from open_roles where id = :i", i=role.id) == hr.id


# --------------------------------------------------------------------------- tenancy and permissions


TEAM_ROUTES = [
    ("GET", ""),
    ("POST", ""),
    ("GET", "/{u}"),
    ("PATCH", "/{u}"),
    ("POST", "/{u}/deactivate"),
    ("POST", "/{u}/reactivate"),
    ("POST", "/{u}/reset-password"),
    ("POST", "/{u}/unlock"),
    ("POST", "/{u}/sign-out-everywhere"),
]


@pytest.mark.parametrize(("method", "suffix"), TEAM_ROUTES)
async def test_team_permission_matrix(client, db_session, method, suffix):
    """Spec 4.3: only the organization admin manages HR users; SA, HR and anonymous are refused."""
    org = await make_org(db_session)
    oa = await make_user(db_session, "org_admin", org)
    hr = await make_user(db_session, "hr_user", org)
    target = await make_user(db_session, "hr_user", org, status="inactive" if suffix.endswith("reactivate") else "active")
    sa = await make_user(db_session, "super_admin")
    await db_session.commit()
    url = TEAM + suffix.format(u=target.id)
    body = {"name": "Matrix User", "email": f"m{uuid.uuid4().hex[:8]}@example.com"} if method == "POST" and suffix == "" else {}

    assert (await client.request(method, url, json=body)).status_code == 401
    for user in (hr, sa):
        r = await client.request(method, url, json=body, headers=await headers_for(db_session, user))
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", (user.role, method, url)
    r = await client.request(method, url, json=body, headers=await headers_for(db_session, oa))
    assert r.status_code in (200, 201), (method, url, r.text)


@pytest.mark.parametrize(("method", "suffix"), [r for r in TEAM_ROUTES if "{u}" in r[1]])
async def test_cross_org_user_ids_are_404(client, db_session, method, suffix):
    org, oa, h = await world(db_session)
    foreign = await make_user(db_session, "hr_user", await make_org(db_session))
    await db_session.commit()
    r = await client.request(method, TEAM + suffix.format(u=foreign.id), json={}, headers=h)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert (await row(db_session, foreign.id)).status == "active"
    assert (await client.get(TEAM, headers=h)).json()["total"] == 1  # only the caller is visible


async def test_org_disabled_blocks_team_routes(client, db_session):
    org, oa, h = await world(db_session)
    await db_session.execute(text("update organizations set status = 'disabled' where id = :o"), {"o": org.id})
    await db_session.commit()
    r = await client.get(TEAM, headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ORG_DISABLED"
