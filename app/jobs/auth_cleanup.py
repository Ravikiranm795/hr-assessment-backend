"""Nightly: purge dead refresh tokens and trim password history to each organization's policy depth.

Refresh tokens are removed one day after they expire. A revoked token is kept until then on purpose: its
expiry is at most ``REFRESH_TTL_DAYS`` away, and until it expires a replay of it must still be recognised
as reuse (which revokes the whole family). Password history keeps the newest ``history`` hashes per user
(organization policy, default 5, minimum 1, same rule as the password-change check; super admins use 5)."""

from datetime import datetime, timedelta

from sqlalchemy import text

from app.jobs.base import DEFAULT_FACTORY, SessionFactory, job_ctx, now_utc

NAME = "auth_cleanup"
TOKEN_GRACE = timedelta(days=1)
DEFAULT_HISTORY = 5

_TRIM_HISTORY = text(
    """
    DELETE FROM password_history WHERE id IN (
        SELECT id FROM (
            SELECT p.id,
                   row_number() OVER (PARTITION BY p.user_id ORDER BY p.created_at DESC, p.id DESC) AS rn,
                   GREATEST(CASE WHEN o.password_policy ->> 'history' ~ '^[0-9]+$'
                                 THEN (o.password_policy ->> 'history')::int ELSE :default END, 1) AS keep
            FROM password_history p
            JOIN users u ON u.id = p.user_id
            LEFT JOIN organizations o ON o.id = u.org_id
        ) ranked WHERE rn > keep
    ) RETURNING id
    """
)


async def run(session_factory: SessionFactory = DEFAULT_FACTORY, now: datetime | None = None) -> dict:
    now = now_utc(now)
    async with session_factory(job_ctx(NAME)) as session:
        tokens = (
            await session.execute(
                text("DELETE FROM refresh_tokens WHERE expires_at < :cutoff RETURNING id"),
                {"cutoff": now - TOKEN_GRACE},
            )
        ).all()
        history = (await session.execute(_TRIM_HISTORY, {"default": DEFAULT_HISTORY})).all()
    return {"refresh_tokens_removed": len(tokens), "password_history_removed": len(history)}
