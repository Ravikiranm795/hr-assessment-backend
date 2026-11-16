"""M2 platform administration routes: ``/api/v1/platform/*`` (super admin only, no feature flag)."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.db import TenantContext
from app.core.deps import DbSession, require_role
from app.core.errors import ERROR_CODES, validation_error
from app.core.pagination import Page, PageParams, page_params
from app.core.roles import SUPER_ADMIN
from app.modules.m02_platform.schemas import (
    AdminIn,
    AdminTempPasswordOut,
    BrandingIn,
    BrandingOut,
    DefaultsIn,
    DefaultsOut,
    FlagCatalogOut,
    FlagsIn,
    MisuseFlagClear,
    MisuseFlagCreate,
    MisuseFlagOut,
    OrgCreate,
    OrgCreatedOut,
    OrgFlagsOut,
    OrgOut,
    OrgRoleOut,
    OrgUpdate,
    OverviewOut,
    PlatformDeactivateIn,
    StatusChangeIn,
    UsageOut,
)
from app.modules.m02_platform.service import PlatformService
from app.modules.m03_team.schemas import UserOut, UserWithTempPasswordOut

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])

SA = Annotated[TenantContext, Depends(require_role(SUPER_ADMIN))]
Pages = Annotated[PageParams, Depends(page_params)]
service = PlatformService()


def _err(code: str, description: str, details: dict | None = None) -> dict[str, Any]:
    spec = ERROR_CODES[code]
    example = {
        "error": {
            "code": code,
            "message_key": spec.message_key,
            "details": details or {},
            "request_id": "req-1",
        }
    }
    return {"description": description, "content": {"application/json": {"example": example}}}


_BASE = {
    401: _err("UNAUTHENTICATED", "Missing or invalid access token."),
    403: _err("FORBIDDEN", "Caller is not a super admin."),
}
_NF = {404: _err("NOT_FOUND", "Unknown id.")}
_VAL = {422: _err("VALIDATION_ERROR", "Invalid input.", {"fields": [{"field": "name", "code": "required"}]})}
_PROTECTED = {
    403: _err("PROTECTED_ACCOUNT", "The break-glass super admin cannot be targeted by any endpoint.")
}


def _page(items: list, total: int, params: PageParams, model) -> Page:
    return Page.build([model.model_validate(i) for i in items], total=total, params=params)


# --------------------------------------------------------------------------- organizations


@router.get(
    "/orgs",
    response_model=Page[OrgOut],
    summary="List organizations",
    description="Paginated list with search (name or slug), status filter and sort.",
    responses={**_BASE, **_VAL},
)
async def list_orgs(
    ctx: SA,
    session: DbSession,
    params: Pages,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: Annotated[str | None, Query(description="active or disabled")] = None,
):
    if status is not None and status not in ("active", "disabled"):
        raise validation_error([{"field": "status", "code": "invalid_choice"}])
    orgs, total = await service.list_orgs(session, params, q, status)
    return _page(orgs, total, params, OrgOut)


@router.post(
    "/orgs",
    status_code=201,
    response_model=OrgCreatedOut,
    summary="Create an organization with its first admin",
    description=(
        "Creates the organization, its first org admin (one-time temporary password), one explicit "
        "feature-flag "
        "row per catalog flag copied from the default set, and a copy of the starter theme."
    ),
    responses={
        **_BASE,
        **_VAL,
        409: _err("CONFLICT", "Slug or admin email already in use.", {"field": "email"}),
    },
)
async def create_org(body: OrgCreate, ctx: SA, session: DbSession):
    org, admin, temp = await service.create_org(ctx, session, body)
    return OrgCreatedOut(
        organization=OrgOut.model_validate(org), admin=UserOut.model_validate(admin), temporary_password=temp
    )


@router.get(
    "/orgs/{org_id}",
    response_model=OrgOut,
    summary="Read an organization",
    description="Organization record.",
    responses={**_BASE, **_NF},
)
async def get_org(org_id: UUID, ctx: SA, session: DbSession):
    return OrgOut.model_validate(await service.get_org(session, org_id))


@router.patch(
    "/orgs/{org_id}",
    response_model=OrgOut,
    summary="Update an organization",
    description="Name and demo marker. Use disable and enable for the status.",
    responses={**_BASE, **_NF, **_VAL},
)
async def update_org(org_id: UUID, body: OrgUpdate, ctx: SA, session: DbSession):
    return OrgOut.model_validate(await service.update_org(ctx, session, org_id, body))


@router.post(
    "/orgs/{org_id}/disable",
    response_model=OrgOut,
    summary="Disable an organization",
    description="No new logins or exam starts; in-progress attempts finish. Optional reason is audited.",
    responses={**_BASE, **_NF, 409: _err("CONFLICT", "Already disabled.", {"status": "disabled"})},
)
async def disable_org(org_id: UUID, ctx: SA, session: DbSession, body: StatusChangeIn | None = None):
    return OrgOut.model_validate(
        await service.set_org_status(ctx, session, org_id, disable=True, reason=body.reason if body else None)
    )


@router.post(
    "/orgs/{org_id}/enable",
    response_model=OrgOut,
    summary="Enable an organization",
    description="Reverses a disable. Optional reason is audited.",
    responses={**_BASE, **_NF, 409: _err("CONFLICT", "Already active.", {"status": "active"})},
)
async def enable_org(org_id: UUID, ctx: SA, session: DbSession, body: StatusChangeIn | None = None):
    return OrgOut.model_validate(
        await service.set_org_status(
            ctx, session, org_id, disable=False, reason=body.reason if body else None
        )
    )


@router.get(
    "/orgs/{org_id}/overview",
    response_model=OverviewOut,
    summary="Organization overview",
    description="Counts of users, roles, invites, attempts and this month's AI calls. No candidate content.",
    responses={**_BASE, **_NF},
)
async def overview(org_id: UUID, ctx: SA, session: DbSession):
    return await service.overview(session, org_id)


@router.get(
    "/orgs/{org_id}/users",
    response_model=Page[UserOut],
    summary="List an organization's users (read-only)",
    description="All users of the organization.",
    responses={**_BASE, **_NF},
)
async def org_users(org_id: UUID, ctx: SA, session: DbSession, params: Pages):
    users, total = await service.org_users(session, org_id, params)
    return _page(users, total, params, UserOut)


@router.get(
    "/orgs/{org_id}/roles",
    response_model=Page[OrgRoleOut],
    summary="List an organization's open roles (read-only)",
    description="Roles with the owner and an `owner_inactive` marker.",
    responses={**_BASE, **_NF},
)
async def org_roles(org_id: UUID, ctx: SA, session: DbSession, params: Pages):
    roles, total = await service.org_roles(session, org_id, params)
    return Page.build([OrgRoleOut(**r) for r in roles], total=total, params=params)


# --------------------------------------------------------------------------- org admins


@router.post(
    "/orgs/{org_id}/admins",
    status_code=201,
    response_model=UserWithTempPasswordOut,
    summary="Create an organization admin",
    description="Only the super admin can create org admins (A-02). Returns a one-time temporary password.",
    responses={**_BASE, **_NF, **_VAL, 409: _err("CONFLICT", "Email already in use.", {"field": "email"})},
)
async def create_admin(org_id: UUID, body: AdminIn, ctx: SA, session: DbSession):
    user, temp = await service.create_admin(ctx, session, org_id, body.name, body.email)
    return UserWithTempPasswordOut(user=UserOut.model_validate(user), temporary_password=temp)


@router.post(
    "/users/{user_id}/deactivate",
    response_model=UserOut,
    summary="Deactivate an organization admin",
    description=(
        "Org admins only. Blocked with LAST_ADMIN_PROTECTED when it would leave the organization "
        "without an active "
        "admin, unless `confirm_last_admin` is true (A-04)."
    ),
    responses={
        **_BASE,
        **_NF,
        **_PROTECTED,
        409: _err("LAST_ADMIN_PROTECTED", "Would leave the organization with no active admin."),
    },
)
async def deactivate_admin(
    user_id: UUID, ctx: SA, session: DbSession, body: PlatformDeactivateIn | None = None
):
    return UserOut.model_validate(
        await service.deactivate_admin(ctx, session, user_id, body or PlatformDeactivateIn())
    )


@router.post(
    "/users/{user_id}/reactivate",
    response_model=UserOut,
    summary="Reactivate an organization admin",
    description="Org admins only.",
    responses={**_BASE, **_NF, **_PROTECTED, 409: _err("CONFLICT", "Already active.")},
)
async def reactivate_admin(user_id: UUID, ctx: SA, session: DbSession):
    return UserOut.model_validate(await service.reactivate_admin(ctx, session, user_id))


@router.post(
    "/users/{user_id}/reset-password",
    response_model=AdminTempPasswordOut,
    summary="Issue a temporary password to an org admin",
    description="Lost admin password (A-07). Forces a change at next login, ends all sessions. Shown once.",
    responses={**_BASE, **_NF, **_PROTECTED},
)
async def reset_admin_password(user_id: UUID, ctx: SA, session: DbSession):
    user, temp = await service.reset_admin_password(ctx, session, user_id)
    return AdminTempPasswordOut(user_id=user.id, temporary_password=temp)


@router.post(
    "/users/{user_id}/unlock",
    response_model=UserOut,
    summary="Unlock an org admin account",
    description="Clears the failed-login counter and lockout.",
    responses={**_BASE, **_NF, **_PROTECTED},
)
async def unlock_admin(user_id: UUID, ctx: SA, session: DbSession):
    return UserOut.model_validate(await service.unlock_admin(ctx, session, user_id))


# --------------------------------------------------------------------------- feature flags


@router.get(
    "/features/catalog",
    response_model=FlagCatalogOut,
    summary="Feature flag catalog",
    description="Every flag with its description, default, dependencies and org visibility.",
    responses={**_BASE},
)
async def flag_catalog(ctx: SA, session: DbSession):
    return {"items": await service.catalog(session)}


@router.get(
    "/features/defaults",
    response_model=DefaultsOut,
    summary="Default flag set for new organizations",
    description="Current defaults.",
    responses={**_BASE},
)
async def get_defaults(ctx: SA, session: DbSession):
    return {"defaults": await service.defaults(session)}


@router.put(
    "/features/defaults",
    response_model=DefaultsOut,
    summary="Change default flags for new organizations",
    description="Partial map. Existing organizations keep their current values. Each change is audited.",
    responses={**_BASE, **_VAL},
)
async def put_defaults(body: DefaultsIn, ctx: SA, session: DbSession):
    return {"defaults": await service.set_defaults(ctx, session, body)}


@router.get(
    "/orgs/{org_id}/features",
    response_model=OrgFlagsOut,
    summary="Read an organization's flags",
    description="Stored and effective value per flag.",
    responses={**_BASE, **_NF},
)
async def get_org_flags(org_id: UUID, ctx: SA, session: DbSession):
    return {"org_id": org_id, "flags": await service.org_flags(session, org_id)}


@router.put(
    "/orgs/{org_id}/features",
    response_model=OrgFlagsOut,
    summary="Bulk-set an organization's flags",
    description=(
        "Partial map of flag key to enabled. Turning a flag off hides the feature and blocks its API "
        "with FEATURE_DISABLED; "
        "data is kept (A-14). Every change is audited with the optional reason."
    ),
    responses={**_BASE, **_NF, **_VAL},
)
async def put_org_flags(org_id: UUID, body: FlagsIn, ctx: SA, session: DbSession):
    return {"org_id": org_id, "flags": await service.set_org_flags(ctx, session, org_id, body)}


# --------------------------------------------------------------------------- misuse flags


@router.post(
    "/orgs/{org_id}/misuse-flags",
    status_code=201,
    response_model=MisuseFlagOut,
    summary="Raise a misuse flag",
    description="Comment is mandatory. Active org admins get a notification carrying the comment (A-11).",
    responses={**_BASE, **_NF, **_VAL},
)
async def raise_flag(org_id: UUID, body: MisuseFlagCreate, ctx: SA, session: DbSession):
    return MisuseFlagOut.model_validate(await service.raise_flag(ctx, session, org_id, body))


@router.get(
    "/orgs/{org_id}/misuse-flags",
    response_model=Page[MisuseFlagOut],
    summary="List an organization's misuse flags",
    description="Optionally filtered by status (open or cleared).",
    responses={**_BASE, **_NF},
)
async def list_flags(
    org_id: UUID,
    ctx: SA,
    session: DbSession,
    params: Pages,
    status: Annotated[str | None, Query(description="open or cleared")] = None,
):
    if status is not None and status not in ("open", "cleared"):
        raise validation_error([{"field": "status", "code": "invalid_choice"}])
    flags_, total = await service.list_flags(session, org_id, status, params)
    return _page(flags_, total, params, MisuseFlagOut)


@router.post(
    "/misuse-flags/{flag_id}/clear",
    response_model=MisuseFlagOut,
    summary="Clear a misuse flag",
    description="A closing note is mandatory. The org admins are notified.",
    responses={**_BASE, **_NF, **_VAL, 409: _err("CONFLICT", "Already cleared.", {"status": "cleared"})},
)
async def clear_flag(flag_id: UUID, body: MisuseFlagClear, ctx: SA, session: DbSession):
    return MisuseFlagOut.model_validate(await service.clear_flag(ctx, session, flag_id, body))


# --------------------------------------------------------------------------- branding, usage


@router.get(
    "/branding",
    response_model=BrandingOut,
    summary="Read the platform theme",
    description="The SA's own theme (SA screens, login page). Starter theme until customized. Isolated.",
    responses={**_BASE},
)
async def get_branding(ctx: SA, session: DbSession):
    return await service.get_branding(session)


@router.put(
    "/branding",
    response_model=BrandingOut,
    summary="Update the platform theme",
    description="Colors are #RRGGBB; style and font from curated lists. Never touches an org theme.",
    responses={**_BASE, **_VAL},
)
async def put_branding(body: BrandingIn, ctx: SA, session: DbSession):
    return await service.save_branding(ctx, session, body)


@router.post(
    "/branding/reset",
    response_model=BrandingOut,
    summary="Reset the platform theme to the starter theme",
    description="Resets only the platform theme.",
    responses={**_BASE},
)
async def reset_branding(ctx: SA, session: DbSession):
    return await service.reset_branding(ctx, session)


@router.get(
    "/usage",
    response_model=UsageOut,
    summary="AI call counts per organization",
    description="Metadata only (counts and tokens) for a month (`YYYY-MM`, default current).",
    responses={**_BASE, **_VAL},
)
async def usage(
    ctx: SA,
    session: DbSession,
    params: Pages,
    month: Annotated[str | None, Query(description="YYYY-MM")] = None,
):
    label, items, total = await service.usage(session, month, params)
    out = [
        {
            "org_id": i["id"],
            "org_name": i["name"],
            **{k: i[k] for k in ("calls", "tokens_in", "tokens_out")},
            "by_feature": i.get("by_feature", {}),
        }
        for i in items
    ]
    return {"month": label, "items": out, "total": total, "page": params.page, "page_size": params.page_size}
