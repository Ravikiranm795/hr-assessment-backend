import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from app.core.db import TenantContext, system_context, tenant_session
from app.core.deps import ExamContext
from app.core.storage import get_storage, set_storage
from app.core.storage.local import LocalStorage
from app.db import AttemptAnswer, ProctorEvent
from app.jobs import (
    JOBS,
    audit_partitions,
    expire_invites,
    notification_cleanup,
    report_cleanup,
    retention_purge,
    retry_ai_scoring,
    timeout_attempts,
    version_purge,
)
from app.modules.m08_exam import hooks as exam_hooks
from app.modules.m08_exam.schemas import EventsIn
from app.modules.m08_exam.service import ExamService
from app.modules.m09_review import hooks as review_hooks
from tests import factories as f

NOW = datetime.now(UTC)


async def _sql(db_session, sql, **p):
    await db_session.commit()
    return (await db_session.execute(text(sql), p)).mappings().all()


@pytest.fixture
async def scenario(db_session, world, tmp_path):
    """One of everything a job can act on, plus rows each job must leave alone."""
    set_storage(LocalStorage(root=str(tmp_path / "s"), base_url="http://api.test"))
    org, s = world.org, {}
    a = await f.make_assessment(db_session, org)
    v = await f.make_version(db_session, a)
    role = await f.make_role(db_session, org, world.hr_owner, version=v)

    async def invite(**kw):
        return await f.make_invite(db_session, org, role, v, **kw)

    s["expired_invite"] = await invite(expires_at=NOW - timedelta(hours=1))
    s["soon_invite"] = await invite(expires_at=NOW + timedelta(hours=10))
    s["far_invite"] = await invite(expires_at=NOW + timedelta(days=5))

    s["overdue"] = await f.make_attempt(
        db_session, await invite(), status="in_progress", started_at=NOW - timedelta(hours=2),
        deadline_at=NOW - timedelta(minutes=1),
    )  # fmt: skip
    s["running"] = await f.make_attempt(db_session, await invite(), status="in_progress")

    old_invite = await invite()
    s["old"] = await f.make_attempt(
        db_session, old_invite, status="submitted", decision="approved", decided_at=NOW - timedelta(days=100),
        flag_counts={"types": {"tab_switch": 2}}, scoring_status="complete", score_percent=80,
    )  # fmt: skip
    s["recent"] = await f.make_attempt(
        db_session, await invite(), status="submitted", decision="approved",
        decided_at=NOW - timedelta(days=10), scoring_status="complete",
    )  # fmt: skip
    s["undecided"] = await f.make_attempt(
        db_session, await invite(), status="submitted", submitted_at=NOW - timedelta(days=200),
        scoring_status="complete",
    )  # fmt: skip
    s["stale_pending"] = await f.make_attempt(
        db_session, await invite(), status="submitted", scoring_status="pending",
        submitted_at=NOW - timedelta(minutes=20),
    )  # fmt: skip
    qid = (
        await db_session.execute(text("SELECT id FROM questions WHERE version_id = :v"), {"v": v.id})
    ).scalar_one()
    db_session.add(AttemptAnswer(attempt_id=s["old"].id, question_id=qid, answer={"option_id": "x"}))
    db_session.add(ProctorEvent(attempt_id=s["old"].id, org_id=org.id, type="tab_switch", severity="minor"))

    for i in range(2, 5):  # four published versions of one assessment, none in use
        await f.make_version(db_session, a, version_no=i)
    await db_session.execute(
        text(
            "INSERT INTO notifications (id, org_id, user_id, type, read_at) VALUES "
            "(gen_random_uuid(), :o, :u, 'attempt_submitted', now() - interval '61 days'), "
            "(gen_random_uuid(), :o, :u, 'attempt_submitted', now() - interval '5 days'), "
            "(gen_random_uuid(), :o, :u, 'attempt_submitted', NULL)"
        ),
        {"o": org.id, "u": world.hr_owner.id},
    )
    stored = await get_storage().put(f"{org.id}/report/r.csv", b"a,b", "text/csv")
    asset_id = (
        await db_session.execute(
            text(
                "INSERT INTO assets (id, org_id, kind, storage_key, content_type, size_bytes, sha256, encrypted) "
                "VALUES (gen_random_uuid(), :o, 'doc_pdf', :k, 'text/csv', 3, :h, false) RETURNING id"
            ),
            {"o": org.id, "k": stored.key, "h": stored.sha256},
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "INSERT INTO report_requests (id, org_id, user_id, source, status, asset_id, created_at) "
            "VALUES (gen_random_uuid(), :o, :u, 'candidates', 'generated', :a, now() - interval '8 days')"
        ),
        {"o": org.id, "u": world.oa.id, "a": asset_id},
    )
    await db_session.commit()
    s["org"], s["role"], s["assessment"], s["storage_key"] = org, role, a, stored.key
    yield s
    set_storage(None)


