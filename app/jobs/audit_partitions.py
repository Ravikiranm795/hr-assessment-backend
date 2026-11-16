"""Monthly: create the next audit partitions, drop those entirely older than 13 months, log the purge.

The partition functions are executable only by the ``maintenance`` role, so DDL runs on its own
connection (``DATABASE_URL_MAINTENANCE``, falling back to ``DATABASE_URL``)."""

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import audit
from app.core.config import settings
from app.jobs.base import DEFAULT_FACTORY, SessionFactory, job_ctx, now_utc

NAME = "audit_partitions"
AHEAD_MONTHS = 14
RETENTION_MONTHS = 13


def _first_of_month(d: date, back: int = 0) -> date:
    index = d.year * 12 + (d.month - 1) - back
    return date(index // 12, index % 12 + 1, 1)


async def run(session_factory: SessionFactory = DEFAULT_FACTORY, now: datetime | None = None) -> dict:
    today = now_utc(now).date()
    start, cutoff = _first_of_month(today), _first_of_month(today, RETENTION_MONTHS)
    engine = create_async_engine(settings.database_url_maintenance or settings.database_url)
    try:
        async with engine.begin() as conn:
            created = (
                await conn.execute(
                    text("SELECT audit_ensure_partitions(:m, CAST(:s AS date))"),
                    {"m": AHEAD_MONTHS, "s": start},
                )
            ).scalar_one()
            dropped = (
                await conn.execute(
                    text("SELECT audit_drop_partitions_before(CAST(:c AS date))"), {"c": cutoff}
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    if dropped:
        ctx = job_ctx(NAME)
        async with session_factory(ctx) as session:
            await audit.record(
                ctx, session, audit.AUDIT_PURGE_RUN, "system", NAME,
                after={"partitions_dropped": dropped, "older_than": cutoff.isoformat()},
            )  # fmt: skip
    return {"created": created, "dropped": dropped}
