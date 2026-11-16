from types import SimpleNamespace

import pytest_asyncio

from app.core import db as core_db
from tests import factories as f


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine():
    """The app engine is cached per URL; function-scoped event loops need a fresh one per test."""
    core_db._engine.cache_clear()
    yield
    await core_db.get_engine().dispose()
    core_db._engine.cache_clear()


@pytest_asyncio.fixture
async def world(db_session):
    org = await f.make_org(db_session)
    other_org = await f.make_org(db_session)
    oa = await f.make_user(db_session, "org_admin", org)
    hr_owner = await f.make_user(db_session, "hr_user", org)
    hr_other = await f.make_user(db_session, "hr_user", org)
    sa = await f.make_user(db_session, "super_admin")
    outsider = await f.make_user(db_session, "org_admin", other_org)
    await db_session.commit()
    users = {"oa": oa, "hr_owner": hr_owner, "hr_other": hr_other, "sa": sa, "outsider": outsider}
    headers = {name: await f.auth_headers(db_session, u.role, user=u) for name, u in users.items()}
    return SimpleNamespace(org=org, other_org=other_org, users=users, h=SimpleNamespace(**headers), **users)
