"""M2 platform administration (super admin only). Account-level actions on organization admins go
through ``m03_team.service.UserAdmin`` so there is one implementation of the A-04/A-05 rules."""

import re
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, flags, notifications
from app.core.branding_text import STARTER_THEME
from app.core.db import TenantContext
from app.core.errors import AppError, validation_error
from app.core.pagination import PageParams, parse_sort
from app.core.roles import ORG_ADMIN
from app.db import MisuseFlag, Organization, User
from app.modules.m02_platform import repo
from app.modules.m02_platform.schemas import (
    BrandingIn,
    DefaultsIn,
    FlagsIn,
    MisuseFlagClear,
    MisuseFlagCreate,
    OrgCreate,
    OrgUpdate,
    PlatformDeactivateIn,
)
from app.modules.m03_team import repo as team_repo
from app.modules.m03_team.service import UserAdmin, audit_ctx

_STARTER_THEME_VALUES = {
    "primary_color": STARTER_THEME["primary"],
    "accent_color": STARTER_THEME["accent"],
    "background_style": STARTER_THEME["background_style"],
    "font_key": STARTER_THEME["font_key"],
}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:50] or "org"


def _month_range(month: str | None) -> tuple[str, datetime, datetime]:
    now = datetime.now(UTC)
    label = month or f"{now.year:04d}-{now.month:02d}"
    match = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", label)
    if not match:
        raise validation_error([{"field": "month", "code": "invalid_format"}])
    year, mon = int(match[1]), int(match[2])
    start = datetime(year, mon, 1, tzinfo=UTC)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
    return label, start, end


