"""Nightly: apply the 3-version rule safely. Version creation already purges one at a time; this catches
any assessment that ended up over the limit. Protected (in-use) versions and drafts are never deleted."""

import logging
from datetime import datetime

from sqlalchemy import text

from app.core import audit
from app.jobs.base import DEFAULT_FACTORY, SessionFactory, job_ctx, now_utc
from app.modules.m05_assessments import repo
from app.modules.m05_assessments.version_rules import MAX_VERSIONS

NAME = "version_purge"
log = logging.getLogger("app.jobs.version_purge")


async def run(session_factory: SessionFactory = DEFAULT_FACTORY, now: datetime | None = None) -> dict:
    now_utc(now)
    async with session_factory(job_ctx(NAME)) as session:
        over = (
            await session.execute(
                text(
                    "SELECT assessment_id, org_id FROM assessment_versions "
                    "GROUP BY assessment_id, org_id HAVING count(*) > :m"
                ),
                {"m": MAX_VERSIONS},
            )
        ).all()
    purged = failed = 0
    for row in over:
        ctx = job_ctx(NAME, row.org_id)
        try:
            async with session_factory(ctx) as session:
                versions = await repo.list_versions(session, row.org_id, [row.assessment_id])
                protected = await repo.protected_version_ids(session, row.org_id, row.assessment_id)
                excess = len(versions) - MAX_VERSIONS
                victims = [
                    v
                    for v in sorted(versions, key=lambda v: v.version_no)
                    if v.status == "published" and v.id not in protected
                ]
                for victim in victims[: max(excess, 0)]:
                    await audit.record(
                        ctx, session, audit.VERSION_PURGED, "assessment_version", victim.id,
                        before={"assessment_id": row.assessment_id, "version_no": victim.version_no},
                        reason="version_purge_job",
                    )  # fmt: skip
                    await repo.delete_version(session, victim)
                    purged += 1
        except Exception:  # noqa: BLE001
            failed += 1
            log.exception("version purge failed assessment=%s", row.assessment_id)
    if purged:
        ctx = job_ctx(NAME)
        async with session_factory(ctx) as session:
            await audit.record(
                ctx, session, audit.VERSION_PURGE_RUN, "system", NAME, after={"versions_purged": purged}
            )
    return {"assessments_over_limit": len(over), "purged": purged, "failed": failed}
