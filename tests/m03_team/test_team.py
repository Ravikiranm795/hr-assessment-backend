"""M3 team management (OA): spec 6/M3, 4.3, A-02, A-04, A-07, D-05, RF-4."""

import uuid

import pytest
from sqlalchemy import text

from app.core import security
from app.modules.m03_team.service import UserAdmin
from tests.factories import make_org, make_role, make_user

TEAM = "/api/v1/team/users"


async def user_row(session, user_id):
    sql = "select status, token_version, role, name, password_hash, must_change_password from users where id = :i"
    return (await session.execute(text(sql), {"i": user_id})).one()


async def test_create_list_update_and_account_controls(client, db_session, world, audit_actions, scalar):
    h, org = world.h, world.org
    r = await client.post(TEAM, json={"name": " Hana HR ", "email": "Hana@Example.com "}, headers=h)
    assert r.status_code == 201, r.text
    user, temp = r.json()["user"], r.json()["temporary_password"]
    uid = uuid.UUID(user["id"])
    assert (user["role"], user["email"], user["name"], user["must_change_password"]) == (
        "hr_user",
        "hana@example.com",
        "Hana HR",
        True,
    )
    row = await user_row(db_session, uid)
    assert row.password_hash.startswith("$argon2id$") and security.verify_password(row.password_hash, temp)

    # shown once: not in reads, not in audit
    assert temp not in (await client.get(TEAM, headers=h)).text + (await client.get(f"{TEAM}/{uid}", headers=h)).text
    assert await scalar("select count(*) from audit_logs where coalesce(after::text,'') like :p", p=f"%{temp}%") == 0
    dup = await client.post(TEAM, json={"name": "Dup", "email": "HANA@example.com"}, headers=h)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "CONFLICT"

    assert (await client.get(TEAM, params={"q": "hana"}, headers=h)).json()["total"] == 1
    assert (await client.get(TEAM, params={"status": "inactive"}, headers=h)).json()["total"] == 0
    assert (await client.get(TEAM, params={"page_size": 1000}, headers=h)).status_code == 422

    # email editable only before the first login
    r = await client.patch(f"{TEAM}/{uid}", json={"name": "Hana R", "email": "new@example.com"}, headers=h)
    assert r.status_code == 200 and r.json()["email"] == "new@example.com"
    await db_session.execute(text("update users set last_login_at = now() where id = :i"), {"i": uid})
    await db_session.commit()
    r = await client.patch(f"{TEAM}/{uid}", json={"email": "again@example.com"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"][0]["code"] == "not_editable_after_login"

    # reset-password and sign-out-everywhere kill tokens; unlock clears the lockout
    await db_session.execute(
        text("update users set failed_login_count = 5, locked_until = now() + interval '15 min' where id = :i"),
        {"i": uid},
    )
    await db_session.commit()
    assert (await client.post(f"{TEAM}/{uid}/unlock", headers=h)).json()["is_locked"] is False
    r = await client.post(f"{TEAM}/{uid}/reset-password", headers=h)
    new_temp = r.json()["temporary_password"]
    row = await user_row(db_session, uid)
    assert security.verify_password(row.password_hash, new_temp) and row.must_change_password and row.token_version == 1
    await client.post(f"{TEAM}/{uid}/sign-out-everywhere", headers=h)
    assert (await user_row(db_session, uid)).token_version == 2
    names = await audit_actions(org.id)
    for expected in (
        "USER_CREATED",
        "USER_UPDATED",
        "AUTH_ACCOUNT_UNLOCKED",
        "AUTH_TEMP_PASSWORD_ISSUED",
        "AUTH_SIGNED_OUT_EVERYWHERE",
    ):
        assert expected in names
    assert new_temp not in str(await scalar("select string_agg(coalesce(after::text,''), ' ') from audit_logs"))


async def test_deactivate_reactivate_never_deletes_and_kills_tokens(client, db_session, world, hdr, scalar):
    hr = await make_user(db_session, "hr_user", world.org)
    hr_h = await hdr(hr)
    r = await client.post(f"{TEAM}/{hr.id}/deactivate", json={"reason": "left"}, headers=world.h)
    assert r.status_code == 200 and r.json()["status"] == "inactive"
    row = await user_row(db_session, hr.id)
    assert row.status == "inactive" and row.token_version == 1  # never deleted, tokens die
    assert (await client.get(TEAM, headers=hr_h)).status_code == 401
    assert await scalar("select reason from audit_logs where action = 'USER_DEACTIVATED'") == "left"
    assert (await client.post(f"{TEAM}/{hr.id}/deactivate", json={}, headers=world.h)).status_code == 409
    r = await client.post(f"{TEAM}/{hr.id}/reactivate", headers=world.h)
    assert r.status_code == 200 and r.json()["status"] == "active"


async def test_oa_cannot_create_or_modify_oa_ADMIN_ONLY_ACTION(client, db_session, world):
    peer = await make_user(db_session, "org_admin", world.org)
    hr = await make_user(db_session, "hr_user", world.org)
    await db_session.commit()
    h = world.h

    def denied(r):
        return r.status_code == 403 and r.json()["error"]["code"] == "ADMIN_ONLY_ACTION"

    for role in ("org_admin", "super_admin"):
        assert denied(await client.post(TEAM, json={"name": "B", "email": f"{role}@example.com", "role": role}, headers=h))
    assert await db_session.scalar(text("select count(*) from users where email like '%_admin@example.com'")) == 0
    assert denied(await client.patch(f"{TEAM}/{hr.id}", json={"role": "org_admin"}, headers=h))  # promotion
    assert (await user_row(db_session, hr.id)).role == "hr_user"
    for target in (peer, world.oa):  # no modification of any admin, including oneself
        calls = [
            ("PATCH", "", {"name": "Changed"}),
            ("POST", "/deactivate", {}),
            ("POST", "/reactivate", None),
            ("POST", "/reset-password", None),
            ("POST", "/unlock", None),
            ("POST", "/sign-out-everywhere", None),
        ]
        for method, suffix, body in calls:
            r = await client.request(method, f"{TEAM}/{target.id}{suffix}", headers=h, **({"json": body} if body is not None else {}))
            assert denied(r), (method, suffix)
    row = await user_row(db_session, peer.id)
    assert row.status == "active" and row.name != "Changed" and row.token_version == 0
    assert (await client.get(f"{TEAM}/{peer.id}", headers=h)).status_code == 200  # reading is fine


async def test_last_active_admin_guard_service_interface(db_session):
    org = await make_org(db_session)
    only = await make_user(db_session, "org_admin", org)
    hr = await make_user(db_session, "hr_user", org)
    await make_user(db_session, "org_admin", org, status="inactive")  # inactive admins do not count
    await db_session.commit()
    guard = UserAdmin().last_active_admin_guard
    with pytest.raises(Exception) as exc:
        await guard(db_session, org.id, only.id)
    assert exc.value.code == "LAST_ADMIN_PROTECTED"
    await guard(db_session, org.id, only.id, confirmed_by_sa=True)
    await guard(db_session, org.id, hr.id)
    await make_user(db_session, "org_admin", org)
    await db_session.commit()
    await guard(db_session, org.id, only.id)


async def test_deactivate_marks_owned_roles_owner_inactive(client, db_session, world):
    hr = await make_user(db_session, "hr_user", world.org)
    r1 = await make_role(db_session, world.org, hr, title="Backend")
    r2 = await make_role(db_session, world.org, hr, title="Analyst", status="closed")
    await make_role(db_session, world.org, world.oa, title="Not mine")
    await db_session.commit()
    h = world.h
    before = (await client.get(f"{TEAM}/{hr.id}", headers=h)).json()
    assert [r["owner_inactive"] for r in before["owned_roles"]] == [False, False]

    out = (await client.post(f"{TEAM}/{hr.id}/deactivate", json={}, headers=h)).json()
    assert out["reassigned_roles"] == 0 and {x["id"] for x in out["owned_roles"]} == {str(r1.id), str(r2.id)}
    assert all(x["owner_inactive"] for x in out["owned_roles"])  # still owned, marked until reassigned
    owners = (await db_session.execute(text("select owner_id from open_roles where id in (:a, :b)"), {"a": r1.id, "b": r2.id})).all()
    assert {o[0] for o in owners} == {hr.id}
    back = (await client.post(f"{TEAM}/{hr.id}/reactivate", headers=h)).json()
    assert not any(x["owner_inactive"] for x in back["owned_roles"])


async def test_deactivate_reassigns_roles_and_validates_new_owner(client, db_session, world, audit_actions, scalar):
    hr = await make_user(db_session, "hr_user", world.org)
    heir = await make_user(db_session, "hr_user", world.org)
    gone = await make_user(db_session, "hr_user", world.org, status="inactive")
    role = await make_role(db_session, world.org, hr)
    await db_session.commit()
    for target in (hr.id, gone.id):  # not to oneself, not to an inactive user
        r = await client.post(f"{TEAM}/{hr.id}/deactivate", json={"reassign_roles_to": str(target)}, headers=world.h)
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = await client.post(f"{TEAM}/{hr.id}/deactivate", json={"reassign_roles_to": str(heir.id)}, headers=world.h)
    assert r.status_code == 200 and r.json()["reassigned_roles"] == 1 and r.json()["owned_roles"] == []
    assert await scalar("select owner_id from open_roles where id = :i", i=role.id) == heir.id
    assert "ROLE_REASSIGNED" in await audit_actions(world.org.id)


async def test_reassign_roles_to_user_in_other_org_is_404(client, db_session, world):
    """RF-4: a foreign id in the body is a 404 and nothing changes."""
    hr = await make_user(db_session, "hr_user", world.org)
    role = await make_role(db_session, world.org, hr)
    foreign = await make_user(db_session, "hr_user", await make_org(db_session))
    await db_session.commit()
    for target in (foreign.id, uuid.uuid4()):
        r = await client.post(f"{TEAM}/{hr.id}/deactivate", json={"reassign_roles_to": str(target)}, headers=world.h)
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert (await user_row(db_session, hr.id)).status == "active"
    assert await db_session.scalar(text("select owner_id from open_roles where id = :i"), {"i": role.id}) == hr.id


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
async def test_team_permission_matrix(client, db_session, hdr, method, suffix):
    """Spec 4.3: only the org admin manages HR users; SA, HR and anonymous are refused. Foreign ids are 404."""
    org = await make_org(db_session)
    oa = await make_user(db_session, "org_admin", org)
    hr = await make_user(db_session, "hr_user", org)
    sa = await make_user(db_session, "super_admin")
    status = "inactive" if suffix.endswith("reactivate") else "active"
    target = await make_user(db_session, "hr_user", org, status=status)
    foreign = await make_user(db_session, "hr_user", await make_org(db_session), status=status)
    url = TEAM + suffix.format(u=target.id)
    body = {"name": "Matrix", "email": f"m{uuid.uuid4().hex[:8]}@example.com"} if (method, suffix) == ("POST", "") else {}

    assert (await client.request(method, url, json=body)).status_code == 401
    for user in (hr, sa):
        r = await client.request(method, url, json=body, headers=await hdr(user))
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", (user.role, method, url)
    oa_h = await hdr(oa)
    r = await client.request(method, url, json=body, headers=oa_h)
    assert r.status_code in (200, 201), (method, url, r.text)
    if "{u}" in suffix:
        r = await client.request(method, TEAM + suffix.format(u=foreign.id), json={}, headers=oa_h)
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
        assert (await user_row(db_session, foreign.id)).status == status
