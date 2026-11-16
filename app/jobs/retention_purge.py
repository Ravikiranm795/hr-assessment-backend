"""Nightly: erase candidate detail once a decision is older than the organization's retention days.

Keeps the summary (score, decision, flag counts) and sets ``purged_at``. Undecided attempts are never
purged: they surface in the "needs attention" gadget."""

import logging
from datetime import datetime

from sqlalchemy import text

from app.core import audit
from app.jobs.base import BATCH, DEFAULT_FACTORY, SessionFactory, job_ctx, now_utc
from app.modules.m09_review.service import ReviewService

NAME = "retention_purge"
log = logging.getLogger("app.jobs.retention_purge")

_DUE = text(
    """
    SELECT a.id, a.org_id FROM attempts a JOIN organizations o ON o.id = a.org_id
    WHERE a.status = 'submitted' AND a.decision <> 'waiting' AND a.purged_at IS NULL
      AND a.decided_at <= CAST(:now AS timestamptz) - make_interval(days => o.retention_days)
    ORDER BY a.decided_at LIMIT :n
    """
)


async def run(session_factory: SessionFactory = DEFAULT_FACTORY, now: datetime | None = None) -> dict:
    now = now_utc(now)
    async with session_factory(job_ctx(NAME)) as session:
        due = (await session.execute(_DUE, {"now": now, "n": BATCH})).all()
    purged = failed = 0
    for row in due:
        ctx = job_ctx(NAME, row.org_id)
        try:
            async with session_factory(ctx) as session:
                await ReviewService.erase_candidate_data(ctx, session, row.id)
            purged += 1
        except Exception:  # noqa: BLE001
            failed += 1
            log.exception("retention purge failed attempt=%s", row.id)
    if purged:
        ctx = job_ctx(NAME)
        async with session_factory(ctx) as session:
            await audit.record(
                ctx, session, audit.RETENTION_PURGE_RUN, "system", NAME,
                after={"attempts_purged": purged, "failed": failed},
            )  # fmt: skip
    return {"due": len(due), "purged": purged, "failed": failed}
