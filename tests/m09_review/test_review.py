import pytest
from sqlalchemy import text

from app.core.db import TenantContext, tenant_session
from app.core.storage import get_storage, set_storage
from app.core.storage.local import LocalStorage
from app.db import Asset, AttemptAnswer, ProctorEvent
from app.modules.m08_exam.service import ExamService
from app.modules.m09_review.scoring import ScoringService  # also registers the on_submit scoring hook
from tests import factories as f

API = "/api/v1"


async def _attempt(db_session, world, open_role, *, status="submitted", questions=2, correct=(True, False)):
    """A role with a published version of ``questions`` questions and one attempt with answers."""
    a = await f.make_assessment(db_session, world.org)
    version = await f.make_version(db_session, a, questions=questions)
    role = await f.make_role(db_session, world.org, world.hr_owner, version=version)
    invite = await f.make_invite(db_session, world.org, role, version)
    attempt = await f.make_attempt(db_session, invite, status=status)
    rows = (
        await db_session.execute(
            text(
                "SELECT q.id AS qid, q.position, "
                "(SELECT id FROM question_options o WHERE o.question_id = q.id AND o.is_correct) AS right_id, "
                "(SELECT id FROM question_options o WHERE o.question_id = q.id AND NOT o.is_correct) AS wrong_id "
                "FROM questions q WHERE q.version_id = :v ORDER BY q.position"
            ),
            {"v": version.id},
        )
    ).all()
    for row, ok in zip(rows, correct, strict=False):
        db_session.add(
            AttemptAnswer(
                attempt_id=attempt.id,
                question_id=row.qid,
                answer={"option_id": str(row.right_id if ok else row.wrong_id)},
            )
        )
    await db_session.commit()
    if status == "submitted":
        async with tenant_session(_ctx(world)) as session:
            await ScoringService.score_attempt(_ctx(world), session, attempt.id)
    return role, invite, attempt, [r.qid for r in rows]


def _ctx(world):
    return TenantContext(
        org_id=world.org.id,
        user_id=world.oa.id,
        role="org_admin",
        is_platform=False,
        request_id="t",
        ip=None,
        user_agent=None,
    )


async def _sql(db_session, sql, **p):
    await db_session.commit()
    return (await db_session.execute(text(sql), p)).mappings().all()


async def test_submit_hook_scores_attempt(world, open_role, db_session):
    _, _, attempt, _ = await _attempt(
        db_session, world, open_role, status="in_progress", questions=3, correct=(True, False)
    )
    async with tenant_session(_ctx(world)) as session:
        done, newly = await ExamService.finalize(_ctx(world), session, attempt.id, "candidate")
    assert newly
    row = (await _sql(db_session, "SELECT * FROM attempts WHERE id = :a", a=attempt.id))[0]
    assert float(row["final_score"]) == 1.0 and float(row["score_percent"]) == 33.33
    assert row["passed"] is False and row["scoring_status"] == "complete"  # pass mark 50; unanswered scores 0
    assert (
        await _sql(
            db_session, "SELECT count(*) AS n FROM attempt_answers WHERE attempt_id = :a", a=attempt.id
        )
    )[0]["n"] == 3


@pytest.mark.parametrize(
    ("who", "status"), [("oa", 200), ("hr_owner", 200), ("hr_other", 403), ("sa", 403), ("outsider", 404)]
)
async def test_report_permissions_audit_and_keys(client, world, open_role, db_session, who, status):
    _, _, attempt, _ = await _attempt(db_session, world, open_role)
    r = await client.get(f"{API}/attempts/{attempt.id}", headers=getattr(world.h, who))
    assert r.status_code == status, r.text
    views = await _sql(db_session, "SELECT count(*) AS n FROM audit_logs WHERE action = 'ATTEMPT_VIEWED'")
    assert views[0]["n"] == (1 if status == 200 else 0)
    if status == 200:
        body = r.json()
        assert (
            body["breakdown"][0]["options"][0]["is_correct"] in (True, False)
            and body["decision"] == "waiting"
        )