async def test_timeout_submits_past_deadline_plus_grace_with_reason_timer_and_scores(scenario, db_session):
    result = await timeout_attempts.run()
    assert result == {"due": 1, "submitted": 1, "failed": 0}
    rows = {
        r["id"]: r
        for r in await _sql(db_session, "SELECT id, status, submit_reason, scoring_status FROM attempts")
    }
    over, running = rows[scenario["overdue"].id], rows[scenario["running"].id]
    assert (over["status"], over["submit_reason"], over["scoring_status"]) == (
        "submitted",
        "timer",
        "complete",
    )
    assert running["status"] == "in_progress"
    assert (
        await _sql(db_session, "SELECT status FROM invites WHERE id = :i", i=scenario["overdue"].invite_id)
    )[0]["status"] == "submitted"
    audit = await _sql(db_session, "SELECT actor_type FROM audit_logs WHERE action = 'EXAM_SUBMITTED'")
    assert [a["actor_type"] for a in audit] == ["system"]


async def test_rf2_sweeper_and_candidate_submit_race_produce_one_submission(scenario, db_session):
    seen = []

    async def hook(ctx, session, attempt_id):
        seen.append(attempt_id)

    exam_hooks.on_submit.append(hook)
    try:
        attempt = scenario["overdue"]
        ctx = system_context("candidate", scenario["org"].id)

        async def candidate():
            async with tenant_session(ctx) as session:
                return await ExamService.finalize(ctx, session, attempt.id, "candidate")

        results = await asyncio.gather(timeout_attempts.run(), candidate(), timeout_attempts.run())
    finally:
        exam_hooks.on_submit.remove(hook)
    assert seen == [attempt.id]
    assert len(await _sql(db_session, "SELECT 1 FROM audit_logs WHERE action = 'EXAM_SUBMITTED'")) == 1
    assert sum(r["submitted"] for r in results if isinstance(r, dict)) + results[1][1] == 1


async def test_expire_invites_marks_overdue_and_warns_24h_ahead_once(scenario, db_session):
    assert await expire_invites.run() == {"expired": 1, "warned": 1}
    status = {r["id"]: r["status"] for r in await _sql(db_session, "SELECT id, status FROM invites")}
    assert (
        status[scenario["expired_invite"].id] == "expired" and status[scenario["soon_invite"].id] == "invited"
    )
    note = await _sql(
        db_session, "SELECT user_id, entity_id FROM notifications WHERE type = 'invite_expiring'"
    )
    assert [(n["user_id"], n["entity_id"]) for n in note] == [
        (scenario["role"].owner_id, scenario["soon_invite"].id)
    ]


async def test_retention_purge_deletes_detail_keeps_summary_never_purges_undecided(scenario, db_session):
    assert await retention_purge.run() == {"due": 1, "purged": 1, "failed": 0}
    rows = {
        r["id"]: r
        for r in await _sql(db_session, "SELECT id, purged_at, flag_counts, score_percent FROM attempts")
    }
    old = rows[scenario["old"].id]
    assert (
        old["purged_at"] is not None
        and old["flag_counts"] == {"types": {"tab_switch": 2}}
        and float(old["score_percent"]) == 80
    )
    assert (
        rows[scenario["recent"].id]["purged_at"] is None
        and rows[scenario["undecided"].id]["purged_at"] is None
    )
    left = (
        await _sql(
            db_session,
            "SELECT (SELECT count(*) FROM attempt_answers) AS a, (SELECT count(*) FROM proctor_events) AS e",
        )
    )[0]
    assert (left["a"], left["e"]) == (0, 0)
    actions = {r["action"] for r in await _sql(db_session, "SELECT action FROM audit_logs")}
    assert {"RETENTION_PURGE_RUN", "CANDIDATE_DATA_DELETED"} <= actions


async def test_retry_ai_scoring_retries_fresh_and_marks_stale_needs_manual(scenario, db_session):
    fresh = await f.make_attempt(
        db_session, await f.make_invite(db_session, scenario["org"], scenario["role"]), status="submitted",
        scoring_status="pending", submitted_at=NOW - timedelta(minutes=1),
    )  # fmt: skip
    await db_session.commit()
    calls = []

    async def hook(ctx, session, attempt_id):
        calls.append(attempt_id)

    review_hooks.ai_scoring_hooks.append(hook)
    try:
        result = await retry_ai_scoring.run()
    finally:
        review_hooks.ai_scoring_hooks.remove(hook)
    assert result == {"pending": 2, "retried": 1, "needs_manual": 1, "failed": 0} and calls == [fresh.id]
    status = {
        r["id"]: r["scoring_status"]
        for r in await _sql(db_session, "SELECT id, scoring_status FROM attempts")
    }
    assert status[scenario["stale_pending"].id] == "needs_manual" and status[fresh.id] == "pending"
    note = await _sql(db_session, "SELECT entity_id FROM notifications WHERE type = 'scoring_needs_manual'")
    assert [n["entity_id"] for n in note] == [scenario["stale_pending"].id]


