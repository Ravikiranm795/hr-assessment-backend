from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.modules.m12_dashboard.gadgets import GADGETS
from tests import factories as f

API = "/api/v1"
ROLE_OF = {"sa": "super_admin", "oa": "org_admin", "hr_owner": "hr_user"}


async def _sql(db_session, sql, **p):
    await db_session.commit()
    return (await db_session.execute(text(sql), p)).mappings().all()


async def _work(db_session, world, owner, n_invites=2, **attempt):
    a = await f.make_assessment(db_session, world.org, owner)
    v = await f.make_version(db_session, a)
    role = await f.make_role(db_session, world.org, owner, version=v)
    invites = [await f.make_invite(db_session, world.org, role, v) for _ in range(n_invites)]
    if attempt:
        await f.make_attempt(db_session, invites[0], **attempt)
    await db_session.commit()
    return role, invites


async def test_each_catalog_key_has_a_gadget_that_runs_for_every_allowed_role(client, world, db_session):
    await _work(db_session, world, world.hr_owner, status="submitted", decision="approved", score_percent=70,
                flag_counts={"severity": {"serious": 1}})  # fmt: skip
    catalog_keys = {r["key"] for r in await _sql(db_session, "SELECT key FROM gadget_catalog")}
    assert catalog_keys == set(GADGETS) and len(catalog_keys) == 18
    seen = set()
    for who in ROLE_OF:
        headers = getattr(world.h, who)
        listed = (await client.get(f"{API}/dashboard/gadgets/catalog", headers=headers)).json()["items"]
        for item in listed:
            r = await client.get(f"{API}/dashboard/gadgets/{item['key']}/data", headers=headers)
            assert r.status_code == 200, (who, item["key"], r.text)
            seen.add(item["key"])
    assert seen == catalog_keys


async def test_gadget_not_for_role_absent_from_catalog_and_rejected(client, world):
    for who, hidden in [("hr_owner", "security_events"), ("oa", "orgs_overview"), ("sa", "open_roles")]:
        h = getattr(world.h, who)
        keys = {
            i["key"]
            for i in (await client.get(f"{API}/dashboard/gadgets/catalog", headers=h)).json()["items"]
        }
        assert hidden not in keys
        assert (await client.get(f"{API}/dashboard/gadgets/{hidden}/data", headers=h)).status_code == 403
    assert (await client.get(f"{API}/dashboard/gadgets/nope/data", headers=world.h.oa)).status_code == 404
    bad = await client.put(
        f"{API}/dashboard/layout", json={"gadgets": [{"key": "orgs_overview"}]}, headers=world.h.oa
    )
    assert bad.status_code == 422


async def test_layout_saved_per_user_with_defaults_and_validation(client, world):
    first = (await client.get(f"{API}/dashboard/layout", headers=world.h.oa)).json()
    assert first["is_default"] is True and first["items"][0]["key"] == "summary_counts"
    body = {"gadgets": [{"key": "open_roles", "size": "lg"}, {"key": "summary_counts", "enabled": False}]}
    saved = (await client.put(f"{API}/dashboard/layout", json=body, headers=world.h.oa)).json()
    assert saved["is_default"] is False and [(g["key"], g["size"], g["enabled"]) for g in saved["items"]] == [
        ("open_roles", "lg", True), ("summary_counts", "md", False)]  # fmt: skip
    assert (await client.get(f"{API}/dashboard/layout", headers=world.h.oa)).json() == saved
    assert (await client.get(f"{API}/dashboard/layout", headers=world.h.hr_owner)).json()[
        "is_default"
    ] is True
    dup = await client.put(
        f"{API}/dashboard/layout",
        json={"gadgets": [{"key": "open_roles"}, {"key": "open_roles"}]},
        headers=world.h.oa,
    )
    assert dup.status_code == 422
    assert (
        await client.put(
            f"{API}/dashboard/layout",
            json={"gadgets": [{"key": "open_roles", "size": "xl"}]},
            headers=world.h.oa,
        )
    ).status_code == 422


async def test_hr_gadget_counts_only_own_roles_and_oa_sees_org(client, world, db_session):
    await _work(db_session, world, world.hr_owner, n_invites=2)
    await _work(db_session, world, world.hr_other, n_invites=3)

    def get(who):
        return client.get(f"{API}/dashboard/gadgets/summary_counts/data", headers=getattr(world.h, who))

    assert (await get("hr_owner")).json()["data"]["invited"] == 2
    assert (await get("hr_other")).json()["data"]["invited"] == 3
    assert (await get("oa")).json()["data"]["invited"] == 5
    assert (await get("outsider")).json()["data"]["invited"] == 0


