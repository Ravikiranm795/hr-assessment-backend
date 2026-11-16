"""RLS must apply at runtime through tenant_session even when the connection user is a superuser."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core import db as core_db
from app.core.db import TenantContext, tenant_session


@pytest.mark.asyncio
async def test_tenant_session_runs_as_app_rw_and_filters_by_org(migrated_db, monkeypatch):
    monkeypatch.setattr(core_db.settings, "database_url", migrated_db)
    core_db._engine.cache_clear()
    ctx = TenantContext(uuid4(), None, "hr_user", False, "r", None, None)
    async with tenant_session(ctx) as session:
        who = (await session.execute(text("select current_user"))).scalar_one()
        assert who == "app_rw"
        seen = (await session.execute(text("select count(*) from organizations"))).scalar_one()
        assert seen == 0  # random org id sees nothing
    core_db._engine.cache_clear()