async def test_audit_partitions_create_next_drop_older_than_13_months_and_log(scenario, db_session):
    await db_session.execute(text("SELECT audit_ensure_partitions(1, DATE '2023-01-01')"))
    await db_session.commit()
    first = await audit_partitions.run(now=datetime(2027, 3, 1, tzinfo=UTC))
    assert first["dropped"] >= 1 and first["created"] >= 1
    names = {
        r["relname"]
        for r in await _sql(
            db_session, "SELECT c.relname FROM pg_class c WHERE c.relname ~ '^audit_logs_y2023'"
        )
    }
    assert names == set()
    assert "AUDIT_PURGE_RUN" in {r["action"] for r in await _sql(db_session, "SELECT action FROM audit_logs")}
    assert await audit_partitions.run(now=datetime(2027, 3, 1, tzinfo=UTC)) == {"created": 0, "dropped": 0}


async def test_version_purge_notification_and_report_cleanup(scenario, db_session):
    assert (await version_purge.run())["purged"] == 1
    left = await _sql(
        db_session,
        "SELECT version_no FROM assessment_versions WHERE assessment_id = :a ORDER BY 1",
        a=scenario["assessment"].id,
    )
    assert [r["version_no"] for r in left] == [1, 3, 4]  # v1 is linked to an open role, so v2 goes
    assert await notification_cleanup.run() == {"removed": 1}
    assert len(await _sql(db_session, "SELECT 1 FROM notifications WHERE type = 'attempt_submitted'")) == 2
    assert await report_cleanup.run() == {"removed": 1}
    assert not await _sql(
        db_session, "SELECT 1 FROM assets WHERE storage_key = :k", k=scenario["storage_key"]
    )
    assert (await _sql(db_session, "SELECT asset_id FROM report_requests"))[0]["asset_id"] is None


async def test_jobs_idempotent_second_run_noop(scenario):
    modules = [expire_invites, timeout_attempts, retry_ai_scoring, retention_purge, version_purge,
               audit_partitions, notification_cleanup, report_cleanup]  # fmt: skip
    assert set(JOBS) == {m.NAME for m in modules}
    for module in modules:
        await module.run()
    for module in modules:
        assert set((await module.run()).values()) == {0}, module.NAME


async def test_serious_flag_notification_debounced_10_min(world, db_session):
    a = await f.make_assessment(db_session, world.org)
    v = await f.make_version(db_session, a)
    role = await f.make_role(db_session, world.org, world.hr_owner, version=v)
    invite = await f.make_invite(db_session, world.org, role, v, session_binding_hash="bind")
    attempt = await f.make_attempt(db_session, invite)
    await db_session.commit()
    ctx = TenantContext(org_id=world.org.id, user_id=None, role="candidate", is_platform=False, request_id="t",
                        ip=None, user_agent=None, actor_type="candidate")  # fmt: skip
    exam = ExamContext(ctx=ctx, attempt_id=attempt.id, invite_id=invite.id, binding="bind")

    async def ingest(kind):
        async with tenant_session(ctx) as session:
            await ExamService.ingest_events(session, exam, EventsIn(events=[{"type": kind}]))

    count = lambda: _sql(db_session, "SELECT count(*) AS n FROM notifications WHERE type = 'serious_flag'")  # noqa: E731
    await ingest("multiple_faces")
    await ingest("camera_lost")  # still inside the window
    await ingest("offline")  # not serious
    assert (await count())[0]["n"] == 1
    await db_session.execute(text("UPDATE notifications SET created_at = now() - interval '11 minutes'"))
    await db_session.commit()
    await ingest("multiple_faces")
    assert (await count())[0]["n"] == 2


async def test_run_job_cli_dispatch(db_engine):
    from app.cli import cli

    runner = CliRunner()
    listed = await asyncio.to_thread(runner.invoke, cli, ["list-jobs"])
    assert listed.exit_code == 0 and "timeout_attempts" in listed.output.split()
    ran = await asyncio.to_thread(runner.invoke, cli, ["run-job", "notification_cleanup"])
    assert ran.exit_code == 0 and json.loads(ran.output.strip().splitlines()[-1]) == {"removed": 0}
    bad = await asyncio.to_thread(runner.invoke, cli, ["run-job", "nope"])
    assert bad.exit_code == 2