async def test_sa_gadgets_contain_no_candidate_fields(client, world, db_session):
    await _work(db_session, world, world.hr_owner, status="submitted", decision="approved")
    sa_keys = {
        i["key"]
        for i in (await client.get(f"{API}/dashboard/gadgets/catalog", headers=world.h.sa)).json()["items"]
    }
    assert sa_keys == {
        "summary_counts",
        "candidates_per_week",
        "decision_split",
        "orgs_overview",
        "active_misuse_flags",
        "platform_usage",
    }
    for key in sa_keys:
        body = (await client.get(f"{API}/dashboard/gadgets/{key}/data", headers=world.h.sa)).text.lower()
        assert not any(
            term in body for term in ("candidate_name", "candidate_email", "email", "answer", "phone")
        ), key
    totals = (await client.get(f"{API}/dashboard/gadgets/summary_counts/data", headers=world.h.sa)).json()[
        "data"
    ]
    assert totals["submitted"] == 1


async def test_needs_attention_lists_undecided_past_retention(client, world, db_session):
    old = datetime.now(UTC) - timedelta(days=100)
    await _work(db_session, world, world.hr_owner, status="submitted", submitted_at=old)  # decision waiting
    await _work(db_session, world, world.hr_owner, status="submitted", submitted_at=datetime.now(UTC),
                flag_counts={"severity": {"serious": 2}})  # fmt: skip
    data = (
        await client.get(f"{API}/dashboard/gadgets/needs_attention/data", headers=world.h.hr_owner)
    ).json()["data"]
    assert data["undecided_past_retention"] == 1 and data["unreviewed"] == 2 and data["flagged_attempts"] == 1
    assert data["pending_help_requests"] == 0 and data["expiring_links"] == 0


async def test_notifications_are_user_scoped_with_unread_count_and_read(client, world, db_session):
    await db_session.execute(
        text(
            "INSERT INTO notifications (id, org_id, user_id, type) VALUES "
            "(gen_random_uuid(), :o, :a, 'attempt_submitted'), (gen_random_uuid(), :o, :a, 'serious_flag'), "
            "(gen_random_uuid(), :o, :b, 'attempt_submitted')"
        ),
        {"o": world.org.id, "a": world.hr_owner.id, "b": world.hr_other.id},
    )
    await db_session.commit()
    mine = world.h.hr_owner
    assert (await client.get(f"{API}/notifications/unread-count", headers=mine)).json() == {"unread": 2}
    assert (await client.get(f"{API}/notifications/unread-count", headers=world.h.oa)).json() == {"unread": 0}
    listed = (await client.get(f"{API}/notifications", headers=mine)).json()
    assert listed["total"] == 2
    theirs = (await client.get(f"{API}/notifications", headers=world.h.hr_other)).json()["items"][0]["id"]
    assert (await client.post(f"{API}/notifications/{theirs}/read", headers=mine)).status_code == 404
    first = listed["items"][0]["id"]
    assert (await client.post(f"{API}/notifications/{first}/read", headers=mine)).status_code == 204
    assert (await client.get(f"{API}/notifications", params={"unread": "true"}, headers=mine)).json()[
        "total"
    ] == 1
    assert (await client.post(f"{API}/notifications/read-all", headers=mine)).json() == {"updated": 1}
    assert (await client.get(f"{API}/notifications/unread-count", headers=mine)).json() == {"unread": 0}
    assert (await client.get(f"{API}/notifications/unread-count", headers=world.h.hr_other)).json() == {
        "unread": 1
    }


async def test_flags_gate_gadgets_and_notifications(client, world, db_session):
    await db_session.execute(
        text(
            "INSERT INTO org_feature_flags (org_id, flag_key, enabled) VALUES (:o, 'dashboard_gadgets', false), (:o, 'dashboard', false)"
        ),
        {"o": world.org.id},
    )
    await db_session.commit()
    for path in ("/dashboard/layout", "/dashboard/gadgets/catalog", "/notifications"):
        r = await client.get(f"{API}{path}", headers=world.h.oa)
        assert r.status_code == 403 and r.json()["error"]["code"] == "FEATURE_DISABLED", path
    assert (await client.get(f"{API}/dashboard/layout")).status_code == 401
