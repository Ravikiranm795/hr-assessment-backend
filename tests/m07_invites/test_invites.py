from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core import security
from app.core.email import MemoryEmailSender, set_email_sender
from app.db import HelpRequest, OrgEmailSettings
from tests import factories as f

CSV_HEAD = "name,email,phone\n"


def url(role):
    return f"/api/v1/roles/{role.id}/invites"


def body(name="Asha", email="asha@example.com", **extra):
    return {"candidate_name": name, "email": email, **extra}


async def _db(db_session, sql, **p):
    await db_session.commit()
    return (await db_session.execute(text(sql), p)).mappings().all()


async def test_create_returns_url_once_stores_hash_and_version(client, world, open_role, db_session):
    role, version = await open_role()
    r = await client.post(url(role), json=body(phone="+1 415 555 0100"), headers=world.h.hr_owner)
    assert r.status_code == 201, r.text
    data = r.json()
    token = data["exam_url"].rsplit("/exam/", 1)[1]
    row = (await _db(db_session, "SELECT * FROM invites WHERE id = :i", i=data["invite"]["id"]))[0]
    assert row["token_hash"] == security.hmac_hash(token) and token not in str(dict(row))
    assert row["assessment_version_id"] == version.id and row["candidate_phone_enc"] != "+1 415 555 0100"
    got = await client.get(f"/api/v1/invites/{data['invite']['id']}", headers=world.h.oa)
    assert "exam_url" not in got.text and token not in got.text
    assert got.json()["candidate_email"] == "asha@example.com"
    audit = await _db(db_session, "SELECT after::text AS a FROM audit_logs WHERE action = 'INVITE_CREATED'")
    assert "asha@example.com" not in audit[0]["a"] and token not in audit[0]["a"]


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [({"status": "draft"}, "ROLE_NOT_OPEN"), ({"linked": False}, "ROLE_HAS_NO_ASSESSMENT")],
)
async def test_requires_open_role_with_linked_assessment(client, world, open_role, kwargs, code):
    role, _ = await open_role(**kwargs)
    r = await client.post(url(role), json=body(), headers=world.h.hr_owner)
    assert r.status_code == 409 and r.json()["error"]["code"] == code


async def test_rf1_duplicate_detected_across_case_and_whitespace(client, world, open_role):
    role, _ = await open_role()
    h = world.h.hr_owner
    assert (await client.post(url(role), json=body(email="Bob@X.com "), headers=h)).status_code == 201
    dup = await client.post(url(role), json=body(email="bob@x.com"), headers=h)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "DUPLICATE_INVITE"
    again = await client.post(url(role), json=body(email=" BOB@x.COM", allow_duplicate=True), headers=h)
    assert again.status_code == 201 and again.json()["invite"]["duplicate_override"] is True
    other, _ = await open_role()
    assert (await client.post(url(other), json=body(email="bob@x.com"), headers=h)).status_code == 201


async def test_regenerate_extend_revoke_and_binding(client, world, open_role, db_session):
    role, _ = await open_role()
    h = world.h.hr_owner
    created = (await client.post(url(role), json=body(), headers=h)).json()
    iid = created["invite"]["id"]
    new = (await client.post(f"/api/v1/invites/{iid}/regenerate-link", headers=h)).json()
    assert new["exam_url"] != created["exam_url"]
    old_hash = security.hmac_hash(created["exam_url"].rsplit("/", 1)[1])
    assert not await _db(db_session, "SELECT 1 FROM invites WHERE token_hash = :h", h=old_hash)

    later = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    short = await client.post(
        f"/api/v1/invites/{iid}/extend", json={"expires_at": later, "reason": "x"}, headers=h
    )
    assert short.status_code == 422
    ok = await client.post(
        f"/api/v1/invites/{iid}/extend", json={"expires_at": later, "reason": "Candidate was ill"}, headers=h
    )
    assert ok.status_code == 200
    assert (
        await client.post(f"/api/v1/invites/{iid}/reset-binding", json={"reason": "new laptop"}, headers=h)
    ).status_code == 200
    assert (await client.post(f"/api/v1/invites/{iid}/revoke", headers=h)).json()["status"] == "revoked"
    assert (await client.post(f"/api/v1/invites/{iid}/regenerate-link", headers=h)).status_code == 409
    actions = {r["action"] for r in await _db(db_session, "SELECT action FROM audit_logs")}
    assert {"INVITE_REGENERATED", "INVITE_EXTENDED", "INVITE_BINDING_RESET", "INVITE_REVOKED"} <= actions


