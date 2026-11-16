"""M4 business rules. Every mutation audits in the caller's transaction (never commits).

Secrets (SMTP password, AI key) are encrypted with ``core.security.encrypt_field``, never returned
(only a mask) and kept out of audit values by the ``*_enc`` redaction rule.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, security
from app.core.ai import ProviderConfig, build_provider
from app.core.branding_text import BACKGROUND_STYLES, FONT_KEYS, STARTER_THEME
from app.core.config import settings
from app.core.db import TenantContext
from app.core.email import OutgoingEmail, SmtpConfig, get_email_sender
from app.core.errors import AppError, validation_error
from app.core.ratelimit import check_rate_limit
from app.core.storage import get_storage
from app.db import Asset, BrandingTheme, Document, DocumentVersion, OrgAiSettings, OrgEmailSettings
from app.modules.m04_org_settings import catalog, repo, schemas
from app.modules.m04_org_settings.contrast import MIN_RATIO, check_text_contrast
from app.modules.m04_org_settings.sanitize import sanitize_html
from app.modules.m04_org_settings.uploads import inspect_image, inspect_pdf, read_limited

logger = logging.getLogger(__name__)

EMAIL_TEST_LIMIT = "5/minute"  # per organization; a module constant so tests can lower it
DEFAULT_POLICY = {"min_length": 12, "history": 5, "blocklist": True}
# Fixed status colours (spec 6/M4): not part of the theme and not overridable.
STATUS_COLORS = {"success": "#1E7B3A", "warning": "#8A5A00", "error": "#B3261E"}
FONT_LABELS = {
    "inter": ("Inter", "sans"),
    "source_sans_3": ("Source Sans 3", "sans"),
    "nunito_sans": ("Nunito Sans", "sans"),
    "ibm_plex_sans": ("IBM Plex Sans", "sans"),
    "lato": ("Lato", "sans"),
    "merriweather": ("Merriweather", "serif"),
}
SMTP_SECURITY = {"none": "none", "starttls": "starttls", "tls": "ssl"}  # DB value -> core.email value


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


async def _org(session: AsyncSession, ctx: TenantContext) -> Any:
    org = await repo.get_org(session, _org_id(ctx), for_update=True)
    if org is None:
        raise AppError("NOT_FOUND")
    return org


def _org_id(ctx: TenantContext) -> UUID:
    if ctx.org_id is None:  # guards only let organization admins through; defensive
        raise AppError("FORBIDDEN")
    return ctx.org_id


# =========================================================================== profile, security, retention, invites


class OrgSettingsService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _apply(self, ctx: TenantContext, org: Any, changes: dict[str, Any], audit_names: dict[str, str] | None = None) -> None:
        """Set ``changes`` on ``org`` and audit only the fields whose value changed."""
        names = audit_names or {}
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for field, value in changes.items():
            old = getattr(org, field)
            if old == value:
                continue
            before[names.get(field, field)] = old
            after[names.get(field, field)] = value
            setattr(org, field, value)
        if after:
            await self.session.flush()
            await audit.record(ctx, self.session, audit.SETTINGS_UPDATED, "organization", org.id, before=before, after=after)

    # ---- profile
    async def get_profile(self, ctx: TenantContext) -> Any:
        org = await repo.get_org(self.session, _org_id(ctx))
        if org is None:
            raise AppError("NOT_FOUND")
        return schemas.ProfileOut.model_validate(org)

    async def update_profile(self, ctx: TenantContext, body: schemas.ProfileIn) -> Any:
        org = await _org(self.session, ctx)
        await self._apply(ctx, org, body.model_dump(exclude_none=True))
        return schemas.ProfileOut.model_validate(org)

    # ---- security
    @staticmethod
    def _security_out(org: Any) -> schemas.SecurityOut:
        return schemas.SecurityOut(
            allowed_google_domains=list(org.allowed_google_domains or []),
            session_timeout_minutes=org.session_timeout_minutes,
            password_policy=schemas.PasswordPolicyOut(**{**DEFAULT_POLICY, **(org.password_policy or {})}),
            totp_required_for_admins=org.totp_required_for_admins,
        )

    async def get_security(self, ctx: TenantContext) -> schemas.SecurityOut:
        org = await repo.get_org(self.session, _org_id(ctx))
        if org is None:
            raise AppError("NOT_FOUND")
        return self._security_out(org)

    async def update_security(self, ctx: TenantContext, body: schemas.SecurityIn) -> schemas.SecurityOut:
        org = await _org(self.session, ctx)
        changes = body.model_dump(exclude_none=True, exclude={"password_policy"})
        if body.password_policy is not None:
            merged = {**DEFAULT_POLICY, **(org.password_policy or {}), **body.password_policy.model_dump(exclude_none=True)}
            changes["password_policy"] = merged
        # `pw_policy`: an audit key named `password_policy` would be redacted by the `password*` rule.
        await self._apply(ctx, org, changes, {"password_policy": "pw_policy"})
        return self._security_out(org)

    # ---- retention and invites
    async def get_retention(self, ctx: TenantContext) -> schemas.RetentionOut:
        org = await repo.get_org(self.session, _org_id(ctx))
        if org is None:
            raise AppError("NOT_FOUND")
        return schemas.RetentionOut(retention_days=org.retention_days)

    async def update_retention(self, ctx: TenantContext, body: schemas.RetentionIn) -> schemas.RetentionOut:
        org = await _org(self.session, ctx)
        await self._apply(ctx, org, {"retention_days": body.retention_days})
        return schemas.RetentionOut(retention_days=org.retention_days)

    async def get_invites(self, ctx: TenantContext) -> schemas.InvitesOut:
        org = await repo.get_org(self.session, _org_id(ctx))
        if org is None:
            raise AppError("NOT_FOUND")
        return schemas.InvitesOut(invite_default_expiry_hours=org.invite_default_expiry_hours)

    async def update_invites(self, ctx: TenantContext, body: schemas.InvitesIn) -> schemas.InvitesOut:
        org = await _org(self.session, ctx)
        await self._apply(ctx, org, {"invite_default_expiry_hours": body.invite_default_expiry_hours})
        return schemas.InvitesOut(invite_default_expiry_hours=org.invite_default_expiry_hours)


# =========================================================================== email


class EmailSettingsService:
    FIELDS = ("host", "port", "security", "username", "sender_name", "sender_email", "enabled")

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _out(row: OrgEmailSettings | None) -> schemas.EmailSettingsOut:
        if row is None:
            return schemas.EmailSettingsOut(configured=False)
        return schemas.EmailSettingsOut(
            configured=True,
            host=row.host,
            port=row.port,
            security=row.security,
            username=row.username,
            password_set=bool(row.password_enc),
            password_masked=security.mask_secret(row.password_enc),
            sender_name=row.sender_name,
            sender_email=row.sender_email,
            enabled=row.enabled,
            verified_at=row.verified_at,
        )

    async def get(self, ctx: TenantContext) -> schemas.EmailSettingsOut:
        return self._out(await repo.get_email(self.session, _org_id(ctx)))

    async def put(self, ctx: TenantContext, body: schemas.EmailSettingsIn) -> schemas.EmailSettingsOut:
        org_id = _org_id(ctx)
        row = await repo.get_email(self.session, org_id)
        new = body.model_dump(include=set(self.FIELDS))
        new["sender_email"] = str(new["sender_email"])
        old_enc = row.password_enc if row else None
        if body.password is None:
            new_enc = old_enc
        else:
            new_enc = security.encrypt_field(body.password) if body.password else None
        before = {f: getattr(row, f) if row else None for f in self.FIELDS} | {"password_enc": old_enc}
        after = new | {"password_enc": new_enc}
        reset = row is None or any(getattr(row, f) != new[f] for f in ("host", "port", "security", "username")) or new_enc != old_enc
        if row is None:
            row = await repo.add(self.session, OrgEmailSettings(org_id=org_id, **new, password_enc=new_enc, updated_by=ctx.user_id))
        else:
            for field, value in new.items():
                setattr(row, field, value)
            row.password_enc = new_enc
            row.updated_by = ctx.user_id
        if reset:
            row.verified_at = None
        await self.session.flush()
        await audit.record(ctx, self.session, audit.EMAIL_SETTINGS_UPDATED, "org_email_settings", org_id, before=before, after=after)
        return self._out(row)

    async def send_test(self, ctx: TenantContext, to: str) -> schemas.EmailTestOut:
        org_id = _org_id(ctx)
        check_rate_limit(EMAIL_TEST_LIMIT, "email_test", "org", str(org_id))
        row = await repo.get_email(self.session, org_id)
        if row is None:
            raise AppError("SMTP_NOT_CONFIGURED")
        reason: str | None = None
        try:
            password = security.decrypt_field(row.password_enc) if row.password_enc else None
        except ValueError:
            password, reason = None, "credentials_unreadable"
        if reason is None:
            smtp = SmtpConfig(
                host=row.host,
                port=row.port,
                security=SMTP_SECURITY[row.security],  # type: ignore[arg-type]
                username=row.username,
                password=password,
                sender_email=row.sender_email,
                sender_name=row.sender_name,
            )
            message = OutgoingEmail(
                to=to,
                subject=f"{settings.app_name}: SMTP test",
                html=f"<p>This is a test message from {settings.app_name}. Your email settings work.</p>",
                text=f"This is a test message from {settings.app_name}. Your email settings work.",
            )
            try:
                await get_email_sender().send(smtp, message)
            except AppError:
                raise
            except Exception:  # transport errors can echo credentials or hosts: log type only
                logger.warning("smtp test send failed for org %s", org_id, exc_info=False)
                reason = "smtp_send_failed"
        if reason is None:
            row.verified_at = _now()
            await self.session.flush()
        await audit.record(
            ctx,
            self.session,
            audit.EMAIL_TEST_SENT,
            "org_email_settings",
            org_id,
            after={"to": to},
            result="success" if reason is None else "failure",
            reason=reason,
        )
        return schemas.EmailTestOut(sent=reason is None, verified_at=row.verified_at if reason is None else None, reason=reason)


# =========================================================================== AI


def _field_error(field: str, code: str) -> AppError:
    return validation_error([{"field": field, "code": code}])


class AiSettingsService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _out(row: OrgAiSettings | None) -> schemas.AiSettingsOut:
        if row is None:
            return schemas.AiSettingsOut(configured=False)
        return schemas.AiSettingsOut(
            configured=True,
            provider=row.provider,
            model=row.model,
            mode=row.mode,
            base_url=row.base_url,
            api_key_set=bool(row.api_key_enc),
            api_key_masked=security.mask_secret(row.api_key_enc),
            cli_command_key=row.cli_command_key,
            monthly_call_cap=row.monthly_call_cap,
            verified_at=row.verified_at,
        )

    async def get(self, ctx: TenantContext) -> schemas.AiSettingsOut:
        return self._out(await repo.get_ai(self.session, _org_id(ctx)))

    async def get_provider_config(self, org_id: UUID) -> ProviderConfig | None:
        """Interface for M14: the organization's provider config with the key decrypted, or None."""
        row = await repo.get_ai(self.session, org_id)
        if row is None:
            return None
        api_key = None
        if row.api_key_enc:
            try:
                api_key = security.decrypt_field(row.api_key_enc)
            except ValueError:
                logger.error("AI key for org %s cannot be decrypted", org_id)
                return None
        return ProviderConfig(
            provider=row.provider,
            model=row.model,
            mode=row.mode,
            base_url=row.base_url,
            api_key=api_key,
            cli_command_key=row.cli_command_key,
            timeout_sec=settings.ai_request_timeout_sec,
        )

    def _validate(self, body: schemas.AiSettingsIn, row: OrgAiSettings | None) -> tuple[str, str | None]:
        """Returns (model, key action) after checking the provider/mode rules. Raises VALIDATION_ERROR."""
        provider = body.provider
        if (provider == catalog.CLI) != (body.mode == "cli"):
            raise _field_error("mode", "invalid_choice")
        if body.mode == "cli":
            if not body.cli_command_key:
                raise _field_error("cli_command_key", "required")
            if body.cli_command_key not in settings.ai_cli_allowlist:
                raise _field_error("cli_command_key", "not_in_allowlist")
            for field in ("api_key", "base_url"):
                if getattr(body, field) is not None:
                    raise _field_error(field, "not_allowed")
            return body.model or "default", None
        if body.cli_command_key is not None:
            raise _field_error("cli_command_key", "not_allowed")
        if provider == catalog.OPENAI_COMPATIBLE:
            if not body.base_url:
                raise _field_error("base_url", "required")
        elif body.base_url is not None:
            raise _field_error("base_url", "not_allowed")
        model = body.model or catalog.default_model(provider)
        if not model:
            raise _field_error("model", "required")
        keep = row is not None and row.mode == "api" and row.provider == provider and (row.base_url or None) == (body.base_url or None)
        if body.api_key is not None:
            return model, "set"
        if keep and row.api_key_enc:
            return model, "keep"
        if provider != catalog.OPENAI_COMPATIBLE:
            raise _field_error("api_key", "required")  # a key is never carried over to another vendor or URL
        return model, "clear"

    async def put(self, ctx: TenantContext, body: schemas.AiSettingsIn) -> schemas.AiSettingsOut:
        org_id = _org_id(ctx)
        row = await repo.get_ai(self.session, org_id)
        model, key_action = self._validate(body, row)
        old_enc = row.api_key_enc if row else None
        new_enc = security.encrypt_field(body.api_key) if key_action == "set" and body.api_key else old_enc if key_action == "keep" else None
        cap = body.monthly_call_cap if ("monthly_call_cap" in body.model_fields_set or row is None) else row.monthly_call_cap
        new = {
            "provider": body.provider,
            "model": model,
            "mode": body.mode,
            "base_url": body.base_url,
            "cli_command_key": body.cli_command_key,
            "monthly_call_cap": cap,
        }
        fields = tuple(new)
        before = {f: getattr(row, f) if row else None for f in fields} | {"api_key_enc": old_enc}
        after = new | {"api_key_enc": new_enc}
        changed = row is None or any(getattr(row, f) != new[f] for f in ("provider", "model", "mode", "base_url", "cli_command_key")) or new_enc != old_enc
        if row is None:
            row = await repo.add(self.session, OrgAiSettings(org_id=org_id, **new, api_key_enc=new_enc, updated_by=ctx.user_id))
        else:
            for field, value in new.items():
                setattr(row, field, value)
            row.api_key_enc = new_enc
            row.updated_by = ctx.user_id
        if changed:
            row.verified_at = None
        await self.session.flush()
        await audit.record(ctx, self.session, audit.AI_SETTINGS_UPDATED, "org_ai_settings", org_id, before=before, after=after)
        return self._out(row)

    async def test_connection(self, ctx: TenantContext) -> schemas.AiTestOut:
        org_id = _org_id(ctx)
        config = await self.get_provider_config(org_id)
        if config is None:
            raise AppError("AI_NOT_CONFIGURED")
        provider = build_provider(config)  # AI_NOT_CONFIGURED when no such provider is registered
        reason: str | None = None
        try:
            ok = await asyncio.wait_for(provider.test_connection(), timeout=settings.ai_request_timeout_sec)
            if not ok:
                reason = "connection_failed"
        except AppError as exc:
            reason = "provider_error" if exc.code == "AI_PROVIDER_ERROR" else "connection_failed"
        except Exception:  # never echo provider messages: they can contain the key
            reason = "connection_failed"
        row = await repo.get_ai(self.session, org_id)
        assert row is not None  # config exists
        verified = _now() if reason is None else None
        if (row.verified_at is None) != (verified is None):
            before = {"verified_at": _iso(row.verified_at)}
            row.verified_at = verified
            await self.session.flush()
            await audit.record(
                ctx, self.session, audit.AI_SETTINGS_UPDATED, "org_ai_settings", org_id,
                before=before, after={"verified_at": _iso(verified)}, reason="connection_test",
            )  # fmt: skip
        else:
            row.verified_at = verified
        return schemas.AiTestOut(ok=reason is None, verified_at=row.verified_at, reason=reason)

    async def usage(self, ctx: TenantContext) -> schemas.AiUsageOut:
        org_id = _org_id(ctx)
        now = _now()
        start = datetime(now.year, now.month, 1, tzinfo=UTC)
        end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
        rows = await repo.ai_usage(self.session, org_id, start, end)
        calls = sum(r.calls for r in rows)
        settings_row = await repo.get_ai(self.session, org_id)
        cap = settings_row.monthly_call_cap if settings_row else None
        return schemas.AiUsageOut(
            month=f"{start.year:04d}-{start.month:02d}",
            calls=calls,
            tokens_in=int(sum(r.tokens_in for r in rows)),
            tokens_out=int(sum(r.tokens_out for r in rows)),
            by_feature={r.feature: r.calls for r in rows},
            monthly_call_cap=cap,
            remaining=None if cap is None else max(cap - calls, 0),
        )


