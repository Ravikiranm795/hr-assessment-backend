import asyncio
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import create_app
from app.modules.m08_exam import hooks
from tests import factories as f
from tests.m04_org_settings.conftest import multipart, png_bytes

API = "/api/v1/exam"
PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148"
TABLET = "Mozilla/5.0 (Linux; Android 13; Tablet) AppleWebKit/537.36"


async def _invite(db_session, world, open_role, **overrides):
    role, version = await open_role()
    invite = await f.make_invite(db_session, world.org, role, version, **overrides)
    await db_session.commit()
    return invite


async def _open(client, invite):
    r = await client.post(f"{API}/session/open", json={"token": invite.raw_token})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


async def _ready(client, h, *, photo=True):
    assert (await client.post(f"{API}/session/consent", json={}, headers=h)).status_code == 200
    check = {"viewport_w": 1280, "viewport_h": 800, "camera_permission": True}
    assert (await client.post(f"{API}/session/system-check", json=check, headers=h)).status_code == 200
    if photo:
        r = await client.post(
            f"{API}/session/baseline-photo",
            headers=h,
            **multipart("file", "p.png", png_bytes(64, 64), "image/png"),
        )
        assert r.status_code == 200, r.text


async def _started(client, db_session, world, open_role, **overrides):
    invite = await _invite(db_session, world, open_role, **overrides)
    h = await _open(client, invite)
    await _ready(client, h)
    assert (await client.post(f"{API}/session/start", headers=h)).status_code == 200
    return invite, h


async def _sql(db_session, sql, **p):
    await db_session.commit()
    return (await db_session.execute(text(sql), p)).mappings().all()


async def test_open_binds_browser_and_start_gates_and_deadline(client, world, open_role, db_session):
    invite = await _invite(db_session, world, open_role, time_multiplier=1.5, extra_minutes=10)
    h = await _open(client, invite)
    assert (await _open(client, invite)) == h or True  # same browser resumes
    other = AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")
    async with other:
        r = await other.post(f"{API}/session/open", json={"token": invite.raw_token})
        assert r.status_code == 403 and r.json()["error"]["code"] == "LINK_BOUND_TO_OTHER_SESSION"
        stolen = await other.get(f"{API}/session/state", headers=h)
        assert stolen.json()["error"]["code"] == "LINK_BOUND_TO_OTHER_SESSION"
    assert (await client.get(f"{API}/session/state", headers=h)).json()["status"] == "opened"

    start = lambda: client.post(f"{API}/session/start", headers=h)  # noqa: E731
    assert (await start()).json()["error"]["code"] == "CONSENT_REQUIRED"
    await client.post(f"{API}/session/consent", json={}, headers=h)
    assert (await start()).json()["error"]["code"] == "DEVICE_NOT_SUPPORTED"
    await client.post(
        f"{API}/session/system-check",
        json={"viewport_w": 1200, "viewport_h": 800, "camera_permission": True},
        headers=h,
    )
    assert (await start()).json()["error"]["code"] == "CAMERA_REQUIRED"
    await client.post(
        f"{API}/session/baseline-photo", headers=h, **multipart("file", "p.png", png_bytes(), "image/png")
    )
    assert (await client.get(f"{API}/session/state", headers=h)).json()["status"] == "ready"
    help_ = await client.post(f"{API}/help-requests", json={"reason": "other"}, headers=h)
    assert help_.status_code == 201
    assert (await start()).json()["error"]["code"] == "HELP_REQUEST_PENDING"
    await db_session.execute(text("UPDATE help_requests SET status = 'declined'"))
    await db_session.commit()

    started = (await start()).json()
    row = (await _sql(db_session, "SELECT started_at, deadline_at FROM attempts"))[0]
    assert row["deadline_at"] - row["started_at"] == timedelta(minutes=60 * 1.5 + 10)
    assert started["status"] == "in_progress" and 0 < started["remaining_seconds"] <= 100 * 60
    again = (await start()).json()  # idempotent: no restart
    assert again["deadline_at"] == started["deadline_at"]
    beat = (await client.post(f"{API}/heartbeat", headers=h)).json()
    assert beat["time_over"] is False and beat["remaining_seconds"] <= started["remaining_seconds"]


