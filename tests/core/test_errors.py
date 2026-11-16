import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from app.core.errors import ERROR_CODES, AppError, install_error_handlers
from app.core.middleware import RequestIDMiddleware, safe_request_id

APPENDIX_A = """UNAUTHENTICATED FORBIDDEN NOT_FOUND VALIDATION_ERROR CONFLICT RATE_LIMITED
INVALID_CREDENTIALS ACCOUNT_LOCKED MUST_CHANGE_PASSWORD PASSWORD_POLICY_FAILED ORG_DISABLED
FEATURE_DISABLED ADMIN_ONLY_ACTION LAST_ADMIN_PROTECTED PROTECTED_ACCOUNT ASSESSMENT_IMMUTABLE
ASSESSMENT_NOT_PUBLISHED VERSION_LIMIT_REACHED_ALL_IN_USE DRAFT_ALREADY_EXISTS QUESTION_INVALID
MODEL_ANSWER_REQUIRED ROLE_NOT_OPEN ROLE_HAS_NO_ASSESSMENT ROLE_HAS_INVITES DUPLICATE_INVITE
INVITE_EXPIRED INVITE_REVOKED LINK_BOUND_TO_OTHER_SESSION DEVICE_NOT_SUPPORTED CONSENT_REQUIRED
CAMERA_REQUIRED HELP_REQUEST_PENDING EXAM_NOT_STARTED EXAM_ALREADY_SUBMITTED EXAM_TIME_OVER
DECISION_ALREADY_MADE AI_NOT_CONFIGURED AI_LIMIT_REACHED AI_PROVIDER_ERROR AI_OUTPUT_INVALID
REPORT_FILTER_INVALID BRANDING_CONTRAST_TOO_LOW SMTP_NOT_CONFIGURED UPLOAD_INVALID""".split()


def test_error_catalog_is_exactly_appendix_a():
    assert set(ERROR_CODES) == set(APPENDIX_A)
    assert len(APPENDIX_A) == 44
    for code, spec in ERROR_CODES.items():
        assert re.fullmatch(r"errors\.[a-z_]+\.[a-z_]+", spec.message_key), code
        assert 400 <= spec.status < 600
    assert ERROR_CODES["DUPLICATE_INVITE"].message_key == "errors.invite.duplicate"
    assert ERROR_CODES["FEATURE_DISABLED"].status == 403


def test_app_error_rejects_unknown_code():
    with pytest.raises(ValueError):
        AppError("NOT_A_REAL_CODE", 400)
    err = AppError("CONFLICT")
    assert err.status == 409 and err.details == {}


class Body(BaseModel):
    name: str = Field(min_length=2)
    age: int


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestIDMiddleware)

    @app.get("/dup")
    async def dup():
        raise AppError("DUPLICATE_INVITE", 409, {"existing_invite_id": "abc"})

    @app.post("/body")
    async def body(payload: Body):
        return payload

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internals")

    return app


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t")


async def test_error_envelope_shape_and_request_id():
    async with await _client(_app()) as http:
        resp = await http.get("/dup", headers={"X-Request-ID": "req-123"})
    assert resp.status_code == 409
    assert resp.headers["X-Request-ID"] == "req-123"
    assert resp.json() == {
        "error": {
            "code": "DUPLICATE_INVITE",
            "message_key": "errors.invite.duplicate",
            "details": {"existing_invite_id": "abc"},
            "request_id": "req-123",
        }
    }


async def test_validation_error_lists_fields():
    async with await _client(_app()) as http:
        resp = await http.post("/body", json={"name": "x"})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["message_key"] == ERROR_CODES["VALIDATION_ERROR"].message_key
    fields = {f["field"]: f["code"] for f in err["details"]["fields"]}
    assert fields == {"name": "too_short", "age": "required"}
    assert err["request_id"] == resp.headers["X-Request-ID"]


async def test_unknown_route_and_unhandled_error_use_envelope():
    async with await _client(_app()) as http:
        missing = await http.get("/nope")
        boom = await http.get("/boom")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert boom.status_code == 500
    assert "secret internals" not in boom.text
    assert boom.json()["error"]["request_id"] == boom.headers["X-Request-ID"]


@pytest.mark.parametrize("bad", ["abc\n", "abc\r\nSet-Cookie: x=1", "réq-ü", "ａｂｃ", "a" * 65, ""])
def test_safe_request_id_rejects_newlines_and_non_ascii(bad):
    rid = safe_request_id(bad)
    assert rid != bad and re.fullmatch(r"[0-9a-f-]{36}", rid)
    assert safe_request_id("req-123.A_b") == "req-123.A_b"


@pytest.mark.parametrize("bad", ["abc\n", "abc\r\nSet-Cookie: x=1", "réq-ü", "a" * 65])
async def test_request_id_middleware_replaces_unsafe_incoming_ids(bad):
    async with await _client(_app()) as http:
        resp = await http.get("/dup", headers=[(b"x-request-id", bad.encode("utf-8"))])
    rid = resp.headers["X-Request-ID"]
    assert rid != bad and re.fullmatch(r"[0-9a-f-]{36}", rid)
    assert "set-cookie" not in resp.headers