class PlatformService:
    def __init__(self, admin: UserAdmin | None = None) -> None:
        self.admin = admin or UserAdmin()

    # --- organizations

    async def create_org(
        self, ctx: TenantContext, session: AsyncSession, payload: OrgCreate
    ) -> tuple[Organization, User, str]:
        """Org, first org admin (one-time temp password), one explicit flag row per catalog flag copied
        from the default set, and the starter theme copied as the org's own theme (A-12, A-13)."""
        slug = payload.slug
        if slug is None:
            slug = _slugify(payload.name)
            if await repo.slug_exists(session, slug):
                slug = f"{slug}-{secrets.token_hex(3)}"
        values: dict[str, Any] = {
            "name": payload.name,
            "slug": slug,
            "is_demo": payload.is_demo,
            "created_by": ctx.user_id,
        }
        if payload.locale:
            values["locale"] = payload.locale
        if payload.timezone:
            values["timezone"] = payload.timezone
        org = await repo.insert_org(session, **values)
        seeded = await repo.seed_org_flags(session, org.id, ctx.user_id)
        await repo.insert_org_theme(session, org.id, STARTER_THEME, ctx.user_id)
        await audit.record(
            audit_ctx(ctx, org.id),
            session,
            audit.ORG_CREATED,
            "organization",
            org.id,
            after={"name": org.name, "slug": org.slug, "flags_seeded": seeded, "theme": "starter"},
        )
        admin, temp = await self.admin.create_user(
            ctx,
            session,
            org_id=org.id,
            role=ORG_ADMIN,
            name=payload.admin.name,
            email=payload.admin.email,
            locale=None,
            action=audit.ORG_ADMIN_CREATED,
        )
        return org, admin, temp

    async def list_orgs(self, session: AsyncSession, params: PageParams, q: str | None, status: str | None):
        sort = parse_sort(params.sort, set(repo.ORG_SORT), default="created_at:desc")
        return await repo.list_orgs(
            session,
            q=q.strip() if q else None,
            status=status,
            sort=sort,
            offset=params.offset,
            limit=params.limit,
        )

    async def get_org(self, session: AsyncSession, org_id: UUID, *, for_update: bool = False) -> Organization:
        org = await repo.get_org(session, org_id, for_update=for_update)
        if org is None:
            raise AppError("NOT_FOUND")
        return org

    async def update_org(
        self, ctx: TenantContext, session: AsyncSession, org_id: UUID, body: OrgUpdate
    ) -> Organization:
        org = await self.get_org(session, org_id, for_update=True)
        before = {"name": org.name, "is_demo": org.is_demo}
        if body.name is not None:
            org.name = body.name
        if body.is_demo is not None:
            org.is_demo = body.is_demo
        await session.flush()
        after = {"name": org.name, "is_demo": org.is_demo}
        if after != before:
            await audit.record(
                audit_ctx(ctx, org.id),
                session,
                audit.ORG_UPDATED,
                "organization",
                org.id,
                before=before,
                after=after,
            )
        return org

    async def set_org_status(
        self, ctx: TenantContext, session: AsyncSession, org_id: UUID, *, disable: bool, reason: str | None
    ) -> Organization:
        org = await self.get_org(session, org_id, for_update=True)
        target = "disabled" if disable else "active"
        if org.status == target:
            raise AppError("CONFLICT", details={"status": org.status})
        before = {"status": org.status}
        org.status = target
        await session.flush()
        await audit.record(
            audit_ctx(ctx, org.id),
            session,
            audit.ORG_DISABLED if disable else audit.ORG_ENABLED,
            "organization",
            org.id,
            before=before,
            after={"status": target},
            reason=reason,
        )
        return org

    async def overview(self, session: AsyncSession, org_id: UUID) -> dict[str, Any]:
        await self.get_org(session, org_id)
        _, start, _ = _month_range(None)
        raw = await repo.overview(session, org_id, start)
        users = {"total": 0, "active": 0, "inactive": 0, "org_admins": 0, "hr_users": 0}
        for role, status, n in raw["users"]:
            users["total"] += n
            users[status] += n
            if role == "org_admin":
                users["org_admins"] += n
            elif role == "hr_user":
                users["hr_users"] += n
        roles = dict(raw["roles"])
        invites = {"total": 0, "invited": 0, "started": 0, "submitted": 0, "expired": 0, "revoked": 0}
        for status, n in raw["invites"]:
            invites["total"] += n
            invites[status] = n
        attempts = dict(raw["attempts"])
        return {
            "users": users,
            "open_roles": roles.get("open", 0),
            "roles_total": sum(roles.values()),
            "invites": invites,
            "attempts": {"total": sum(attempts.values()), "submitted": attempts.get("submitted", 0)},
            "ai_usage": {"calls_this_month": raw["ai_calls"]},
        }

    async def org_users(self, session: AsyncSession, org_id: UUID, params: PageParams):
        await self.get_org(session, org_id)
        return await repo.list_org_users(session, org_id, params.offset, params.limit)

    async def org_roles(self, session: AsyncSession, org_id: UUID, params: PageParams):
        await self.get_org(session, org_id)
        return await repo.list_org_roles(session, org_id, params.offset, params.limit)

    # --- organization admins

    async def create_admin(
        self, ctx: TenantContext, session: AsyncSession, org_id: UUID, name: str, email: str
    ) -> tuple[User, str]:
        await self.get_org(session, org_id)
        return await self.admin.create_user(
            ctx,
            session,
            org_id=org_id,
            role=ORG_ADMIN,
            name=name,
            email=email,
            locale=None,
            action=audit.ORG_ADMIN_CREATED,
        )

    async def _admin_target(self, session: AsyncSession, user_id: UUID) -> User:
        """Platform user endpoints address organization admins only (spec 6/M2); the break-glass SA is
        refused first so it is untargetable whatever the endpoint."""
        user = await team_repo.get_user_any_org(session, user_id, for_update=True)
        if user is None:
            raise AppError("NOT_FOUND")
        UserAdmin._protect(user)
        if user.role != ORG_ADMIN:
            raise AppError("FORBIDDEN")
        return user

    async def deactivate_admin(
        self, ctx: TenantContext, session: AsyncSession, user_id: UUID, body: PlatformDeactivateIn
    ) -> User:
        user = await self._admin_target(session, user_id)
        await self.admin.deactivate(
            ctx,
            session,
            user,
            action=audit.ORG_ADMIN_DEACTIVATED,
            reassign_roles_to=body.reassign_roles_to,
            confirmed_by_sa=body.confirm_last_admin,
            reason=body.reason,
        )
        return user

    async def reactivate_admin(self, ctx: TenantContext, session: AsyncSession, user_id: UUID) -> User:
        user = await self._admin_target(session, user_id)
        await self.admin.reactivate(ctx, session, user, action=audit.ORG_ADMIN_REACTIVATED)
        return user

    async def reset_admin_password(
        self, ctx: TenantContext, session: AsyncSession, user_id: UUID
    ) -> tuple[User, str]:
        user = await self._admin_target(session, user_id)
        return user, await self.admin.reset_password(ctx, session, user)

    async def unlock_admin(self, ctx: TenantContext, session: AsyncSession, user_id: UUID) -> User:
        user = await self._admin_target(session, user_id)
        await self.admin.unlock(ctx, session, user)
        return user

    # --- feature flags

    @staticmethod
    def _check_keys(keys: list[str], prefix: str) -> None:
        unknown = [k for k in keys if k not in flags.FLAG_KEYS]
        if unknown:
            raise validation_error([{"field": f"{prefix}.{k}", "code": "unknown_flag"} for k in unknown])

    async def catalog(self, session: AsyncSession) -> list[dict[str, Any]]:
        return [
            {
                "key": f.key,
                "description": f.description,
                "default_enabled": f.default_enabled,
                "visible_to_org": f.visible_to_org,
                "depends_on": list(flags.FLAG_DEPENDENCIES.get(f.key, ())),
            }
            for f in await repo.flag_catalog(session)
        ]

    async def org_flags(self, session: AsyncSession, org_id: UUID) -> dict[str, dict[str, bool]]:
        await self.get_org(session, org_id)
        raw = await repo.org_flag_raw(session, org_id)
        effective = await flags.effective_map(session, org_id, include_hidden=True)
        visible = {f.key: f.visible_to_org for f in await repo.flag_catalog(session)}
        return {
            k: {"enabled": raw[k], "effective": effective.get(k, False), "visible_to_org": visible[k]}
            for k in sorted(raw)
        }

    async def set_org_flags(
        self, ctx: TenantContext, session: AsyncSession, org_id: UUID, body: FlagsIn
    ) -> dict[str, dict[str, bool]]:
        await self.get_org(session, org_id, for_update=True)
        self._check_keys(list(body.flags), "flags")
        current = await repo.org_flag_raw(session, org_id)
        actx = audit_ctx(ctx, org_id)
        for key, enabled in body.flags.items():
            if current[key] == enabled:
                continue
            await repo.set_org_flag(session, org_id, key, enabled, ctx.user_id)
            await audit.record(
                actx,
                session,
                audit.FEATURE_FLAG_CHANGED,
                "feature_flag",
                key,
                before={"enabled": current[key]},
                after={"enabled": enabled},
                reason=body.reason,
            )
        return await self.org_flags(session, org_id)

    async def defaults(self, session: AsyncSession) -> dict[str, bool]:
        return {f.key: f.default_enabled for f in await repo.flag_catalog(session)}

    async def set_defaults(
        self, ctx: TenantContext, session: AsyncSession, body: DefaultsIn
    ) -> dict[str, bool]:
        self._check_keys(list(body.defaults), "defaults")
        current = await self.defaults(session)
        changed = {k: v for k, v in body.defaults.items() if current[k] != v}
        if changed:
            await repo.freeze_existing_orgs(session)  # existing orgs keep their values (A-13)
        for key, enabled in changed.items():
            await repo.set_default(session, key, enabled)
            await audit.record(
                ctx.for_org(None),
                session,
                audit.FEATURE_DEFAULTS_CHANGED,
                "feature_flag",
                key,
                before={"default_enabled": current[key]},
                after={"default_enabled": enabled},
                reason=body.reason,
            )
        return await self.defaults(session)

    # --- misuse flags (A-11)

    async def raise_flag(
        self, ctx: TenantContext, session: AsyncSession, org_id: UUID, body: MisuseFlagCreate
    ) -> MisuseFlag:
        await self.get_org(session, org_id)
        if body.user_id is not None and await team_repo.get_user(session, org_id, body.user_id) is None:
            raise AppError("NOT_FOUND")  # a user of another organization is not addressable
        assert ctx.user_id is not None
        flag = MisuseFlag(org_id=org_id, user_id=body.user_id, raised_by=ctx.user_id, comment=body.comment)
        session.add(flag)
        await session.flush()
        await audit.record(
            audit_ctx(ctx, org_id),
            session,
            audit.MISUSE_FLAG_RAISED,
            "misuse_flag",
            flag.id,
            after={"user_id": str(body.user_id) if body.user_id else None, "status": "open"},
            reason=body.comment,
        )
        for admin_id in await repo.active_org_admin_ids(session, org_id):
            await notifications.notify(
                session,
                org_id,
                admin_id,
                notifications.MISUSE_FLAG_RAISED,
                {"misuse_flag_id": str(flag.id), "comment": body.comment},
                "misuse_flag",
                flag.id,
            )
        return flag

    async def list_flags(self, session: AsyncSession, org_id: UUID, status: str | None, params: PageParams):
        await self.get_org(session, org_id)
        return await repo.list_misuse_flags(session, org_id, status, params.offset, params.limit)

    async def clear_flag(
        self, ctx: TenantContext, session: AsyncSession, flag_id: UUID, body: MisuseFlagClear
    ) -> MisuseFlag:
        flag = await repo.get_misuse_flag(session, flag_id)
        if flag is None:
            raise AppError("NOT_FOUND")
        if flag.status != "open":
            raise AppError("CONFLICT", details={"status": flag.status})
        flag.status = "cleared"
        flag.cleared_by = ctx.user_id
        flag.closing_note = body.closing_note
        flag.cleared_at = datetime.now(UTC)
        await session.flush()
        await audit.record(
            audit_ctx(ctx, flag.org_id),
            session,
            audit.MISUSE_FLAG_CLEARED,
            "misuse_flag",
            flag.id,
            before={"status": "open"},
            after={"status": "cleared"},
            reason=body.closing_note,
        )
        for admin_id in await repo.active_org_admin_ids(session, flag.org_id):
            await notifications.notify(
                session,
                flag.org_id,
                admin_id,
                notifications.MISUSE_FLAG_CLEARED,
                {"misuse_flag_id": str(flag.id), "closing_note": body.closing_note},
                "misuse_flag",
                flag.id,
            )
        return flag

    # --- platform branding (A-12: isolated from every organization theme)

    @staticmethod
    def _theme_out(theme) -> dict[str, Any]:
        if theme is None:
            return {**_STARTER_THEME_VALUES, "scope": "platform", "is_default": True, "updated_at": None}
        return {
            "scope": "platform",
            "primary_color": theme.primary_color or _STARTER_THEME_VALUES["primary_color"],
            "accent_color": theme.accent_color or _STARTER_THEME_VALUES["accent_color"],
            "background_style": theme.background_style or _STARTER_THEME_VALUES["background_style"],
            "font_key": theme.font_key or _STARTER_THEME_VALUES["font_key"],
            "is_default": False,
            "updated_at": theme.updated_at,
        }

    async def get_branding(self, session: AsyncSession) -> dict[str, Any]:
        return self._theme_out(await repo.get_platform_theme(session))

    async def save_branding(
        self, ctx: TenantContext, session: AsyncSession, body: BrandingIn
    ) -> dict[str, Any]:
        before = await self.get_branding(session)
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        theme = await repo.save_platform_theme(
            session, {**{k: before[k] for k in _STARTER_THEME_VALUES}, **changes}, ctx.user_id
        )
        await self._audit_branding(ctx, session, before, theme)
        return self._theme_out(theme)

    async def reset_branding(self, ctx: TenantContext, session: AsyncSession) -> dict[str, Any]:
        before = await self.get_branding(session)
        theme = await repo.save_platform_theme(session, dict(_STARTER_THEME_VALUES), ctx.user_id)
        await self._audit_branding(ctx, session, before, theme)
        return self._theme_out(theme)

    async def _audit_branding(
        self, ctx: TenantContext, session: AsyncSession, before: dict[str, Any], theme
    ) -> None:
        keys = list(_STARTER_THEME_VALUES)
        after = self._theme_out(theme)
        await audit.record(
            ctx.for_org(None),
            session,
            audit.PLATFORM_BRANDING_UPDATED,
            "branding_theme",
            theme.id,
            before={k: before[k] for k in keys},
            after={k: after[k] for k in keys},
        )

    # --- usage

    async def usage(self, session: AsyncSession, month: str | None, params: PageParams):
        label, start, end = _month_range(month)
        items, total = await repo.usage(session, start, end, params.offset, params.limit)
        return label, items, total
