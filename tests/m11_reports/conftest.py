"""Fixtures for the M11 report-chat tests. The AI is always a MockProvider; no real call is made."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.core import db as core_db
from app.core.ai import MockProvider, set_provider_override
from app.core.security import encrypt_field
from app.db import OrgAiSettings
from tests import factories as f

DECIDED = {"field": "invite_status", "op": "eq", "value": "invited"}
SPEC = {
    "source": "candidates",
    "filters": [DECIDED],
    "columns": ["candidate_name", "candidate_email", "role_title"],
}
PEOPLE = SimpleNamespace(
    name_a="Zorro Quillfeather", email_a="zorro.quill@example.com", name_b="Other Org Person"
)


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine():
    core_db._engine.cache_clear()
    yield
    await core_db.get_engine().dispose()
    core_db._engine.cache_clear()
    set_provider_override(None)


@pytest_asyncio.fixture
async def world(db_session):
    org_a, org_b = await f.make_org(db_session), await f.make_org(db_session)
    db_session.add(
        OrgAiSettings(
            org_id=org_a.id, provider="claude", model="m", mode="api", api_key_enc=encrypt_field("sk")
        )
    )
    oa_a = await f.make_user(db_session, "org_admin", org_a)
    hr_a = await f.make_user(db_session, "hr_user", org_a)
    sa = await f.make_user(db_session, "super_admin")
    oa_b = await f.make_user(db_session, "org_admin", org_b)
    # Org A: a candidate whose name starts with a formula character; org B: one candidate that must never show up.
    await f.make_invite(
        db_session,
        org_a,
        email=PEOPLE.email_a,
        candidate_name=f"={PEOPLE.name_a}",
        candidate_email_enc=encrypt_field(PEOPLE.email_a),
    )
    await f.make_invite(db_session, org_a, candidate_name="Second Candidate")
    await f.make_invite(db_session, org_b, candidate_name=PEOPLE.name_b)
    await db_session.commit()
    h = {
        "oa": await f.auth_headers(db_session, "org_admin", user=oa_a),
        "hr": await f.auth_headers(db_session, "hr_user", user=hr_a),
        "sa": await f.auth_headers(db_session, "super_admin", user=sa),
        "oa_b": await f.auth_headers(db_session, "org_admin", user=oa_b),
    }
    return SimpleNamespace(org_a=org_a, org_b=org_b, oa=oa_a, h=SimpleNamespace(**h))


@pytest.fixture
def ai():
    """``ai(*responses)`` installs a MockProvider returning those dicts and returns it."""

    def install(*responses):
        provider = MockProvider(list(responses))
        set_provider_override(provider)
        return provider

    return install


def interpretations(*specs, label="Candidates still waiting for a decision"):
    return {"interpretations": [{"label": label, "spec": s} for s in specs]}
