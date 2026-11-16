"""Nightly: remove read notifications older than 60 days (unread ones are kept)."""

from datetime import datetime, timedelta

from sqlalchemy import text

from app.jobs.base import DEFAULT_FACTORY, SessionFactory, job_ctx, now_utc

NAME = "notification_cleanup"
KEEP = timedelta(days=60)


async def run(session_factory: SessionFactory = DEFAULT_FACTORY, now: datetime | None = None) -> dict:
    now = now_utc(now)
    async with session_factory(job_ctx(NAME)) as session:
        removed = (
            await session.execute(
                text(
                    "DELETE FROM notifications WHERE read_at IS NOT NULL AND read_at < :cutoff RETURNING id"
                ),
                {"cutoff": now - KEEP},
            )
        ).all()
    return {"removed": len(removed)}
