"""Fixtures for the M10 audit tests.

Audit rows are written through ``core.audit.record`` (the single write path) with a hand-built
``TenantContext``; the API under test reads them through the app role (``SET LOCAL ROLE app_rw``).
"""

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core import db as core_db
from app.core.db import TenantContext
from tests import factories


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """The app engine is cached per URL; every test runs in its own event loop."""
    yield
    await core_db.get_engine().dispose()
    core_db._engine.cache_clear()


def make_ctx(
    org_id: uuid.UUID | None,
    role: str = "org_admin",
    *,
    user_id: uuid.UUID | None = None,
    name: str | None = "Actor",
    actor_type: str = "user",
) -> TenantContext:
    return TenantContext(
        org_id=org_id,
        user_id=user_id or uuid.uuid4(),
        role=role,
        is_platform=org_id is None,
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        ip="203.0.113.7",
        user_agent="pytest-agent",
        actor_type=actor_type,
        actor_name=name,
    )


@dataclass
class World:
    org_a: Any
    org_b: Any
    oa_a: dict[str, str]
    oa_b: dict[str, str]
    hr_a: dict[str, str]
    sa: dict[str, str]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    org_a = await factories.make_org(db_session)
    org_b = await factories.make_org(db_session)
    w = World(
        org_a=org_a,
        org_b=org_b,
        oa_a=await factories.auth_headers(db_session, "org_admin", org_a),
        oa_b=await factories.auth_headers(db_session, "org_admin", org_b),
        hr_a=await factories.auth_headers(db_session, "hr_user", org_a),
        sa=await factories.auth_headers(db_session, "super_admin"),
    )
    await db_session.commit()
    return w


@pytest.fixture
def add_row(db_session: AsyncSession) -> Callable[..., Awaitable[None]]:
    """``await add_row(org_id, ACTION, entity_id=..., ...)`` writes and commits one audit row."""

    async def _add(
        org_id: uuid.UUID | None,
        action: str = audit.USER_CREATED,
        *,
        entity_type: str = "user",
        entity_id: Any = None,
        before: dict | None = None,
        after: dict | None = None,
        reason: str | None = None,
        result: str = "success",
        ctx: TenantContext | None = None,
        **ctx_kwargs: Any,
    ) -> None:
        await audit.record(
            ctx or make_ctx(org_id, **ctx_kwargs),
            db_session,
            action,
            entity_type,
            entity_id if entity_id is not None else uuid.uuid4(),
            before=before,
            after=after,
            reason=reason,
            result=result,
        )
        await db_session.commit()

    return _add


@pytest.fixture
def add_raw_row(db_session: AsyncSession) -> Callable[..., Awaitable[uuid.UUID]]:
    """Insert a row with an explicit ``created_at`` and unredacted JSON (superuser, bypasses record)."""

    async def _add(org_id: uuid.UUID | None, created_at: datetime, **values: Any) -> uuid.UUID:
        from uuid6 import uuid7

        row_id = uuid7()
        params = {
            "id": row_id,
            "org_id": org_id,
            "action": values.pop("action", audit.USER_UPDATED),
            "entity_type": values.pop("entity_type", "user"),
            "entity_id": values.pop("entity_id", "raw-1"),
            "before": json.dumps(values["before"]) if values.get("before") else None,
            "after": json.dumps(values["after"]) if values.get("after") else None,
            "created_at": created_at,
        }
        await db_session.execute(
            text(
                "INSERT INTO audit_logs (id, org_id, actor_type, actor_name_snapshot, actor_role, action, "
                "entity_type, entity_id, before, after, result, created_at) VALUES (:id, :org_id, 'user', "
                "'Raw', 'org_admin', :action, :entity_type, :entity_id, CAST(:before AS jsonb), "
                "CAST(:after AS jsonb), 'success', :created_at)"
            ),
            params,
        )
        await db_session.commit()
        return row_id

    return _add
