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


@pytest_asyncio.fixture(autouse=True)
async def _isolation():
    from app.core.email import set_email_sender
    from app.core.ratelimit import set_rate_limiter

    set_rate_limiter(None)
    set_email_sender(None)
    yield
    set_rate_limiter(None)
    set_email_sender(None)


@pytest_asyncio.fixture
async def open_role(db_session, world):
    """An open role owned by ``hr_owner`` with a linked published version."""

    async def make(*, linked=True, status="open"):
        a = await f.make_assessment(db_session, world.org)
        v = await f.make_version(db_session, a)
        role = await f.make_role(
            db_session, world.org, world.hr_owner, version=v if linked else None, status=status
        )
        await db_session.commit()
        return role, v

    return make


@pytest_asyncio.fixture(autouse=True)
async def _clean_between_tests(db_engine):
    """Assertions count rows across the table, so every test starts from catalogs only."""
    from tests.conftest import truncate_all

    yield
    truncate_all(db_engine.url.render_as_string(hide_password=False))