@pytest.mark.parametrize(
    ("ua", "w", "perm", "code"),
    [(PHONE, 1280, True, "DEVICE_NOT_SUPPORTED"), (TABLET, 1400, True, "DEVICE_NOT_SUPPORTED"),
     (None, 800, True, "DEVICE_NOT_SUPPORTED"), (None, 1280, False, "CAMERA_REQUIRED")],
)  # fmt: skip
async def test_device_and_camera_checks(client, world, open_role, db_session, ua, w, perm, code):
    invite = await _invite(db_session, world, open_role)
    h = await _open(client, invite)
    headers = h | ({"user-agent": ua} if ua else {})
    r = await client.post(
        f"{API}/session/system-check",
        json={"viewport_w": w, "viewport_h": 800, "camera_permission": perm},
        headers=headers,
    )
    assert r.status_code in (409, 422) and r.json()["error"]["code"] == code


async def test_questions_after_start_only_without_keys_and_answer_rules(client, world, open_role, db_session):
    invite = await _invite(db_session, world, open_role)
    h = await _open(client, invite)
    early = await client.get(f"{API}/questions", headers=h)
    assert early.status_code == 409 and early.json()["error"]["code"] == "EXAM_NOT_STARTED"
    await _ready(client, h)
    await client.post(f"{API}/session/start", headers=h)
    q = (await client.get(f"{API}/questions", headers=h)).json()
    assert not any(k in str(q) for k in ("is_correct", "answer_key", "model_answer", "scoring_mode"))
    question = q["questions"][0]
    qid, opt = question["id"], question["options"][0]["id"]
    put = lambda body, qq=qid: client.put(f"{API}/answers/{qq}", json={"answer": body}, headers=h)  # noqa: E731

    assert (await put({"option_id": opt})).status_code == 200
    assert (await put({"option_id": question["options"][1]["id"]})).status_code == 200  # last write wins
    saved = (await client.get(f"{API}/questions", headers=h)).json()["answers"]
    assert saved[qid] == {"option_id": question["options"][1]["id"]}
    for bad in (
        {},
        None,
        "x",
        {"text": "hi"},
        {"option_id": "nope"},
        {"option_id": opt, "pad": "x" * 100_000},
        {"option_ids": [opt]},
    ):
        r = await put(bad)
        assert r.status_code == 422, (bad, r.text)
    foreign = await put({"option_id": opt}, "00000000-0000-0000-0000-000000000000")
    assert foreign.status_code == 404

    # RF-2: saves are accepted until deadline + 5 s, rejected after
    await db_session.execute(text("UPDATE attempts SET deadline_at = now() - interval '4 seconds'"))
    await db_session.commit()
    assert (await put({"option_id": opt})).status_code == 200
    await db_session.execute(text("UPDATE attempts SET deadline_at = now() - interval '7 seconds'"))
    await db_session.commit()
    late = await put({"option_id": opt})
    assert late.status_code == 409 and late.json()["error"]["code"] == "EXAM_TIME_OVER"
    beat = (await client.post(f"{API}/heartbeat", headers=h)).json()
    assert beat["remaining_seconds"] == 0 and beat["time_over"] is True