async def test_attempt_list_limited_for_non_owner(client, world, open_role, db_session):
    role, _, _, _ = await _attempt(db_session, world, open_role)
    full = (await client.get(f"{API}/roles/{role.id}/attempts", headers=world.h.hr_owner)).json()["items"][0]
    limited = (await client.get(f"{API}/roles/{role.id}/attempts", headers=world.h.hr_other)).json()["items"][
        0
    ]
    assert "score_percent" in full and set(limited) == {"id", "candidate_name", "status", "decision"}
    assert (await client.get(f"{API}/roles/{role.id}/attempts", headers=world.h.sa)).status_code == 403


async def test_decision_once_by_owner_override_by_admin_with_history(client, world, open_role, db_session):
    _, _, attempt, _ = await _attempt(db_session, world, open_role)
    _, _, draft, _ = await _attempt(db_session, world, open_role, status="in_progress")
    url = f"{API}/attempts/{attempt.id}/decision"
    body = {"decision": "approved", "note": "strong"}
    assert (await client.post(url, json=body, headers=world.h.oa)).status_code == 403  # OA: override only
    assert (await client.post(url, json=body, headers=world.h.hr_other)).status_code == 403
    early = await client.post(f"{API}/attempts/{draft.id}/decision", json=body, headers=world.h.hr_owner)
    assert early.status_code == 409
    assert (await client.post(url, json=body, headers=world.h.hr_owner)).status_code == 201
    again = await client.post(url, json={"decision": "rejected"}, headers=world.h.hr_owner)
    assert again.status_code == 409 and again.json()["error"]["code"] == "DECISION_ALREADY_MADE"
    ov = f"{url}/override"
    assert (
        await client.post(ov, json={"decision": "rejected", "reason": "x"}, headers=world.h.oa)
    ).status_code == 422
    assert (
        await client.post(
            ov, json={"decision": "rejected", "reason": "Conflict found"}, headers=world.h.hr_owner
        )
    ).status_code == 403
    assert (
        await client.post(ov, json={"decision": "rejected", "reason": "Conflict found"}, headers=world.h.oa)
    ).status_code == 201
    history = (
        await client.get(f"{API}/attempts/{attempt.id}/decision-history", headers=world.h.hr_owner)
    ).json()
    assert [(h["decision"], h["is_override"]) for h in history] == [("approved", False), ("rejected", True)]
    assert history[1]["previous_decision"] == "approved"
    actions = {r["action"] for r in await _sql(db_session, "SELECT action FROM audit_logs")}
    assert {"DECISION_MADE", "DECISION_OVERRIDDEN"} <= actions


async def test_score_override_dismiss_flag_and_extra_time(client, world, open_role, db_session):
    _, _, attempt, qids = await _attempt(db_session, world, open_role)
    live_role, _, live, _ = await _attempt(db_session, world, open_role, status="in_progress")
    event = ProctorEvent(attempt_id=attempt.id, org_id=world.org.id, type="tab_switch", severity="serious")
    db_session.add(event)
    await db_session.commit()
    h = world.h.hr_owner
    score = f"{API}/attempts/{attempt.id}/answers/{qids[1]}/score"
    assert (
        await client.patch(score, json={"marks": 5, "reason": "partial credit"}, headers=h)
    ).status_code == 422
    r = await client.patch(score, json={"marks": 1, "reason": "Reasoning was sound"}, headers=h)
    assert r.status_code == 200 and r.json()["score_percent"] == 100.0
    assert (
        await _sql(
            db_session,
            "SELECT scored_by FROM attempt_answers WHERE attempt_id = :a AND question_id = :q",
            a=attempt.id,
            q=qids[1],
        )
    )[0]["scored_by"] == "manual"
    dismiss = f"{API}/attempts/{attempt.id}/events/{event.id}/dismiss"
    assert (await client.post(dismiss, json={"note": ""}, headers=h)).status_code == 422
    assert (await client.post(dismiss, json={"note": "Candidate told us"}, headers=h)).json()[
        "dismissed"
    ] is True
    late = await client.post(
        f"{API}/attempts/{attempt.id}/extra-time", json={"minutes": 10, "reason": "tech issue"}, headers=h
    )
    assert late.status_code == 409
    ok = await client.post(
        f"{API}/attempts/{live.id}/extra-time",
        json={"minutes": 10, "reason": "tech issue"},
        headers=world.h.oa,
    )
    assert ok.status_code == 200
    moved = (
        await _sql(db_session, "SELECT deadline_at - started_at AS d FROM attempts WHERE id = :a", a=live.id)
    )[0]["d"]
    assert moved.total_seconds() == 70 * 60
    actions = {r["action"] for r in await _sql(db_session, "SELECT action FROM audit_logs")}
    assert {"SCORE_OVERRIDDEN", "FLAG_DISMISSED", "EXTRA_TIME_GRANTED"} <= actions
    assert (await client.post(f"{API}/attempts/{attempt.id}/rescore", headers=h)).json()["error"][
        "code"
    ] == "AI_NOT_CONFIGURED"


