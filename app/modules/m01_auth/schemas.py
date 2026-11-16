"""Request and response models for M1, with the examples Swagger shows."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ERROR_CODES

_UID = "3f0c2d9e-8d1b-4c53-9a77-0b8d2f4e6a11"
_ORG = "9b1d6c0a-5f2e-4a38-8c1d-2e7f3a9b4c55"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- errors (docs only)


class ErrorBody(BaseModel):
    code: str
    message_key: str
    details: dict[str, Any] = {}
    request_id: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def error_responses(*codes: str) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` for the given Appendix A codes, grouped by HTTP status with one
    example per code (several codes may share a status)."""
    grouped: dict[int, dict[str, Any]] = {}
    for code in codes:
        spec = ERROR_CODES[code]
        grouped.setdefault(spec.status, {})[code] = {
            "summary": code,
            "value": {
                "error": {
                    "code": spec.code,
                    "message_key": spec.message_key,
                    "details": {},
                    "request_id": "6f1c1b9e-2d7a-4c44-9c52-1f0f1d7a9a10",
                }
            },
        }
    return {
        status: {
            "model": ErrorEnvelope,
            "description": " / ".join(examples),
            "content": {"application/json": {"examples": examples}},
        }
        for status, examples in grouped.items()
    }


# --------------------------------------------------------------------------- requests


class LoginIn(_Model):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"email": "hr@example.com", "password": "correct horse battery"}]},
    )


class TotpLoginIn(_Model):
    totp_token: str = Field(min_length=10, max_length=512, description="Challenge from /auth/login")
    code: str = Field(min_length=6, max_length=32, description="6-digit code or a recovery code")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"totp_token": "eyJ1IjoiLi4uIn0.ab12", "code": "123456"}]},
    )


class GoogleLoginIn(_Model):
    id_token: str = Field(min_length=10, max_length=8192)

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"id_token": "eyJhbGciOi..."}]})


class ChangePasswordIn(_Model):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"current_password": "Temp-4f9!kQ2xZ7pL", "new_password": "a much longer passphrase"}
            ]
        },
    )


class TotpVerifyIn(_Model):
    code: str = Field(min_length=6, max_length=12, description="6-digit code from the authenticator app")

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"code": "123456"}]})


class TotpDisableIn(_Model):
    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=6, max_length=32, description="6-digit code or a recovery code")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"password": "my current password", "code": "123456"}]},
    )


# --------------------------------------------------------------------------- responses


class UserOut(_Model):
    id: UUID
    email: str
    name: str
    role: Literal["super_admin", "org_admin", "hr_user"]
    org_id: UUID | None
    locale: str | None
    must_change_password: bool
    totp_enabled: bool
    is_default_super_admin: bool
    last_login_at: datetime | None


class SessionOut(_Model):
    """Result of login, refresh and change-password. While ``totp_required`` is true there is no
    access token yet: send ``totp_token`` and the code to ``/auth/login/totp``."""

    access_token: str | None
    token_type: Literal["bearer"] = "bearer"
    expires_in: int | None = Field(description="Access token lifetime in seconds")
    must_change_password: bool
    totp_required: bool
    totp_token: str | None
    totp_setup_required: bool = Field(
        description="The organization requires an authenticator app for admins and this admin has none yet"
    )
    user: UserOut | None

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "access_token": "eyJhbGciOiJFZERTQSIsImtpZCI6ImsxIn0...",
                    "token_type": "bearer",
                    "expires_in": 900,
                    "must_change_password": False,
                    "totp_required": False,
                    "totp_token": None,
                    "totp_setup_required": False,
                    "user": {
                        "id": _UID,
                        "email": "hr@example.com",
                        "name": "Asha Rao",
                        "role": "hr_user",
                        "org_id": _ORG,
                        "locale": "en",
                        "must_change_password": False,
                        "totp_enabled": False,
                        "is_default_super_admin": False,
                        "last_login_at": "2026-11-16T09:30:00Z",
                    },
                }
            ]
        },
    )


class OrganizationOut(_Model):
    id: UUID
    name: str
    slug: str
    status: Literal["active", "disabled"]
    locale: str
    timezone: str
    session_timeout_minutes: int
    totp_required_for_admins: bool


class BrandingRef(_Model):
    scope: Literal["platform", "org"]
    endpoint: str = Field(description="Where the client reads the theme for this user")


class MeOut(_Model):
    user: UserOut
    organization: OrganizationOut | None
    flags: dict[str, bool] = Field(description="Effective feature flags (hidden flags only for super admins)")
    locale: str
    branding: BrandingRef
    session_timeout_minutes: int
    totp_setup_required: bool

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "user": {
                        "id": _UID,
                        "email": "hr@example.com",
                        "name": "Asha Rao",
                        "role": "hr_user",
                        "org_id": _ORG,
                        "locale": "en",
                        "must_change_password": False,
                        "totp_enabled": False,
                        "is_default_super_admin": False,
                        "last_login_at": "2026-11-16T09:30:00Z",
                    },
                    "organization": {
                        "id": _ORG,
                        "name": "Example Org",
                        "slug": "example-org",
                        "status": "active",
                        "locale": "en",
                        "timezone": "UTC",
                        "session_timeout_minutes": 30,
                        "totp_required_for_admins": False,
                    },
                    "flags": {"roles": True, "assessments": True, "invites": True},
                    "locale": "en",
                    "branding": {"scope": "org", "endpoint": "/api/v1/org/branding"},
                    "session_timeout_minutes": 30,
                    "totp_setup_required": False,
                }
            ]
        },
    )


class OkOut(_Model):
    ok: bool = True


class TotpSetupOut(_Model):
    secret: str = Field(description="Base32 secret for manual entry")
    otpauth_uri: str = Field(description="Feed this to a QR code on the client")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "secret": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
                    "otpauth_uri": "otpauth://totp/App:admin%40example.com?secret=JBSWY3DP&issuer=App",
                }
            ]
        },
    )


class TotpVerifyOut(_Model):
    enabled: bool
    recovery_codes: list[str] = Field(description="Shown once. Each works once if the phone is lost.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"enabled": True, "recovery_codes": ["a1b2c-3d4e5", "f6a7b-8c9d0"]}]},
    )