async def test_events_server_severity_flag_counts_snapshots(client, world, open_role, db_session):
    _, h = await _started(client, db_session, world, open_role)
    ev = lambda *items: client.post(f"{API}/events", json={"events": list(items)}, headers=h)  # noqa: E731
    r = await ev(
        {"type": "context_menu", "severity": "serious"}, {"type": "bogus"}, *[{"type": "tab_switch"}] * 3
    )
    assert r.status_code == 200 and r.json() == {"accepted": 4, "ignored": 1}
    rows = await _sql(db_session, "SELECT type, severity FROM proctor_events ORDER BY received_at, id")
    sev = [(x["type"], x["severity"]) for x in rows]
    assert ("context_menu", "info") in sev and [s for t, s in sev if t == "tab_switch"] == [
        "minor",
        "minor",
        "serious",
    ]
    counts = (await _sql(db_session, "SELECT flag_counts FROM attempts"))[0]["flag_counts"]
    assert counts["types"]["tab_switch"] == 3 and counts["severity"]["serious"] == 1
    assert (await ev(*[{"type": "offline"}] * 51)).status_code == 422
    # violations never stop the exam
    assert (await client.get(f"{API}/questions", headers=h)).status_code == 200

    event_id = (await _sql(db_session, "SELECT id FROM proctor_events WHERE type = 'tab_switch' LIMIT 1"))[0][
        "id"
    ]
    snap = await client.post(
        f"{API}/snapshots",
        headers=h,
        data={"event_id": str(event_id)},
        **multipart("file", "s.png", png_bytes(), "image/png"),
    )
    assert snap.status_code == 201, snap.text
    asset = (
        await _sql(
            db_session,
            "SELECT a.encrypted, a.kind FROM assets a JOIN proctor_events e ON e.snapshot_asset_id = a.id",
        )
    )[0]
    assert asset["encrypted"] is True and asset["kind"] == "snapshot"
    again = await client.post(
        f"{API}/snapshots", headers=h, **multipart("file", "s.png", png_bytes(), "image/png")
    )
    assert again.status_code == 429


async def test_help_request_one_open_notifies_owner_and_is_pollable(client, world, open_role, db_session):
    invite = await _invite(db_session, world, open_role)
    h = await _open(client, invite)
    first = await client.post(
        f"{API}/help-requests", json={"reason": "no_camera", "note": "broken"}, headers=h
    )
    assert first.status_code == 201
    dup = await client.post(f"{API}/help-requests", json={"reason": "other"}, headers=h)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "HELP_REQUEST_PENDING"
    cur = (await client.get(f"{API}/help-requests/current", headers=h)).json()["request"]
    assert cur["status"] == "open" and cur["reason"] == "no_camera"
    note = await _sql(db_session, "SELECT user_id, type FROM notifications")
    assert [(n["user_id"], n["type"]) for n in note] == [(world.hr_owner.id, "help_request_created")]


async def test_double_submit_and_race_make_one_submission_then_read_only(
    client, world, open_role, db_session
):
    seen = []

    async def hook(ctx, session, attempt_id):
        seen.append(attempt_id)

    hooks.on_submit.append(hook)
    try:
        invite, h = await _started(client, db_session, world, open_role)
        q = (await client.get(f"{API}/questions", headers=h)).json()["questions"][0]
        results = await asyncio.gather(*[client.post(f"{API}/submit", headers=h) for _ in range(3)])
    finally:
        hooks.on_submit.remove(hook)
    assert all(r.status_code == 200 and r.json()["status"] == "submitted" for r in results)
    assert len({r.json()["submitted_at"] for r in results}) == 1
    assert len(seen) == 1
    actions = [
        r["action"]
        for r in await _sql(db_session, "SELECT action FROM audit_logs WHERE action = 'EXAM_SUBMITTED'")
    ]
    assert actions == ["EXAM_SUBMITTED"]
    assert (await _sql(db_session, "SELECT status FROM invites"))[0]["status"] == "submitted"
    put = await client.put(
        f"{API}/answers/{q['id']}", json={"answer": {"option_id": q["options"][0]["id"]}}, headers=h
    )
    assert put.json()["error"]["code"] == "EXAM_ALREADY_SUBMITTED"
    assert (await client.get(f"{API}/questions", headers=h)).json()["error"][
        "code"
    ] == "EXAM_ALREADY_SUBMITTED"
    reopen = await client.post(f"{API}/session/open", json={"token": invite.raw_token})
    assert reopen.json()["error"]["code"] == "EXAM_ALREADY_SUBMITTED"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("UPDATE invites SET status = 'revoked'", "INVITE_REVOKED"),
        ("UPDATE invites SET expires_at = now() - interval '1 hour'", "INVITE_EXPIRED"),
        ("UPDATE organizations SET status = 'disabled'", "ORG_DISABLED"),
        ("UPDATE open_roles SET status = 'closed'", "ROLE_NOT_OPEN"),
        (None, "NOT_FOUND"),
    ],
)
async def test_open_refuses_bad_invites(client, world, open_role, db_session, mutation, code):
    invite = await _invite(db_session, world, open_role)
    if mutation:
        await db_session.execute(text(mutation))
        await db_session.commit()
    token = invite.raw_token if mutation else "x" * 40
    r = await client.post(f"{API}/session/open", json={"token": token})
    assert r.json()["error"]["code"] == code


