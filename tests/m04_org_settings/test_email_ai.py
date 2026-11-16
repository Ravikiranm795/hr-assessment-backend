"""SMTP and AI provider settings: write-only secrets, audit, rate limit, allowlist (D-08, D-12)."""

import pytest

from app.core import security
from app.core.ai import MockProvider, set_provider_override
from app.core.config import settings
from app.core.email import MemoryEmailSender, set_email_sender
from app.modules.m04_org_settings import service as m4service
from tests.m04_org_settings.conftest import audit_rows, row

API = "/api/v1/org"
SMTP = {
    "host": "smtp.example.com",
    "port": 587,
    "security": "starttls",
    "username": "mailer",
    "password": "s3cret-smtp-pass",
    "sender_name": "Acme HR",
    "sender_email": "hr@example.com",
    "enabled": True,
}


# --------------------------------------------------------------------------- email


async def test_email_get_when_not_configured(http, tenant):
    r = await http.get(f"{API}/settings/email", headers=tenant.headers)
    assert r.status_code == 200
    assert r.json()["configured"] is False and r.json()["password_set"] is False


async def test_smtp_password_write_only_masked_on_read(http, tenant, db_session):
    put = await http.put(f"{API}/settings/email", headers=tenant.headers, json=SMTP)
    assert put.status_code == 200, put.text
    for response in (put.json(), (await http.get(f"{API}/settings/email", headers=tenant.headers)).json()):
        assert "password" not in response and "password_enc" not in response
        assert response["password_set"] is True
        assert response["password_masked"] == "********" and SMTP["password"] not in str(response)
        assert response["host"] == "smtp.example.com" and response["security"] == "starttls"

    stored = await row(db_session, "SELECT * FROM org_email_settings WHERE org_id = :o", o=tenant.org.id)
    assert stored["password_enc"] != SMTP["password"] and SMTP["password"] not in stored["password_enc"]
    assert security.decrypt_field(stored["password_enc"]) == SMTP["password"]

    # omitting the password keeps it; empty string clears it
    body = {k: v for k, v in SMTP.items() if k != "password"} | {"port": 2525}
    kept = await http.put(f"{API}/settings/email", headers=tenant.headers, json=body)
    assert kept.json()["password_set"] is True and kept.json()["port"] == 2525
    stored = await row(db_session, "SELECT password_enc FROM org_email_settings WHERE org_id = :o", o=tenant.org.id)
    assert security.decrypt_field(stored["password_enc"]) == SMTP["password"]
    cleared = await http.put(f"{API}/settings/email", headers=tenant.headers, json=body | {"password": ""})
    assert cleared.json()["password_set"] is False

    audits = await audit_rows(db_session, tenant.org.id, "EMAIL_SETTINGS_UPDATED")
    assert len(audits) == 3
    assert SMTP["password"] not in str(audits)
    assert audits[0]["after"]["password_enc"] == "[REDACTED]"
    assert "password_enc" in audits[0]["after"]["_redacted_changed"]


@pytest.mark.parametrize(
    "patch",
    [
        {"host": ""},
        {"host": "smtp.example.com/evil"},
        {"host": "http://smtp.example.com"},
        {"host": "has space.com"},
        {"port": 0},
        {"port": 70000},
        {"security": "plaintext"},
        {"sender_email": "not-an-email"},
        {"sender_email": "a@b.co\r\nBcc: x@y.z"},
        {"sender_name": "Eve\r\nBcc: x@y.z"},
    ],
)
async def test_email_settings_rejects_bad_input(http, tenant, patch):
    r = await http.put(f"{API}/settings/email", headers=tenant.headers, json=SMTP | patch)
    assert r.status_code == 422, patch
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_email_host_rejects_internal_ip_in_prod(http, tenant, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "prod")
    for host in ("127.0.0.1", "169.254.169.254", "10.0.0.5", "::1", "0.0.0.0"):
        r = await http.put(f"{API}/settings/email", headers=tenant.headers, json=SMTP | {"host": host})
        assert r.status_code == 422, host


async def test_changing_smtp_target_resets_verified(http, tenant, db_session, outbox):
    await http.put(f"{API}/settings/email", headers=tenant.headers, json=SMTP)
    sent = await http.post(f"{API}/settings/email/test", headers=tenant.headers, json={"to": "me@example.com"})
    assert sent.json()["verified_at"]
    same = await http.put(f"{API}/settings/email", headers=tenant.headers, json=SMTP | {"enabled": False})
    assert same.json()["verified_at"] is not None  # only `enabled` changed
    moved = await http.put(f"{API}/settings/email", headers=tenant.headers, json=SMTP | {"host": "other.example.com"})
    assert moved.json()["verified_at"] is None


