"""Request and response models for M4. Secrets are write-only: responses carry only a mask."""

import ipaddress
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictBool, field_validator

from app.core.branding_text import BACKGROUND_STYLES, FONT_KEYS
from app.core.config import settings
from app.modules.m04_org_settings.contrast import normalize_hex

_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _no_crlf(value: str | None) -> str | None:
    if value is not None and re.search(r"[\r\n\x00]", value):
        raise ValueError("control characters not allowed")
    return value


# -------------------- profile, security, retention, invites


class ProfileIn(Strict):
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    locale: str | None = None
    timezone: str | None = None

    @field_validator("locale")
    @classmethod
    def _locale(cls, v: str | None) -> str | None:
        if v is not None and not _LOCALE.match(v):
            raise ValueError("invalid locale")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is not None and v not in available_timezones():
            raise ValueError("unknown timezone")
        return v


class ProfileOut(Out):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [{"name": "Acme Hiring", "slug": "acme", "locale": "en", "timezone": "UTC"}]
        },
    )
    name: str
    slug: str
    locale: str
    timezone: str


class PasswordPolicyIn(Strict):
    min_length: Annotated[int, Field(strict=True, ge=12, le=128)] | None = None
    history: Annotated[int, Field(strict=True, ge=5, le=24)] | None = None
    blocklist: StrictBool | None = None

    @field_validator("blocklist")
    @classmethod
    def _blocklist_on(cls, v: bool | None) -> bool | None:
        if v is False:
            raise ValueError("the common-password blocklist cannot be turned off")
        return v


class PasswordPolicyOut(BaseModel):
    min_length: int = 12
    history: int = 5
    blocklist: bool = True


class SecurityIn(Strict):
    allowed_google_domains: Annotated[list[str], Field(max_length=50)] | None = None
    session_timeout_minutes: Annotated[int, Field(strict=True, ge=5, le=480)] | None = None
    password_policy: PasswordPolicyIn | None = None
    totp_required_for_admins: StrictBool | None = None

    @field_validator("allowed_google_domains")
    @classmethod
    def _domains(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        cleaned = list(dict.fromkeys(d.strip().lower() for d in v))
        if any(not _DOMAIN.match(d) for d in cleaned):
            raise ValueError("invalid domain")
        return cleaned


class SecurityOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "allowed_google_domains": ["example.com"],
                    "session_timeout_minutes": 30,
                    "password_policy": {"min_length": 12, "history": 5, "blocklist": True},
                    "totp_required_for_admins": False,
                }
            ]
        }
    )
    allowed_google_domains: list[str]
    session_timeout_minutes: int
    password_policy: PasswordPolicyOut
    totp_required_for_admins: bool


class RetentionIn(Strict):
    retention_days: Annotated[int, Field(strict=True, ge=7, le=730)]


class RetentionOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"retention_days": 90}]})
    retention_days: int


class InvitesIn(Strict):
    invite_default_expiry_hours: Annotated[int, Field(strict=True, ge=1, le=720)]


class InvitesOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"invite_default_expiry_hours": 72}]})
    invite_default_expiry_hours: int


# --------------------------------------------------------------------------- email


