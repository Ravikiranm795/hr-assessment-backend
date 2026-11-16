"""Every minute: submit in-progress attempts past ``deadline_at`` plus the save grace, then score them.

Uses ``ExamService.finalize`` (row locked, idempotent), so a sweep racing a candidate's own submit
produces exactly one submission (RF-2). Importing the scoring module registers the submit hook."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

import app.modules.m09_review.scoring  # noqa: F401  (registers the on_submit scoring hook)
from app.jobs.base import BATCH, DEFAULT_FACTORY, SessionFactory, job_ctx, now_utc
from app.modules.m08_exam.service import SAVE_GRACE, ExamService

NAME = "timeout_attempts"
log = logging.getLogger("app.jobs.timeout_attempts")


async def run(session_factory: SessionFactory = DEFAULT_FACTORY, now: datetime | None = None) -> dict:
    now = now_utc(now)
    cutoff: datetime = now - SAVE_GRACE if isinstance(SAVE_GRACE, timedelta) else now
    async with session_factory(job_ctx(NAME)) as session:
        due = (
            await session.execute(
                text(
                    "SELECT id, org_id FROM attempts WHERE status = 'in_progress' AND deadline_at <= :cutoff "
                    "ORDER BY deadline_at LIMIT :n"
                ),
                {"cutoff": cutoff, "n": BATCH},
            )
        ).all()
    submitted = failed = 0
    for row in due:
        ctx = job_ctx(NAME, row.org_id)
        try:
            async with session_factory(ctx) as session:
                _, newly = await ExamService.finalize(ctx, session, row.id, "timer")
            submitted += newly
        except Exception:  # noqa: BLE001 - one bad attempt must not block the rest; it is retried next minute
            failed += 1
            log.exception("timeout submit failed attempt=%s", row.id)
    return {"due": len(due), "submitted": submitted, "failed": failed}
