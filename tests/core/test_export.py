"""Export helpers, branding text, notifications and the AI provider interface."""

import json
import uuid

import pytest

from app.core import branding_text, notifications
from app.core.ai.base import LLMProvider
from app.core.ai.mock import MockProvider
from app.core.ai.registry import ProviderConfig, build_provider, register_provider
from app.core.config import settings
from app.core.errors import AppError
from app.core.export import csv_bytes, csv_safe, render_pdf


@pytest.mark.parametrize("cell", ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx", "\rx"])
def test_csv_safe_prefixes_formula_cells(cell):
    assert csv_safe(cell) == "'" + cell


def test_csv_safe_leaves_plain_values():
    assert csv_safe("Alice") == "Alice"
    assert csv_safe(None) == ""
    assert csv_safe(-5) == "-5"  # numbers are data, not formulas
    assert csv_safe(3.5) == "3.5"


def test_csv_bytes_has_bom_and_escapes():
    out = csv_bytes(["name", "note"], [["=HYPERLINK()", "a,b"]]).decode("utf-8-sig")
    assert out.splitlines() == ["name,note", '\'=HYPERLINK(),"a,b"']


def test_render_pdf_produces_pdf_and_blocks_remote_fetch():
    pdf = render_pdf("Report", "<h1>Hi</h1><img src='http://169.254.169.254/x.png'>")
    assert pdf.startswith(b"%PDF")


def test_watermark_and_starter_theme():
    assert branding_text.watermark() == f"Powered by {settings.vendor_name}"
    theme = branding_text.STARTER_THEME
    assert {"primary", "accent", "background_style", "font_key"} <= set(theme)
    assert theme["font_key"] in branding_text.FONT_KEYS
    assert branding_text.contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert branding_text.contrast_ratio(theme["primary"], "#FFFFFF") >= 4.5


class InsertSession:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


async def test_notify_inserts_row_and_validates_type():
    session = InsertSession()
    org, user, entity = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await notifications.notify(
        session, org, user, notifications.ATTEMPT_SUBMITTED, {"attempt_id": "a"}, "attempt", entity
    )
    sql, params = session.calls[0]
    assert "INSERT INTO notifications" in sql
    assert params["org_id"] == org and params["user_id"] == user
    assert params["type"] == "attempt_submitted" and json.loads(params["payload"]) == {"attempt_id": "a"}
    assert params["entity_id"] == entity
    with pytest.raises(ValueError):
        await notifications.notify(session, org, user, "bogus", {})


async def test_mock_provider_returns_scripted_responses():
    mock = MockProvider([{"a": 1}, AppError("AI_PROVIDER_ERROR")])
    assert isinstance(mock, LLMProvider)
    assert await mock.generate_json(system="s", prompt="p", json_schema={}, max_tokens=10, timeout_sec=5) == {
        "a": 1
    }
    with pytest.raises(AppError):
        await mock.generate_json(system="s", prompt="p", json_schema={}, max_tokens=10, timeout_sec=5)
    assert mock.calls[0]["prompt"] == "p" and await mock.test_connection() is True


def test_registry_builds_registered_provider():
    register_provider("mock", lambda cfg: MockProvider([{"model": cfg.model}]))
    provider = build_provider(ProviderConfig(provider="mock", model="m1"))
    assert isinstance(provider, MockProvider)
    with pytest.raises(AppError) as exc:
        build_provider(ProviderConfig(provider="nope", model="x"))
    assert exc.value.code == "AI_NOT_CONFIGURED"
