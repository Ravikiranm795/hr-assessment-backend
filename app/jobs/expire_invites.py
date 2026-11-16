"""Every 5 min: overdue unstarted invites become ``expired``; owners hear 24 h ahead (once per invite)."""

from datetime import datetime, timedelta

from sqlalchemy import text

from app.core import notifications
from app.jobs.base import DEFAULT_FACTORY, SessionFactory, job_ctx, now_utc

NAME = "expire_invites"
WARN_AHEAD = timedelta(hours=24)

_SOON = text(
    """
    SELECT i.id, i.org_id, r.owner_id, i.expires_at FROM invites i
    JOIN open_roles r ON r.id = i.role_id
    WHERE i.status = 'invited' AND i.expires_at > :now AND i.expires_at <= :until
      AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.type = 'invite_expiring' AND n.entity_id = i.id)
    """
)


async def run(session_factory: SessionFactory = DEFAULT_FACTORY, now: datetime | None = None) -> dict:
    now = now_utc(now)
    async with session_factory(job_ctx(NAME)) as session:
        expired = (
            await session.execute(
                text(
                    "UPDATE invites SET status = 'expired', updated_at = now() "
                    "WHERE status = 'invited' AND expires_at <= :now RETURNING id"
                ),
                {"now": now},
            )
        ).all()
        soon = (await session.execute(_SOON, {"now": now, "until": now + WARN_AHEAD})).all()
        for row in soon:
            await notifications.notify(
                session, row.org_id, row.owner_id, "invite_expiring",
                {"invite_id": str(row.id), "expires_at": row.expires_at.isoformat()}, "invite", row.id,
            )  # fmt: skip
    return {"expired": len(expired), "warned": len(soon)}
