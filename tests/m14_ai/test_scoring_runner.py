from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.ai import MockProvider, set_provider_override
from app.core.db import TenantContext, system_context, tenant_session
from app.core.errors import AppError
from app.db import AttemptAnswer, Question
from app.jobs import retry_ai_scoring
from app.modules.m09_review import hooks as review_hooks
from app.modules.m09_review.scoring import ScoringService
from app.modules.m14_ai.scoring_runner import run_ai_scoring
from tests import factories as f

GOOD = {"fraction": 0.5, "reason": "Covers the main idea.", "matched": ["a"], "missed": ["b"]}


async def _sql(db_session, sql, **p):
    await db_session.commit()
    return (await db_session.execute(text(sql), p)).mappings().all()


def _candidate(world):
    return TenantContext(
        org_id=world.org.id, user_id=None, role="candidate", is_platform=False, request_id="t", ip=None,
        user_agent=None, actor_type="candidate",
    )  # fmt: skip


async def _ai_attempt(db_session, world, answer="Joins combine rows from two tables."):
    """A submitted attempt on a published version with one auto question and one AI short answer (4 marks)."""
    a = await f.make_assessment(db_session, world.org, world.hr_owner)
    v = await f.make_version(db_session, a, status="draft", questions=1)
    ai_q = Question(
        version_id=v.id, org_id=world.org.id, position=2, type="short_answer", prompt_html="<p>Explain joins</p>",
        marks=4, scoring_mode="ai", model_answer="A join combines rows of two tables on a key.",
    )  # fmt: skip
    db_session.add(ai_q)
    v.total_marks = 5
    await db_session.flush()
    v.status, v.published_at, v.published_by = "published", datetime.now(UTC), world.hr_owner.id
    role = await f.make_role(db_session, world.org, world.hr_owner, version=v)
    invite = await f.make_invite(db_session, world.org, role, v)
    attempt = await f.make_attempt(db_session, invite, status="submitted")
    db_session.add(AttemptAnswer(attempt_id=attempt.id, question_id=ai_q.id, answer={"text": answer}))
    await db_session.commit()
    ctx = _candidate(world)  # as on a real submit: the AI part is deferred and stays pending
    async with tenant_session(ctx) as session:
        await ScoringService.score_attempt(ctx, session, attempt.id)
    return attempt, ai_q


async def _run(world, attempt):
    ctx = system_context("t", world.org.id)
    async with tenant_session(ctx) as session:
        await run_ai_scoring(ctx, session, attempt.id)


async def _state(db_session, attempt):
    return (
        await _sql(
            db_session,
            "SELECT scoring_status, final_score, score_percent FROM attempts WHERE id = :a",
            a=attempt.id,
        )
    )[0]


async def test_ai_marks_stored_with_reason_and_totals_complete(world, configure, db_session):
    await configure(world.org)
    attempt, q = await _ai_attempt(db_session, world)
    assert (await _state(db_session, attempt))["scoring_status"] == "pending"
    set_provider_override(MockProvider(GOOD))
    await _run(world, attempt)
    row = (
        await _sql(
            db_session,
            "SELECT ai_marks, ai_reason, final_marks, scored_by FROM attempt_answers WHERE question_id = :q",
            q=q.id,
        )
    )[0]
    assert (
        float(row["ai_marks"]) == 2.0
        and row["ai_reason"] == "Covers the main idea."
        and row["scored_by"] == "ai"
    )
    assert float(row["final_marks"]) == 2.0
    state = await _state(db_session, attempt)
    assert (
        state["scoring_status"] == "complete"
        and float(state["final_score"]) == 2.0
        and float(state["score_percent"]) == 40.0
    )


@pytest.mark.parametrize("cause", ["no_provider", "flag_off"])
async def test_missing_provider_or_flag_off_sets_needs_manual_and_notifies(
    world, configure, db_session, cause
):
    if cause == "flag_off":
        await configure(world.org)
        await db_session.execute(
            text(
                "INSERT INTO org_feature_flags (org_id, flag_key, enabled) VALUES (:o, 'ai_scoring', false)"
            ),
            {"o": world.org.id},
        )
        await db_session.commit()
        set_provider_override(MockProvider(GOOD))
    attempt, _ = await _ai_attempt(db_session, world)
    await _run(world, attempt)
    assert (await _state(db_session, attempt))["scoring_status"] == "needs_manual"
    note = await _sql(
        db_session, "SELECT user_id, payload FROM notifications WHERE type = 'scoring_needs_manual'"
    )
    assert [n["user_id"] for n in note] == [world.hr_owner.id]


async def test_repeated_failure_marks_needs_manual_then_rescore_recovers(world, configure, db_session):
    await configure(world.org)
    attempt, q = await _ai_attempt(db_session, world)
    set_provider_override(MockProvider(AppError("AI_PROVIDER_ERROR")))
    for _ in range(2):  # a failing run, then an HR rescore that fails again
        await db_session.execute(
            text("UPDATE attempts SET scoring_status = 'pending' WHERE id = :a"), {"a": attempt.id}
        )
        await db_session.commit()
        await _run(world, attempt)
        assert (await _state(db_session, attempt))["scoring_status"] == "needs_manual"
    assert (await _sql(db_session, "SELECT ai_marks FROM attempt_answers WHERE question_id = :q", q=q.id))[0][
        "ai_marks"
    ] is None
    set_provider_override(MockProvider(GOOD))
    await db_session.execute(
        text("UPDATE attempts SET scoring_status = 'pending' WHERE id = :a"), {"a": attempt.id}
    )
    await db_session.commit()
    await _run(world, attempt)
    assert (await _state(db_session, attempt))["scoring_status"] == "complete"


async def test_submit_path_defers_and_retry_job_scores(world, configure, db_session):
    await configure(world.org)
    attempt, _ = await _ai_attempt(db_session, world)
    candidate = _candidate(world)
    mock = MockProvider(GOOD)
    set_provider_override(mock)
    async with tenant_session(candidate) as session:
        await run_ai_scoring(candidate, session, attempt.id)
    assert mock.calls == [] and (await _state(db_session, attempt))["scoring_status"] == "pending"
    assert run_ai_scoring in review_hooks.ai_scoring_hooks
    assert (await retry_ai_scoring.run())["retried"] == 1
    assert (await _state(db_session, attempt))["scoring_status"] == "complete" and len(mock.calls) == 1


async def test_audit_ai_scoring_run_has_counts_only_and_no_answer_text(world, configure, db_session):
    await configure(world.org)
    attempt, _ = await _ai_attempt(db_session, world, answer="SECRET-ANSWER jane@example.com")
    mock = MockProvider(GOOD)
    set_provider_override(mock)
    await _run(world, attempt)
    audit = await _sql(db_session, "SELECT after::text AS a FROM audit_logs WHERE action = 'AI_SCORING_RUN'")
    assert len(audit) == 1 and "answers" in audit[0]["a"]
    assert "SECRET-ANSWER" not in audit[0]["a"] and "Covers the main idea" not in audit[0]["a"]
    assert "jane@example.com" not in mock.calls[0]["prompt"] and "SECRET-ANSWER" in mock.calls[0]["prompt"]