# =========================================================================== branding


class BrandingService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _values(theme: BrandingTheme | None) -> dict[str, str | None]:
        return {
            "primary_color": (theme.primary_color if theme else None) or STARTER_THEME["primary"],
            "accent_color": (theme.accent_color if theme else None) or STARTER_THEME["accent"],
            "background_style": (theme.background_style if theme else None) or STARTER_THEME["background_style"],
            "font_key": (theme.font_key if theme else None) or STARTER_THEME["font_key"],
        }

    @staticmethod
    def _checks(primary: str, accent: str) -> dict[str, dict[str, Any]]:
        return {"primary": check_text_contrast(primary).as_dict(), "accent": check_text_contrast(accent).as_dict()}

    async def _asset_url(self, org_id: UUID, asset_id: UUID | None) -> str | None:
        if asset_id is None:
            return None
        asset = await repo.get_asset(self.session, org_id, asset_id)
        return await get_storage().signed_url(asset.storage_key) if asset else None

    async def _view(self, org_id: UUID, theme: BrandingTheme | None) -> schemas.BrandingOut:
        values = self._values(theme)
        starter = self._values(None)
        return schemas.BrandingOut(
            **values,  # type: ignore[arg-type]
            logo_url=await self._asset_url(org_id, theme.logo_asset_id if theme else None),
            favicon_url=await self._asset_url(org_id, theme.favicon_asset_id if theme else None),
            is_customized=values != starter,
            status_colors=STATUS_COLORS,
            contrast=self._checks(values["primary_color"], values["accent_color"]),  # type: ignore[arg-type]
        )

    async def get(self, ctx: TenantContext) -> schemas.BrandingOut:
        org_id = _org_id(ctx)
        return await self._view(org_id, await repo.get_theme(self.session, org_id))

    async def _theme_for_update(self, ctx: TenantContext) -> BrandingTheme:
        org_id = _org_id(ctx)
        theme = await repo.get_theme(self.session, org_id, for_update=True)
        if theme is None:
            theme = await repo.add(self.session, BrandingTheme(scope="org", org_id=org_id, updated_by=ctx.user_id))
        return theme

    async def copy_starter_theme(self, org_id: UUID) -> None:
        """Interface for M2: give a new organization its own copy of the starter theme. Idempotent."""
        if await repo.get_theme(self.session, org_id) is None:
            await repo.add(
                self.session,
                BrandingTheme(
                    scope="org",
                    org_id=org_id,
                    primary_color=STARTER_THEME["primary"],
                    accent_color=STARTER_THEME["accent"],
                    background_style=STARTER_THEME["background_style"],
                    font_key=STARTER_THEME["font_key"],
                ),
            )

    def _audit_state(self, theme: BrandingTheme | None) -> dict[str, Any]:
        state: dict[str, Any] = dict(self._values(theme))
        state["logo_asset_id"] = str(theme.logo_asset_id) if theme and theme.logo_asset_id else None
        state["favicon_asset_id"] = str(theme.favicon_asset_id) if theme and theme.favicon_asset_id else None
        return state

    async def _save(self, ctx: TenantContext, theme: BrandingTheme, before: dict[str, Any]) -> schemas.BrandingOut:
        theme.updated_by = ctx.user_id
        await self.session.flush()
        await audit.record(ctx, self.session, audit.BRANDING_UPDATED, "branding_theme", theme.id, before=before, after=self._audit_state(theme))
        return await self._view(_org_id(ctx), theme)

    async def _drop_asset(self, org_id: UUID, asset_id: UUID | None) -> None:
        asset = await repo.get_asset(self.session, org_id, asset_id) if asset_id else None
        if asset is not None:
            key = asset.storage_key
            await repo.delete(self.session, asset)
            await get_storage().delete(key)

    def _check_colors(self, primary: str, accent: str, fields: set[str]) -> None:
        for name, color in (("primary_color", primary), ("accent_color", accent)):
            result = check_text_contrast(color)
            if name in fields and not result.passes:
                raise AppError(
                    "BRANDING_CONTRAST_TOO_LOW", 422,
                    {"field": name, "ratio": result.as_dict()["ratio"], "minimum": MIN_RATIO, "text_color": result.text_color},
                )  # fmt: skip

    async def put(self, ctx: TenantContext, body: schemas.BrandingIn) -> schemas.BrandingOut:
        org_id = _org_id(ctx)
        provided = {k: v for k, v in body.model_dump(include={"primary_color", "accent_color", "background_style", "font_key"}).items() if v is not None}
        values = {**self._values(await repo.get_theme(self.session, org_id)), **provided}
        self._check_colors(values["primary_color"], values["accent_color"], set(provided))  # type: ignore[arg-type]
        assets: dict[str, UUID | None] = {}
        for field, kind in (("logo_asset_id", "logo"), ("favicon_asset_id", "favicon")):
            if field in body.model_fields_set:
                new_id = getattr(body, field)
                if new_id is not None and await repo.get_asset(self.session, org_id, new_id, kind) is None:
                    raise AppError("NOT_FOUND")
                assets[field] = new_id
        theme = await self._theme_for_update(ctx)
        before = self._audit_state(theme)
        for field, value in provided.items():
            setattr(theme, field, value)
        for field, new_id in assets.items():
            old_id = getattr(theme, field)
            if old_id != new_id:
                setattr(theme, field, new_id)
                await self.session.flush()
                await self._drop_asset(org_id, old_id)
        return await self._save(ctx, theme, before)

    async def validate(self, ctx: TenantContext, body: schemas.BrandingValidateIn) -> schemas.BrandingValidateOut:
        values = self._values(await repo.get_theme(self.session, _org_id(ctx)))
        primary = body.primary_color or values["primary_color"]
        accent = body.accent_color or values["accent_color"]
        checks = self._checks(primary, accent)  # type: ignore[arg-type]
        return schemas.BrandingValidateOut(valid=all(c["passes"] for c in checks.values()), checks=checks)

    async def reset(self, ctx: TenantContext) -> schemas.BrandingOut:
        theme = await self._theme_for_update(ctx)
        before = self._audit_state(theme)
        starter = self._values(None)
        for field, value in starter.items():
            setattr(theme, field, value)
        return await self._save(ctx, theme, before)  # the organization's own logo and favicon stay

    async def upload(self, ctx: TenantContext, kind: str, upload: UploadFile | None) -> schemas.BrandingOut:
        org_id = _org_id(ctx)
        data, declared = await read_limited(upload)
        info = inspect_image(data, declared=declared, allow_ico=kind == "favicon")
        storage = get_storage()
        stored = await storage.put(f"{org_id}/{kind}/{uuid4()}.{info.extension}", data, info.content_type)
        try:
            asset = await repo.add(
                self.session,
                Asset(
                    org_id=org_id, kind=kind, storage_key=stored.key, content_type=stored.content_type,
                    size_bytes=stored.size_bytes, sha256=stored.sha256, encrypted=False, created_by=ctx.user_id,
                ),
            )  # fmt: skip
            theme = await self._theme_for_update(ctx)
            before = self._audit_state(theme)
            field = f"{kind}_asset_id"
            old_id = getattr(theme, field)
            setattr(theme, field, asset.id)
            await self.session.flush()
            await self._drop_asset(org_id, old_id)
            return await self._save(ctx, theme, before)
        except Exception:
            await storage.delete(stored.key)
            raise

    @staticmethod
    def fonts() -> schemas.FontsOut:
        return schemas.FontsOut(
            fonts=[{"key": k, "label": FONT_LABELS[k][0], "category": FONT_LABELS[k][1]} for k in FONT_KEYS],
            background_styles=list(BACKGROUND_STYLES),
        )


