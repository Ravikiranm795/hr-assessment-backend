import pytest

from app.modules.m06_roles import hooks
from app.modules.m06_roles.service import RoleService
from tests import factories as f

BASE = "/api/v1/roles"
BODY = {"title": "Backend Engineer", "department": "Platform", "vacancies": 2}


async def _role(client, world, who="hr_owner"):
    r = await client.post(BASE, json=BODY, headers=getattr(world.h, who))
    assert r.status_code == 201, r.text
    return r.json()


async def _versions(db_session, org, n=2):
    a = await f.make_assessment(db_session, org)
    out = [await f.make_version(db_session, a, version_no=i + 1) for i in range(n)]
    await db_session.commit()
    return out


async def test_lifecycle_link_replace_history_and_close_hook(client, world, db_session):
    role = await _role(client, world)
    url = f"{BASE}/{role['id']}"
    assert role["status"] == "draft" and role["owner_id"] == str(world.hr_owner.id)
    v1, v2 = await _versions(db_session, world.org)
    hr = world.h.hr_owner

    assert (
        await client.put(f"{url}/assessment", json={"assessment_version_id": str(v1.id)}, headers=hr)
    ).status_code == 200
    invite = await f.make_invite(db_session, world.org, await _db_role(db_session, role["id"]), v1)
    await db_session.commit()
    replaced = await client.put(f"{url}/assessment", json={"assessment_version_id": str(v2.id)}, headers=hr)
    assert replaced.json()["version_no"] == 2
    history = (await client.get(f"{url}/assessment/history", headers=hr)).json()["items"]
    assert [bool(h["unlinked_at"]) for h in history] == [False, True]
    assert str(invite.assessment_version_id) == str(v1.id)  # pending invites keep their version

    detail = (await client.get(url, headers=world.h.hr_other)).json()
    assert detail["progress"]["invites"]["invited"] == 1 and detail["assessment"][
        "assessment_version_id"
    ] == str(v2.id)
    assert (await client.post(f"{url}/close", headers=hr)).status_code == 409  # draft cannot close
    assert (await client.post(f"{url}/open", headers=hr)).json()["status"] == "open"

    closed = []

    async def hook(ctx, session, role_id):
        closed.append(role_id)

    hooks.on_role_close.append(hook)
    try:
        assert (await client.post(f"{url}/close", headers=hr)).json()["status"] == "closed"
    finally:
        hooks.on_role_close.remove(hook)
    assert [str(c) for c in closed] == [role["id"]]
    assert (await client.post(f"{url}/reopen", headers=hr)).json()["status"] == "open"
    moved = await client.post(
        f"{url}/reassign", json={"owner_id": str(world.hr_other.id)}, headers=world.h.oa
    )
    assert moved.json()["owner_id"] == str(world.hr_other.id)
    assert (await client.delete(f"{url}/assessment", headers=world.h.oa)).status_code == 204
    assert (await client.get(f"{url}/assessment", headers=hr)).json()["error"][
        "code"
    ] == "ROLE_HAS_NO_ASSESSMENT"


async def _db_role(db_session, role_id):
    from app.db import OpenRole

    return await db_session.get(OpenRole, role_id)


@pytest.mark.parametrize(
    ("who", "method", "suffix", "body", "status"),
    [
        ("sa", "POST", "", BODY, 403),
        ("hr_other", "POST", "", BODY, 201),
        ("hr_other", "PATCH", "/{id}", {"title": "X"}, 403),
        ("hr_other", "POST", "/{id}/open", None, 403),
        ("hr_other", "GET", "/{id}", None, 200),
        ("sa", "PATCH", "/{id}", {"title": "X"}, 403),
        ("sa", "GET", "/{id}", None, 422),  # read-only needs ?org_id
        ("oa", "PATCH", "/{id}", {"title": "X"}, 200),
        ("hr_owner", "PATCH", "/{id}", {"title": "X"}, 200),
        ("outsider", "GET", "/{id}", None, 404),
    ],
)
async def test_permission_matrix(client, world, who, method, suffix, body, status):
    role = await _role(client, world)
    resp = await client.request(
        method, BASE + suffix.format(id=role["id"]), json=body, headers=getattr(world.h, who)
    )
    assert resp.status_code == status, resp.text


async def test_rf4_foreign_version_and_owner_are_404_and_draft_not_linkable(client, world, db_session):
    role = await _role(client, world)
    url = f"{BASE}/{role['id']}"
    (foreign,) = await _versions(db_session, world.other_org, 1)
    mine = await f.make_assessment(db_session, world.org)
    draft = await f.make_version(db_session, mine, status="draft")
    await db_session.commit()

    def link(v):
        return client.put(
            f"{url}/assessment", json={"assessment_version_id": str(v)}, headers=world.h.hr_owner
        )

    assert (await link(foreign.id)).status_code == 404
    unpublished = await link(draft.id)
    assert unpublished.json()["error"]["code"] == "ASSESSMENT_NOT_PUBLISHED"
    outsider = world.outsider
    moved = await client.post(f"{url}/reassign", json={"owner_id": str(outsider.id)}, headers=world.h.oa)
    assert moved.status_code == 404


async def test_delete_only_draft_without_invites(client, world, db_session):
    clean = await _role(client, world)
    assert (await client.delete(f"{BASE}/{clean['id']}", headers=world.h.hr_owner)).status_code == 204
    assert (await client.get(f"{BASE}/{clean['id']}", headers=world.h.hr_owner)).status_code == 404

    with_invite = await _role(client, world)
    await f.make_invite(db_session, world.org, await _db_role(db_session, with_invite["id"]))
    await db_session.commit()
    r = await client.delete(f"{BASE}/{with_invite['id']}", headers=world.h.hr_owner)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROLE_HAS_INVITES"

    opened = await _role(client, world)
    await client.post(f"{BASE}/{opened['id']}/open", headers=world.h.hr_owner)
    assert (await client.delete(f"{BASE}/{opened['id']}", headers=world.h.hr_owner)).status_code == 409


async def test_get_open_linked_role_interface(client, world, db_session):
    role = await _role(client, world)
    rid = role["id"]
    from uuid import UUID

    from app.core.db import TenantContext, tenant_session

    ctx = TenantContext(org_id=world.org.id, user_id=world.oa.id, role="org_admin", is_platform=False,
                        request_id="t", ip=None, user_agent=None)  # fmt: skip

    async def check():
        async with tenant_session(ctx) as s:
            return await RoleService.get_open_linked_role(s, world.org.id, UUID(rid))

    with pytest.raises(Exception) as draft:
        await check()
    assert draft.value.code == "ROLE_NOT_OPEN"
    await client.post(f"{BASE}/{rid}/open", headers=world.h.hr_owner)
    with pytest.raises(Exception) as unlinked:
        await check()
    assert unlinked.value.code == "ROLE_HAS_NO_ASSESSMENT"
    (v,) = await _versions(db_session, world.org, 1)
    await client.put(
        f"{BASE}/{rid}/assessment", json={"assessment_version_id": str(v.id)}, headers=world.h.hr_owner
    )
    found, version_id = await check()
    assert str(found.id) == rid and version_id == v.id
