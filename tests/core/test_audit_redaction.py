import json
import uuid

import pytest

from app.core import audit
from app.core.db import TenantContext

REDACTED = "[REDACTED]"


class FakeSession:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


def _ctx():
    return TenantContext(
        org_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role="org_admin",
        is_platform=False,
        request_id="req-1",
        ip="10.0.0.1",
        user_agent="pytest",
        actor_name="Ada Admin",
    )


def test_redact_masks_sensitive_names_recursively():
    data = {
        "name": "Ada",
        "password": "p",
        "password_hash": "h",
        "token_hash": "t",
        "candidate_email_enc": "e",
        "api_key": "k",
        "secret_note": "s",
        "refresh_token": "r",
        "nested": {"tokens": ["a"], "safe": 1, "items": [{"api_key_last4": "1234", "x": 2}]},
    }
    out = audit.redact(data)
    assert out["name"] == "Ada"
    for key in ("password", "password_hash", "token_hash", "candidate_email_enc", "api_key"):
        assert out[key] == REDACTED
    assert out["secret_note"] == REDACTED and out["refresh_token"] == REDACTED
    assert out["nested"]["tokens"] == REDACTED and out["nested"]["safe"] == 1
    assert out["nested"]["items"][0] == {"api_key_last4": REDACTED, "x": 2}
    assert data["password"] == "p"  # input not mutated


async def test_audit_redacts_password_hash_token_fields():
    session = FakeSession()
    ctx = _ctx()
    entity = uuid.uuid4()
    await audit.record(
        ctx,
        session,
        audit.USER_UPDATED,
        "user",
        entity,
        before={"name": "Old", "password_hash": "$argon2id$old", "token_version": 1},
        after={"name": "New", "password_hash": "$argon2id$new", "token_version": 1},
        reason="rename",
    )
    assert len(session.calls) == 1
    sql, params = session.calls[0]
    assert "INSERT INTO audit_logs" in sql
    before, after = json.loads(params["before"]), json.loads(params["after"])
    assert "$argon2id" not in params["before"] + params["after"]
    assert before["password_hash"] == REDACTED and after["password_hash"] == REDACTED
    assert before["token_version"] == REDACTED
    assert after["name"] == "New"
    # Changed marker: the redacted password_hash changed, token_version did not.
    assert after[audit.REDACTED_CHANGED_KEY] == ["password_hash"]
    assert params["action"] == "USER_UPDATED"
    assert params["org_id"] == ctx.org_id and params["actor_user_id"] == ctx.user_id
    assert params["actor_role"] == "org_admin" and params["actor_name_snapshot"] == "Ada Admin"
    assert params["entity_id"] == str(entity) and params["request_id"] == "req-1"
    assert params["result"] == "success" and params["reason"] == "rename"
    assert isinstance(params["id"], uuid.UUID) and params["id"].version == 7


async def test_audit_rejects_unknown_action_and_result():
    with pytest.raises(ValueError):
        await audit.record(_ctx(), FakeSession(), "MADE_UP", "user", None)
    with pytest.raises(ValueError):
        await audit.record(_ctx(), FakeSession(), audit.USER_CREATED, "user", None, result="meh")


def test_audit_catalog_matches_appendix_b():
    assert len(audit.AUDIT_ACTIONS) == 85
    for name in ("AUTH_LOGIN_SUCCESS", "AUDIT_PURGE_RUN", "INVITE_BINDING_RESET", "SNAPSHOT_VIEWED"):
        assert getattr(audit, name) == name and name in audit.AUDIT_ACTIONS