# =========================================================================== documents


class DocumentService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _doc(self, ctx: TenantContext, doc_id: UUID | str, *, for_update: bool = False) -> Document:
        doc = await repo.get_document(self.session, _org_id(ctx), _uuid(doc_id), for_update=for_update)
        if doc is None:
            raise AppError("NOT_FOUND")
        return doc

    @staticmethod
    def _out(doc: Document, stats: dict[str, Any]) -> schemas.DocumentOut:
        current = stats["current"]
        return schemas.DocumentOut(
            id=doc.id, doc_type=doc.doc_type, title=doc.title, version_count=stats["count"],
            current_version_id=current.id if current else None,
            current_version_no=current.version_no if current else None,
            created_at=doc.created_at, updated_at=doc.updated_at,
        )  # fmt: skip

    async def _out_one(self, doc: Document) -> schemas.DocumentOut:
        stats = (await repo.version_stats(self.session, [doc.id]))[doc.id]
        return self._out(doc, stats)

    async def create(self, ctx: TenantContext, body: schemas.DocumentCreate) -> schemas.DocumentOut:
        doc = await repo.add(self.session, Document(org_id=_org_id(ctx), doc_type=body.doc_type, title=body.title, created_by=ctx.user_id))
        await audit.record(ctx, self.session, audit.DOCUMENT_CREATED, "document", doc.id, after={"doc_type": doc.doc_type, "title": doc.title})
        await self.session.refresh(doc)
        return await self._out_one(doc)

    async def list(self, ctx: TenantContext, *, doc_type: str | None, order: list[Any], offset: int, limit: int) -> tuple[list[schemas.DocumentOut], int]:
        docs, total = await repo.list_documents(self.session, _org_id(ctx), doc_type=doc_type, order=order, offset=offset, limit=limit)
        stats = await repo.version_stats(self.session, [d.id for d in docs])
        return [self._out(d, stats[d.id]) for d in docs], total

    async def get(self, ctx: TenantContext, doc_id: UUID) -> schemas.DocumentOut:
        return await self._out_one(await self._doc(ctx, doc_id))

    async def update(self, ctx: TenantContext, doc_id: UUID, body: schemas.DocumentPatch) -> schemas.DocumentOut:
        doc = await self._doc(ctx, doc_id, for_update=True)
        changes = body.model_dump(exclude_none=True)
        if "doc_type" in changes and changes["doc_type"] != doc.doc_type and await repo.has_published(self.session, doc.id):
            raise AppError("CONFLICT", 409, {"reason": "document_has_published_version"})  # consent refers to versions
        before = {k: getattr(doc, k) for k in changes if getattr(doc, k) != changes[k]}
        for field in before:
            setattr(doc, field, changes[field])
        if before:
            await self.session.flush()
            await audit.record(
                ctx, self.session, audit.SETTINGS_UPDATED, "document", doc.id,
                before=before, after={k: changes[k] for k in before},
            )  # fmt: skip
            await self.session.refresh(doc)
        return await self._out_one(doc)

    async def _version_out(self, org_id: UUID, version: DocumentVersion) -> schemas.VersionOut:
        return schemas.VersionOut(
            id=version.id, document_id=version.document_id, version_no=version.version_no, status=version.status,
            content_html=version.content_html, pdf_url=await BrandingService(self.session)._asset_url(org_id, version.pdf_asset_id),
            published_at=version.published_at, published_by=version.published_by, created_at=version.created_at,
        )  # fmt: skip

    async def create_version(
        self, ctx: TenantContext, doc_id: UUID, *, html: str | None = None, pdf: UploadFile | None = None
    ) -> schemas.VersionOut:
        org_id = _org_id(ctx)
        doc = await self._doc(ctx, doc_id, for_update=True)  # serializes version numbering per document
        content_html: str | None = None
        pdf_asset: Asset | None = None
        storage = get_storage()
        stored_key: str | None = None
        if html is not None:
            content_html = sanitize_html(html)
            if not content_html.strip():
                raise validation_error([{"field": "content_html", "code": "empty"}])
        else:
            data, declared = await read_limited(pdf)
            content_type = inspect_pdf(data, declared=declared)
            stored = await storage.put(f"{org_id}/doc_pdf/{uuid4()}.pdf", data, content_type)
            stored_key = stored.key
        try:
            if stored_key:
                pdf_asset = await repo.add(
                    self.session,
                    Asset(
                        org_id=org_id, kind="doc_pdf", storage_key=stored.key, content_type=stored.content_type,
                        size_bytes=stored.size_bytes, sha256=stored.sha256, encrypted=False, created_by=ctx.user_id,
                    ),
                )  # fmt: skip
            version = await repo.add(
                self.session,
                DocumentVersion(
                    document_id=doc.id, version_no=await repo.next_version_no(self.session, doc.id), content_html=content_html,
                    pdf_asset_id=pdf_asset.id if pdf_asset else None, status="draft", created_by=ctx.user_id,
                ),
            )  # fmt: skip
            await audit.record(
                ctx, self.session, audit.DOCUMENT_VERSION_CREATED, "document_version", version.id,
                after={"document_id": str(doc.id), "version_no": version.version_no, "kind": "pdf" if pdf_asset else "html", "content_html": content_html},
            )  # fmt: skip
        except Exception:
            if stored_key:
                await storage.delete(stored_key)
            raise
        await self.session.refresh(version)
        return await self._version_out(org_id, version)

    async def list_versions(self, ctx: TenantContext, doc_id: UUID) -> schemas.VersionListOut:
        doc = await self._doc(ctx, doc_id)
        versions = await repo.list_versions(self.session, doc.id)
        items = [await self._version_out(_org_id(ctx), v) for v in versions]
        return schemas.VersionListOut(items=items, total=len(items))

    async def publish_version(self, ctx: TenantContext, doc_id: UUID | str, version_id: UUID | str) -> DocumentVersion:
        """Interface (also used by M8 consent): a published version never changes again."""
        doc = await self._doc(ctx, doc_id, for_update=True)
        version = await repo.get_version(self.session, doc.id, _uuid(version_id), for_update=True)
        if version is None:
            raise AppError("NOT_FOUND")
        if version.status == "published":
            raise AppError("CONFLICT", 409, {"reason": "already_published"})
        version.status = "published"
        version.published_at = _now()
        version.published_by = ctx.user_id
        await self.session.flush()
        await audit.record(
            ctx, self.session, audit.DOCUMENT_PUBLISHED, "document_version", version.id,
            before={"status": "draft"}, after={"status": "published", "document_id": str(doc.id), "version_no": version.version_no},
        )  # fmt: skip
        await self.session.refresh(version)
        return version

    async def publish_out(self, ctx: TenantContext, doc_id: UUID, version_id: UUID) -> schemas.VersionOut:
        return await self._version_out(_org_id(ctx), await self.publish_version(ctx, doc_id, version_id))

    async def latest_published(self, org_id: UUID, doc_type: str) -> DocumentVersion | None:
        """Interface for M8: the current published version of the organization's document of this type."""
        return await repo.latest_published(self.session, org_id, doc_type)


def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))