async def test_email_test_send_audited_and_rate_limited(http, tenant, db_session, outbox, monkeypatch):
    monkeypatch.setattr(m4service, "EMAIL_TEST_LIMIT", "2/minute")
    await http.put(f"{API}/settings/email", headers=tenant.headers, json=SMTP)

    for _ in range(2):
        r = await http.post(f"{API}/settings/email/test", headers=tenant.headers, json={"to": "me@example.com"})
        assert r.status_code == 200 and r.json()["sent"] is True
    assert len(outbox.outbox) == 2
    smtp, message = outbox.outbox[0]
    assert message.to == "me@example.com" and smtp.password == SMTP["password"] and smtp.security == "starttls"
    assert smtp.sender_email == "hr@example.com"

    limited = await http.post(f"{API}/settings/email/test", headers=tenant.headers, json={"to": "me@example.com"})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert limited.json()["error"]["details"]["retry_after_sec"] >= 1
    assert len(outbox.outbox) == 2

    audits = await audit_rows(db_session, tenant.org.id, "EMAIL_TEST_SENT")
    assert len(audits) == 2 and audits[0]["result"] == "success"
    assert audits[0]["after"]["to"] == "me@example.com"
    assert SMTP["password"] not in str(audits)
    done = await row(db_session, "SELECT verified_at FROM org_email_settings WHERE org_id = :o", o=tenant.org.id)
    assert done["verified_at"] is not None


async def test_email_test_rate_limit_is_per_org(http, tenant, other_tenant, outbox, monkeypatch):
    monkeypatch.setattr(m4service, "EMAIL_TEST_LIMIT", "1/minute")
    for t in (tenant, other_tenant):
        await http.put(f"{API}/settings/email", headers=t.headers, json=SMTP)
        r = await http.post(f"{API}/settings/email/test", headers=t.headers, json={"to": "me@example.com"})
        assert r.status_code == 200