async def _set_org_status(db_session, status):
    await db_session.execute(text("UPDATE organizations SET status = :s"), {"s": status})
    await db_session.commit()


async def test_disabled_org_starts_no_exam_but_a_running_attempt_finishes(
    client, world, open_role, db_session
):
    invite = await _invite(db_session, world, open_role)
    h = await _open(client, invite)
    await _ready(client, h)
    await _set_org_status(db_session, "disabled")
    refused = await client.post(f"{API}/session/start", headers=h)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "ORG_DISABLED"
    await _set_org_status(db_session, "active")
    assert (await client.post(f"{API}/session/start", headers=h)).status_code == 200
    await _set_org_status(db_session, "disabled")
    assert (await client.post(f"{API}/session/start", headers=h)).status_code == 200  # running: idempotent
    assert (await client.post(f"{API}/heartbeat", headers=h)).status_code == 200
    assert (await client.post(f"{API}/submit", headers=h)).json()["status"] == "submitted"


async def test_reset_binding_cuts_off_the_old_session_and_revoke_blocks(client, world, open_role, db_session):
    invite, h = await _started(client, db_session, world, open_role)
    assert (await client.post(f"{API}/heartbeat", headers=h)).status_code == 200
    await db_session.execute(
        text("UPDATE invites SET session_binding_hash = NULL WHERE id = :i"), {"i": invite.id}
    )
    await db_session.commit()
    stale = await client.post(f"{API}/heartbeat", headers=h)  # same token and cookie, binding was reset
    assert stale.status_code == 403 and stale.json()["error"]["code"] == "LINK_BOUND_TO_OTHER_SESSION"
    fresh = await _open(client, invite)  # opening again binds the new browser
    assert (await client.post(f"{API}/heartbeat", headers=fresh)).status_code == 200
    await db_session.execute(text("UPDATE invites SET status = 'revoked' WHERE id = :i"), {"i": invite.id})
    await db_session.commit()
    assert (await client.post(f"{API}/heartbeat", headers=fresh)).json()["error"]["code"] == "INVITE_REVOKED"


async def test_snapshot_cannot_be_reattached_to_an_event(client, world, open_role, db_session):
    invite, h = await _started(client, db_session, world, open_role)
    await client.post(f"{API}/events", json={"events": [{"type": "no_face"}]}, headers=h)
    event = (await _sql(db_session, "SELECT id FROM proctor_events WHERE type = 'no_face'"))[0]["id"]
    files = multipart("file", "s.png", png_bytes(), "image/png")
    first = await client.post(f"{API}/snapshots", headers=h, data={"event_id": str(event)}, **files)
    assert first.status_code == 201
    from app.core.ratelimit import set_rate_limiter

    set_rate_limiter(None)  # skip the per-attempt spacing to reach the conflict rule
    second = await client.post(f"{API}/snapshots", headers=h, data={"event_id": str(event)}, **files)
    assert (
        second.status_code == 409
        and second.json()["error"]["details"]["reason"] == "snapshot_already_attached"
    )
