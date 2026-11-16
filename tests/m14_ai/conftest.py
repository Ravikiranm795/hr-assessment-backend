from types import SimpleNamespace

import pytest_asyncio

from app.core import db as core_db
from app.core.ai import set_provider_override
from app.core.ratelimit import set_rate_limiter
from app.core.security import encrypt_field
from app.db import OrgAiSettings
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
async def _no_leaked_overrides():
    set_rate_limiter(None)
    set_provider_override(None)
    yield
    set_provider_override(None)
    set_rate_limiter(None)


@pytest_asyncio.fixture
async def configure(db_session):
    """``await configure(org, monthly_call_cap=None)``: a Claude API settings row; AI calls are mocked."""

    async def _configure(org, **overrides):
        values = {
            "provider": "claude",
            "model": "claude-haiku-5-5",
            "mode": "api",
            "api_key_enc": encrypt_field("sk-test"),
        }
        db_session.add(OrgAiSettings(org_id=org.id, **(values | overrides)))
        await db_session.commit()

    return _configure


TOPICS = [
    "database indexing strategy",
    "transaction isolation levels",
    "REST idempotency",
    "python generators",
    "asyncio event loops",
    "connection pooling",
    "cache invalidation",
    "rate limiting algorithms",
    "JWT expiry handling",
    "schema migrations",
    "unit test isolation",
    "message queue ordering",
    "pagination cursors",
    "retry with backoff",
    "structured logging",
]


def questions(n: int) -> dict:
    return {
        "questions": [
            {
                "type": "single_choice",
                "difficulty": "medium",
                "prompt": f"Which statement about {TOPICS[i]} is correct for a production service?",
                "marks": 1,
                "options": [
                    {"text": f"Right answer for {TOPICS[i]}", "is_correct": True},
                    {"text": f"Wrong answer for {TOPICS[i]}", "is_correct": False},
                ],
            }
            for i in range(n)
        ]
    }


@pytest_asyncio.fixture(autouse=True)
async def _clean_between_tests(db_engine):
    """Notification and audit assertions count rows, so every test starts from catalogs only."""
    from tests.conftest import truncate_all

    yield
    truncate_all(db_engine.url.render_as_string(hide_password=False))