async def test_accommodations_and_help_requests(client, world, open_role, db_session):
    role, _ = await open_role()
    h = world.h.hr_owner
    iid = (await client.post(url(role), json=body(), headers=h)).json()["invite"]["id"]
    acc = await client.patch(
        f"/api/v1/invites/{iid}/accommodations",
        json={"time_multiplier": 1.5, "reason": "Dyslexia"},
        headers=h,
    )
    assert acc.json()["time_multiplier"] == 1.5 and acc.json()["accommodation_reason"] == "Dyslexia"
    assert (
        await client.patch(
            f"/api/v1/invites/{iid}/accommodations", json={"time_multiplier": 9, "reason": "abc"}, headers=h
        )
    ).status_code == 422

    from uuid import UUID

    reqs = [
        HelpRequest(org_id=world.org.id, invite_id=UUID(iid), reason=r) for r in ("no_camera", "extra_time")
    ]
    reqs[1].status = "declined"
    db_session.add_all(reqs)
    await db_session.commit()
    inbox = (await client.get("/api/v1/help-requests", params={"status": "open"}, headers=h)).json()
    assert inbox["total"] == 1
    assert (await client.get("/api/v1/help-requests", headers=world.h.hr_other)).json()[
        "total"
    ] == 0  # not their role
    hid = inbox["items"][0]["id"]
    assert (
        await client.post(f"/api/v1/help-requests/{hid}/approve", json={}, headers=world.h.hr_other)
    ).status_code == 403
    ok = await client.post(
        f"/api/v1/help-requests/{hid}/approve",
        json={"camera_exempt": True, "extra_minutes": 15, "note": "ok"},
        headers=h,
    )
    assert ok.json()["status"] == "approved"
    invite = (await _db(db_session, "SELECT camera_exempt, extra_minutes FROM invites WHERE id = :i", i=iid))[
        0
    ]
    assert invite["camera_exempt"] is True and invite["extra_minutes"] == 15
    assert (
        await client.post(f"/api/v1/help-requests/{hid}/decline", json={"note": "late"}, headers=h)
    ).status_code == 409
    actions = {r["action"] for r in await _db(db_session, "SELECT action FROM audit_logs")}
    assert {"ACCOMMODATION_SET", "HELP_REQUEST_APPROVED"} <= actions


async def test_send_email_requires_verified_smtp(client, world, open_role, db_session):
    role, _ = await open_role()
    h = world.h.hr_owner
    iid = (await client.post(url(role), json=body(), headers=h)).json()["invite"]["id"]
    r = await client.post(f"/api/v1/invites/{iid}/send-email", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "SMTP_NOT_CONFIGURED"
    db_session.add(
        OrgEmailSettings(
            org_id=world.org.id, host="smtp.example.com", port=587, security="starttls",
            sender_email="hr@example.com", enabled=True, verified_at=datetime.now(UTC),
        )
    )  # fmt: skip
    await db_session.commit()
    outbox = MemoryEmailSender()
    set_email_sender(outbox)
    assert (await client.post(f"/api/v1/invites/{iid}/send-email", headers=h)).json()["sent"] is True
    ((_, message),) = outbox.outbox
    assert message.to == "asha@example.com" and "/exam/" in message.text


async def test_rf5_csv_bulk_bom_bad_rows_duplicates_cap_and_formulas(client, world, open_role):
    role, _ = await open_role()
    h = world.h.hr_owner
    lines = [
        "Ann,ann@example.com,+1 415 555 0100",
        "=cmd|' /C calc'!A0,evil@example.com,",
        "Bad Email,not-an-email,",
        "Ann Again,ANN@example.com,",
        "Phone Bad,pb@example.com,abc",
        "Ok Two,two@example.com,",
        ",missing@example.com,",
    ]
    data = ("﻿" + CSV_HEAD + "\n".join(lines)).encode()
    r = await client.post(f"{url(role)}/bulk", files={"file": ("c.csv", data, "text/csv")}, headers=h)
    assert r.status_code == 201, r.text
    out = r.json()
    codes = [row.get("code") for row in out["rows"]]
    assert codes == [
        None,
        "formula_not_allowed",
        "email_invalid",
        "duplicate_in_file",
        "phone_invalid",
        None,
        "name_invalid",
    ]
    assert out["created"] == 2 and out["failed"] == 5
    big = (CSV_HEAD + "".join(f"U{i},u{i}@example.com,\n" for i in range(201))).encode()
    over = await client.post(f"{url(role)}/bulk", files={"file": ("c.csv", big, "text/csv")}, headers=h)
    assert over.status_code == 422 and over.json()["error"]["details"]["fields"][0]["code"] == "too_many_rows"
    bad = await client.post(
        f"{url(role)}/bulk", files={"file": ("c.csv", b"a,b\n1,2\n", "text/csv")}, headers=h
    )
    assert bad.status_code == 422


@pytest.mark.parametrize(
    ("who", "method", "path", "status"),
    [
        ("sa", "GET", "list", 403),
        ("hr_other", "POST", "list", 403),
        ("hr_other", "GET", "list", 200),
        ("oa", "POST", "list", 201),
        ("outsider", "GET", "list", 404),
    ],
)
async def test_permission_matrix_and_limited_view(client, world, open_role, who, method, path, status):
    role, _ = await open_role()
    await client.post(url(role), json=body(), headers=world.h.hr_owner)
    kwargs = {"json": body(email="n@example.com")} if method == "POST" else {}
    r = await client.request(method, url(role), headers=getattr(world.h, who), **kwargs)
    assert r.status_code == status, r.text
    if who == "hr_other" and method == "GET":
        assert set(r.json()["items"][0]) == {"id", "candidate_name", "status", "decision"}


async def test_close_revokes_unstarted_not_in_progress(client, world, open_role, db_session):
    role, version = await open_role()
    unstarted = await f.make_invite(db_session, world.org, role, version)
    started = await f.make_invite(db_session, world.org, role, version, status="started")
    await db_session.commit()
    r = await client.post(f"/api/v1/roles/{role.id}/close", headers=world.h.hr_owner)
    assert r.status_code == 200
    rows = {x["id"]: x["status"] for x in await _db(db_session, "SELECT id, status FROM invites")}
    assert rows[unstarted.id] == "revoked" and rows[started.id] == "started"