def check_host(host: str, *, strict: bool) -> str:
    """Hostname or IP literal. ``strict`` (prod) also refuses loopback, private and link-local literals."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME.match(host):
            raise ValueError("invalid host") from None
        if strict and host.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError("internal host not allowed") from None
        return host
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        raise ValueError("address not allowed")
    if strict and (ip.is_loopback or ip.is_private or ip.is_reserved):
        raise ValueError("internal address not allowed")
    return host


class EmailSettingsIn(Strict):
    host: Annotated[str, Field(min_length=1, max_length=253)]
    port: Annotated[int, Field(strict=True, ge=1, le=65535)]
    security: Literal["none", "starttls", "tls"] = "starttls"
    username: Annotated[str, Field(max_length=255)] | None = None
    password: Annotated[str, Field(max_length=1024)] | None = Field(
        None, description="Write-only. Omit to keep the stored password, send an empty string to clear it."
    )
    sender_name: Annotated[str, Field(max_length=200)] | None = None
    sender_email: EmailStr
    enabled: StrictBool = False

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return check_host(v, strict=settings.app_env == "prod")

    @field_validator("sender_name", "username")
    @classmethod
    def _crlf(cls, v: str | None) -> str | None:
        return _no_crlf(v)


class EmailSettingsOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "configured": True,
                    "host": "smtp.example.com",
                    "port": 587,
                    "security": "starttls",
                    "username": "mailer",
                    "password_set": True,
                    "password_masked": "********",
                    "sender_name": "Acme HR",
                    "sender_email": "hr@example.com",
                    "enabled": True,
                    "verified_at": None,
                }
            ]
        }
    )
    configured: bool
    host: str | None = None
    port: int | None = None
    security: str | None = None
    username: str | None = None
    password_set: bool = False
    password_masked: str | None = None
    sender_name: str | None = None
    sender_email: str | None = None
    enabled: bool = False
    verified_at: datetime | None = None


class EmailTestIn(Strict):
    to: EmailStr


class EmailTestOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"sent": True, "verified_at": "2026-11-16T10:00:00Z", "reason": None}]
        }
    )
    sent: bool
    verified_at: datetime | None = None
    reason: str | None = Field(
        None, description="Machine code when `sent` is false, for example `smtp_send_failed`."
    )


# --------------------------------------------------------------------------- AI


class AiSettingsIn(Strict):
    provider: Literal["claude", "gemini", "openai", "openai-compatible", "cli"]
    model: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    mode: Literal["api", "cli"] = "api"
    base_url: Annotated[str, Field(max_length=2048)] | None = None
    api_key: Annotated[str, Field(min_length=1, max_length=4096)] | None = Field(
        None, description="Write-only. Omit to keep the stored key."
    )
    cli_command_key: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    monthly_call_cap: Annotated[int, Field(strict=True, ge=1, le=10_000_000)] | None = None

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from urllib.parse import urlsplit

        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("invalid base url")
        check_host(parts.hostname, strict=False)
        return v


class AiSettingsOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "configured": True,
                    "provider": "claude",
                    "model": "claude-haiku-5-5",
                    "mode": "api",
                    "base_url": None,
                    "api_key_set": True,
                    "api_key_masked": "********",
                    "cli_command_key": None,
                    "monthly_call_cap": 500,
                    "verified_at": None,
                }
            ]
        }
    )
    configured: bool
    provider: str | None = None
    model: str | None = None
    mode: str | None = None
    base_url: str | None = None
    api_key_set: bool = False
    api_key_masked: str | None = None
    cli_command_key: str | None = None
    monthly_call_cap: int | None = None
    verified_at: datetime | None = None


class AiTestOut(BaseModel):
    ok: bool
    verified_at: datetime | None = None
    reason: str | None = None


class AiProvidersOut(BaseModel):
    providers: list[dict[str, Any]]
    cli_notice_key: str = "settings.ai.cli_runs_on_backend_host"


class AiUsageOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "month": "2026-11",
                    "calls": 3,
                    "tokens_in": 117,
                    "tokens_out": 58,
                    "by_feature": {"question_gen": 1, "scoring": 2},
                    "monthly_call_cap": 10,
                    "remaining": 7,
                }
            ]
        }
    )
    month: str
    calls: int
    tokens_in: int
    tokens_out: int
    by_feature: dict[str, int]
    monthly_call_cap: int | None
    remaining: int | None


# --------------------------------------------------------------------------- branding


def _hex(v: str | None) -> str | None:
    return None if v is None else normalize_hex(v)


class BrandingIn(Strict):
    primary_color: str | None = None
    accent_color: str | None = None
    background_style: Literal[*BACKGROUND_STYLES] | None = None  # type: ignore[valid-type]
    font_key: Literal[*FONT_KEYS] | None = None  # type: ignore[valid-type]
    logo_asset_id: uuid.UUID | None = Field(
        None, description="Only `null` (remove the logo) or an asset of this organization."
    )
    favicon_asset_id: uuid.UUID | None = None

    @field_validator("primary_color", "accent_color")
    @classmethod
    def _colors(cls, v: str | None) -> str | None:
        return _hex(v)


class BrandingValidateIn(Strict):
    primary_color: str | None = None
    accent_color: str | None = None

    @field_validator("primary_color", "accent_color")
    @classmethod
    def _colors(cls, v: str | None) -> str | None:
        return _hex(v)


class BrandingOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "primary_color": "#1B2A4A",
                    "accent_color": "#F26B5B",
                    "background_style": "mint",
                    "font_key": "inter",
                    "logo_url": None,
                    "favicon_url": None,
                    "is_customized": False,
                    "status_colors": {"success": "#1E7B3A", "warning": "#8A5A00", "error": "#B3261E"},
                    "contrast": {"primary": {"ratio": 14.1, "minimum": 4.5, "passes": True}},
                }
            ]
        }
    )
    primary_color: str
    accent_color: str
    background_style: str
    font_key: str
    logo_url: str | None
    favicon_url: str | None
    is_customized: bool
    status_colors: dict[str, str]
    contrast: dict[str, dict[str, Any]]


class BrandingValidateOut(BaseModel):
    valid: bool
    checks: dict[str, dict[str, Any]]


class FontsOut(BaseModel):
    fonts: list[dict[str, str]]
    background_styles: list[str]


# --------------------------------------------------------------------------- documents

DocType = Literal["privacy", "terms", "consent", "other"]
Title = Annotated[str, Field(min_length=1, max_length=300)]


class DocumentCreate(Strict):
    doc_type: DocType
    title: Title


class DocumentPatch(Strict):
    title: Title | None = None
    doc_type: DocType | None = None


class DocumentOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "0192f6e0-0000-7000-8000-000000000001",
                    "doc_type": "privacy",
                    "title": "Privacy policy",
                    "version_count": 2,
                    "current_version_id": "0192f6e0-0000-7000-8000-000000000002",
                    "current_version_no": 1,
                    "created_at": "2026-11-16T10:00:00Z",
                    "updated_at": "2026-11-16T10:00:00Z",
                }
            ]
        }
    )
    id: uuid.UUID
    doc_type: str
    title: str
    version_count: int
    current_version_id: uuid.UUID | None
    current_version_no: int | None
    created_at: datetime
    updated_at: datetime


class VersionJsonIn(Strict):
    content_html: Annotated[str, Field(min_length=1, max_length=600_000)]


class VersionOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    status: str
    content_html: str | None
    pdf_url: str | None = None
    published_at: datetime | None
    published_by: uuid.UUID | None
    created_at: datetime


class VersionListOut(BaseModel):
    items: list[VersionOut]
    total: int