async def test_snapshot_url_signed_and_erase_candidate_data(client, world, open_role, db_session, tmp_path):
    set_storage(LocalStorage(root=str(tmp_path / "s"), base_url="http://api.test"))
    try:
        _, invite, attempt, _ = await _attempt(db_session, world, open_role)
        stored = await get_storage().put(
            f"{world.org.id}/snapshot/a.png", b"\x89PNG-bytes", "image/png", encrypt=True
        )
        asset = Asset(
            org_id=world.org.id,
            kind="snapshot",
            storage_key=stored.key,
            content_type="image/png",
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            encrypted=True,
        )
        db_session.add(asset)
        await db_session.flush()
        db_session.add(
            ProctorEvent(
                attempt_id=attempt.id,
                org_id=world.org.id,
                type="no_face",
                severity="minor",
                snapshot_asset_id=asset.id,
            )
        )
        await db_session.execute(
            text("UPDATE invites SET candidate_phone_enc = 'x' WHERE id = :i"), {"i": invite.id}
        )
        await db_session.commit()

        url = f"{API}/attempts/{attempt.id}/snapshots/{asset.id}/url"
        got = await client.get(url, headers=world.h.hr_owner)
        assert (
            got.status_code == 200
            and got.json()["expires_in_sec"] == 60
            and "/api/v1/files/" in got.json()["url"]
        )
        assert (await client.get(url, headers=world.h.sa)).status_code == 403
        other = f"{API}/attempts/{attempt.id}/snapshots/{invite.id}/url"
        assert (await client.get(other, headers=world.h.hr_owner)).status_code == 404

        erase = f"{API}/attempts/{attempt.id}/candidate-data"
        assert (await client.delete(erase, headers=world.h.hr_owner)).status_code == 403
        assert (await client.delete(erase, headers=world.h.sa)).status_code == 403
        done = await client.delete(erase, params={"include_identity": "true"}, headers=world.h.oa)
        assert done.status_code == 200 and done.json() == {
            "answers": 2,
            "events": 1,
            "snapshots": 1,
            "identity_erased": True,
        }
        left = (
            await _sql(
                db_session,
                "SELECT (SELECT count(*) FROM attempt_answers) AS a, (SELECT count(*) FROM proctor_events) AS e, (SELECT count(*) FROM assets WHERE kind = 'snapshot') AS s",
            )
        )[0]
        assert (left["a"], left["e"], left["s"]) == (0, 0, 0)
        inv = (
            await _sql(
                db_session,
                "SELECT candidate_name, candidate_phone_enc, candidate_email_enc FROM invites WHERE id = :i",
                i=invite.id,
            )
        )[0]
        assert inv["candidate_name"] == "Erased" and inv["candidate_phone_enc"] is None
        assert "CANDIDATE_DATA_DELETED" in {
            r["action"] for r in await _sql(db_session, "SELECT action FROM audit_logs")
        }
    finally:
        set_storage(None)
