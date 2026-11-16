"""In-app notifications (spec 4.10, Appendix C). Insert-only helper using Core ``text()``."""

import json
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import text

HELP_REQUEST_CREATED = "help_request_created"
ATTEMPT_SUBMITTED = "attempt_submitted"
SERIOUS_FLAG = "serious_flag"
INVITE_EXPIRING = "invite_expiring"
SCORING_NEEDS_MANUAL = "scoring_needs_manual"
MISUSE_FLAG_RAISED = "misuse_flag_raised"
MISUSE_FLAG_CLEARED = "misuse_flag_cleared"
ACCOUNT_LOCKED = "account_locked"

NOTIFICATION_TYPES: frozenset[str] = frozenset(
    {
        HELP_REQUEST_CREATED,
        ATTEMPT_SUBMITTED,
        SERIOUS_FLAG,
        INVITE_EXPIRING,
        SCORING_NEEDS_MANUAL,
        MISUSE_FLAG_RAISED,
        MISUSE_FLAG_CLEARED,
        ACCOUNT_LOCKED,
    }
)

_INSERT = text(
    """
    INSERT INTO notifications (id, org_id, user_id, type, payload, entity_type, entity_id, created_at)
    VALUES (:id, :org_id, :user_id, :type, CAST(:payload AS jsonb), :entity_type, :entity_id, now())
    """
)


async def notify(
    session: Any,
    org_id: UUID | None,
    user_id: UUID,
    type: str,  # noqa: A002 - frozen signature
    payload: dict,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
) -> None:
    """Insert one notification for ``user_id`` in the caller's transaction. ``payload`` holds
    ids and codes only (no display text, no candidate answers). Debouncing is the caller's job."""
    if type not in NOTIFICATION_TYPES:
        raise ValueError(f"unknown notification type {type!r} (spec Appendix C)")
    await session.execute(
        _INSERT,
        {
            "id": uuid.uuid4(),
            "org_id": org_id,
            "user_id": user_id,
            "type": type,
            "payload": json.dumps(payload, default=str),
            "entity_type": entity_type,
            "entity_id": entity_id,
        },
    )