async def test_email_test_without_settings_is_SMTP_NOT_CONFIGURED(http, tenant, outbox):
    r = await http.post(f"{API}/settings/email/test", headers=tenant.headers, json={"to": "me@example.com"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "SMTP_NOT_CONFIGURED"
    assert outbox.outbox == []


async def test_email_test_failure_is_reported_and_audited(http, tenant, db_session):
    class Boom(MemoryEmailSender):
        async def send(self, smtp, message):
            raise ConnectionRefusedError("connect to smtp.example.com:587 failed")

    set_email_sender(Boom())
    await http.put(f"{API}/settings/email", headers=tenant.headers, json=SMTP)
    r = await http.post(f"{API}/settings/email/test", headers=tenant.headers, json={"to": "me@example.com"})
    assert r.status_code == 200
    assert r.json()["sent"] is False and r.json()["verified_at"] is None
    assert r.json()["reason"] == "smtp_send_failed"
    assert SMTP["password"] not in r.text
    audits = await audit_rows(db_session, tenant.org.id, "EMAIL_TEST_SENT")
    assert len(audits) == 1 and audits[0]["result"] == "failure"


@pytest.mark.parametrize("to", ["", "nope", "a@b.co\r\nBcc: x@y.z", "x" * 300 + "@b.co"])
async def test_email_test_rejects_bad_recipient(http, tenant, outbox, to):
    await http.put(f"{API}/settings/email", headers=tenant.headers, json=SMTP)
    r = await http.post(f"{API}/settings/email/test", headers=tenant.headers, json={"to": to})
    assert r.status_code == 422 and outbox.outbox == []


# --------------------------------------------------------------------------- ai providers and settings


async def test_ai_provider_catalog(http, tenant):
    r = await http.get(f"{API}/settings/ai/providers", headers=tenant.headers)
    assert r.status_code == 200
    data = {p["id"]: p for p in r.json()["providers"]}
    assert list(data) == ["claude", "gemini", "openai", "openai-compatible", "cli"]
    assert data["claude"]["default_model"] == "claude-haiku-5-5" and data["claude"]["requires_api_key"] is True
    assert data["openai-compatible"]["requires_base_url"] is True
    assert data["cli"]["cli_commands"] == list(settings.ai_cli_allowlist)
    assert r.json()["cli_notice_key"] == "settings.ai.cli_runs_on_backend_host"


async def test_ai_get_when_not_configured(http, tenant):
    r = await http.get(f"{API}/settings/ai", headers=tenant.headers)
    assert r.status_code == 200 and r.json()["configured"] is False and r.json()["api_key_set"] is False


async def test_ai_key_encrypted_and_masked(http, tenant, db_session):
    key = "sk-ant-api03-SUPERSECRETKEYVALUE1234"
    body = {"provider": "claude", "model": "claude-haiku-5-5", "mode": "api", "api_key": key, "monthly_call_cap": 500}
    put = await http.put(f"{API}/settings/ai", headers=tenant.headers, json=body)
    assert put.status_code == 200, put.text
    for response in (put.json(), (await http.get(f"{API}/settings/ai", headers=tenant.headers)).json()):
        assert "api_key" not in response and "api_key_enc" not in response
        assert response["api_key_set"] is True and key not in str(response)
        assert response["api_key_masked"] == "********"
        assert response["provider"] == "claude" and response["monthly_call_cap"] == 500

    stored = await row(db_session, "SELECT * FROM org_ai_settings WHERE org_id = :o", o=tenant.org.id)
    assert key not in stored["api_key_enc"] and security.decrypt_field(stored["api_key_enc"]) == key

    # omit the key: kept
    again = await http.put(f"{API}/settings/ai", headers=tenant.headers, json={k: v for k, v in body.items() if k != "api_key"})
    assert again.json()["api_key_set"] is True
    audits = await audit_rows(db_session, tenant.org.id, "AI_SETTINGS_UPDATED")
    assert len(audits) == 2 and key not in str(audits)
    assert audits[0]["after"]["api_key_enc"] == "[REDACTED]"


async def test_ai_key_dropped_when_provider_or_base_url_changes(http, tenant, db_session):
    await http.put(
        f"{API}/settings/ai",
        headers=tenant.headers,
        json={"provider": "claude", "model": "claude-haiku-5-5", "mode": "api", "api_key": "sk-first-key-9999"},
    )
    # switching provider without a new key must not send the old key to a different vendor
    r = await http.put(
        f"{API}/settings/ai", headers=tenant.headers, json={"provider": "openai", "model": "gpt-5-mini", "mode": "api"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["fields"][0] == {"field": "api_key", "code": "required"}
    # compatible endpoint: key optional, but old key still must not carry over
    ok = await http.put(
        f"{API}/settings/ai",
        headers=tenant.headers,
        json={"provider": "openai-compatible", "model": "llama", "mode": "api", "base_url": "http://llm.internal:8000/v1"},
    )
    assert ok.status_code == 200 and ok.json()["api_key_set"] is False
    stored = await row(db_session, "SELECT api_key_enc FROM org_ai_settings WHERE org_id = :o", o=tenant.org.id)
    assert stored["api_key_enc"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "skynet", "model": "m", "mode": "api", "api_key": "k"},
        {"provider": "claude", "model": "", "mode": "api", "api_key": "k"},
        {"provider": "claude", "model": "m", "mode": "api"},  # key required on first save
        {"provider": "claude", "model": "m", "mode": "web", "api_key": "k"},
        {"provider": "openai-compatible", "model": "m", "mode": "api"},  # base_url required
        {"provider": "openai-compatible", "model": "m", "mode": "api", "base_url": "ftp://x"},
        {"provider": "openai-compatible", "model": "m", "mode": "api", "base_url": "http://u:p@host/v1"},
        {"provider": "claude", "model": "m", "mode": "api", "api_key": "k", "monthly_call_cap": 0},
        {"provider": "claude", "model": "m", "mode": "api", "api_key": "k", "monthly_call_cap": 10_000_001},
        {"provider": "claude", "model": "m", "mode": "api", "api_key": "k", "api_key_enc": "x"},
    ],
)
async def test_ai_settings_rejects_bad_input(http, tenant, body):
    r = await http.put(f"{API}/settings/ai", headers=tenant.headers, json=body)
    assert r.status_code == 422, body
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_cli_mode_rejects_command_not_in_allowlist(http, tenant, db_session):
    bad = await http.put(
        f"{API}/settings/ai",
        headers=tenant.headers,
        json={"provider": "cli", "mode": "cli", "cli_command_key": "rm -rf /"},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["details"]["fields"] == [{"field": "cli_command_key", "code": "not_in_allowlist"}]
    other = await http.put(
        f"{API}/settings/ai",
        headers=tenant.headers,
        json={"provider": "cli", "mode": "cli", "cli_command_key": "bash"},
    )
    assert other.status_code == 422

    missing = await http.put(f"{API}/settings/ai", headers=tenant.headers, json={"provider": "cli", "mode": "cli"})
    assert missing.status_code == 422

    good = await http.put(
        f"{API}/settings/ai",
        headers=tenant.headers,
        json={"provider": "cli", "mode": "cli", "cli_command_key": "claude"},
    )
    assert good.status_code == 200, good.text
    assert good.json()["cli_command_key"] == "claude" and good.json()["mode"] == "cli"
    stored = await row(db_session, "SELECT * FROM org_ai_settings WHERE org_id = :o", o=tenant.org.id)
    assert stored["cli_command_key"] == "claude" and stored["api_key_enc"] is None and stored["base_url"] is None


async def test_cli_allowlist_follows_settings(http, tenant, monkeypatch):
    monkeypatch.setattr(settings, "ai_cli_allowlist", ["claude", "codex"])
    r = await http.put(
        f"{API}/settings/ai", headers=tenant.headers, json={"provider": "cli", "mode": "cli", "cli_command_key": "codex"}
    )
    assert r.status_code == 200
    monkeypatch.setattr(settings, "ai_cli_allowlist", ["claude"])
    r = await http.put(
        f"{API}/settings/ai", headers=tenant.headers, json={"provider": "cli", "mode": "cli", "cli_command_key": "codex"}
    )
    assert r.status_code == 422


async def test_ai_connection_test_uses_provider_and_records_verification(http, tenant, db_session, mock_ai):
    nothing = await http.post(f"{API}/settings/ai/test", headers=tenant.headers)
    assert nothing.status_code == 409 and nothing.json()["error"]["code"] == "AI_NOT_CONFIGURED"

    await http.put(
        f"{API}/settings/ai",
        headers=tenant.headers,
        json={"provider": "gemini", "model": "gemini-flash", "mode": "api", "api_key": "AIza-secret-0000"},
    )
    ok = await http.post(f"{API}/settings/ai/test", headers=tenant.headers)
    assert ok.status_code == 200 and ok.json()["ok"] is True and ok.json()["verified_at"]
    saved = await row(db_session, "SELECT verified_at FROM org_ai_settings WHERE org_id = :o", o=tenant.org.id)
    assert saved["verified_at"] is not None

    mock_ai.connected = False
    failed = await http.post(f"{API}/settings/ai/test", headers=tenant.headers)
    assert failed.status_code == 200 and failed.json()["ok"] is False and failed.json()["reason"] == "connection_failed"

    # changing the model invalidates the verification
    changed = await http.put(
        f"{API}/settings/ai", headers=tenant.headers, json={"provider": "gemini", "model": "gemini-pro", "mode": "api"}
    )
    assert changed.json()["verified_at"] is None


async def test_ai_connection_test_provider_exception_does_not_leak(http, tenant, db_session):
    set_provider_override(MockProvider(RuntimeError("boom sk-live-1234 leaked")))

    class Bad(MockProvider):
        async def test_connection(self):
            raise RuntimeError("401 invalid key sk-live-1234")

    set_provider_override(Bad())
    await http.put(
        f"{API}/settings/ai",
        headers=tenant.headers,
        json={"provider": "openai", "model": "gpt-5-mini", "mode": "api", "api_key": "sk-live-1234"},
    )
    r = await http.post(f"{API}/settings/ai/test", headers=tenant.headers)
    assert r.status_code == 200 and r.json()["ok"] is False and "sk-live" not in r.text


async def test_get_provider_config_for_m14(db_session, http, tenant):
    from app.modules.m04_org_settings.service import AiSettingsService

    assert await AiSettingsService(db_session).get_provider_config(tenant.org.id) is None
    await http.put(
        f"{API}/settings/ai",
        headers=tenant.headers,
        json={"provider": "claude", "model": "claude-haiku-5-5", "mode": "api", "api_key": "sk-ant-zzzz"},
    )
    await db_session.rollback()
    config = await AiSettingsService(db_session).get_provider_config(tenant.org.id)
    assert config.provider == "claude" and config.api_key == "sk-ant-zzzz" and config.model == "claude-haiku-5-5"
    assert "sk-ant-zzzz" not in repr(config)


async def test_ai_usage_meter(http, tenant, other_tenant, db_session):
    from sqlalchemy import text

    await http.put(
        f"{API}/settings/ai",
        headers=tenant.headers,
        json={"provider": "claude", "model": "claude-haiku-5-5", "mode": "api", "api_key": "k-1", "monthly_call_cap": 10},
    )
    rows = [
        (tenant.org.id, "question_gen", 100, 50, "now()"),
        (tenant.org.id, "scoring", 10, 5, "now()"),
        (tenant.org.id, "scoring", 7, 3, "now()"),
        (tenant.org.id, "scoring", 999, 999, "now() - interval '70 days'"),  # previous month
        (other_tenant.org.id, "scoring", 1, 1, "now()"),
    ]
    for org_id, feature, tin, tout, when in rows:
        await db_session.execute(
            text(
                "INSERT INTO ai_usage_log (id, org_id, feature, provider, model, status, tokens_in, tokens_out, created_at) "
                f"VALUES (gen_random_uuid(), :o, :f, 'claude', 'm', 'ok', :i, :t, {when})"
            ),
            {"o": org_id, "f": feature, "i": tin, "t": tout},
        )
    await db_session.commit()
    r = await http.get(f"{API}/ai-usage", headers=tenant.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["calls"] == 3 and body["tokens_in"] == 117 and body["tokens_out"] == 58
    assert body["by_feature"] == {"question_gen": 1, "scoring": 2}
    assert body["monthly_call_cap"] == 10 and body["remaining"] == 7 and len(body["month"]) == 7
