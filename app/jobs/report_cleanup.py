"""Nightly: remove generated report files older than 7 days (the request row stays as history)."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from app.core.storage import get_storage
from app.jobs.base import BATCH, DEFAULT_FACTORY, SessionFactory, job_ctx, now_utc

NAME = "report_cleanup"
KEEP = timedelta(days=7)
log = logging.getLogger("app.jobs.report_cleanup")


async def run(session_factory: SessionFactory = DEFAULT_FACTORY, now: datetime | None = None) -> dict:
    now = now_utc(now)
    keys: list[str] = []
    async with session_factory(job_ctx(NAME)) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT r.id, r.asset_id, a.storage_key FROM report_requests r "
                    "JOIN assets a ON a.id = r.asset_id "
                    "WHERE r.created_at < :cutoff ORDER BY r.created_at LIMIT :n"
                ),
                {"cutoff": now - KEEP, "n": BATCH},
            )
        ).all()
        for row in rows:
            await session.execute(
                text("UPDATE report_requests SET asset_id = NULL WHERE id = :r"), {"r": row.id}
            )
            await session.execute(text("DELETE FROM assets WHERE id = :a"), {"a": row.asset_id})
            keys.append(row.storage_key)
    for key in keys:  # after commit: a failed file delete leaves an orphan file, never a dangling row
        try:
            await get_storage().delete(key)
        except Exception:  # noqa: BLE001
            log.warning("report file delete failed key=%s", key)
    return {"removed": len(keys)}
