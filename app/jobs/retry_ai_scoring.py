"""Every 5 min: retry attempts whose AI scoring is still ``pending`` through the registered hooks.

An attempt still pending 15 minutes after submit (about three sweeps) becomes ``needs_manual`` and the
role owner is notified."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from app.core import notifications
from app.jobs.base import BATCH, DEFAULT_FACTORY, SessionFactory, job_ctx, now_utc
from app.modules.m09_review import hooks

NAME = "retry_ai_scoring"
STALE_AFTER = timedelta(minutes=15)
log = logging.getLogger("app.jobs.retry_ai_scoring")


async def run(session_factory: SessionFactory = DEFAULT_FACTORY, now: datetime | None = None) -> dict:
    now = now_utc(now)
    async with session_factory(job_ctx(NAME)) as session:
        pending = (
            await session.execute(
                text(
                    "SELECT a.id, a.org_id, a.submitted_at, r.owner_id FROM attempts a "
                    "JOIN invites i ON i.id = a.invite_id JOIN open_roles r ON r.id = i.role_id "
                    "WHERE a.status = 'submitted' AND a.scoring_status = 'pending' AND a.purged_at IS NULL "
                    "ORDER BY a.submitted_at LIMIT :n"
                ),
                {"n": BATCH},
            )
        ).all()
    retried = manual = failed = 0
    for row in pending:
        ctx = job_ctx(NAME, row.org_id)
        try:
            async with session_factory(ctx) as session:
                if row.submitted_at is not None and row.submitted_at <= now - STALE_AFTER:
                    await session.execute(
                        text(
                            "UPDATE attempts SET scoring_status = 'needs_manual' "
                            "WHERE id = :a AND scoring_status = 'pending'"
                        ),
                        {"a": row.id},
                    )
                    await notifications.notify(
                        session, row.org_id, row.owner_id, "scoring_needs_manual",
                        {"attempt_id": str(row.id)}, "attempt", row.id,
                    )  # fmt: skip
                    manual += 1
                else:
                    for hook in hooks.ai_scoring_hooks:
                        await hook(ctx, session, row.id)
                    retried += 1
        except Exception:  # noqa: BLE001
            failed += 1
            log.exception("ai scoring retry failed attempt=%s", row.id)
    return {"pending": len(pending), "retried": retried, "needs_manual": manual, "failed": failed}
