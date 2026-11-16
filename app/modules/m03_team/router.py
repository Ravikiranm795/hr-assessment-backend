"""M3 team management routes: ``/api/v1/team/users`` (organization admin only, flag ``team``)."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core import flags
from app.core.db import TenantContext
from app.core.deps import DbSession, require_feature, require_role
from app.core.errors import ERROR_CODES, validation_error
from app.core.pagination import Page, PageParams, page_params
from app.core.roles import ORG_ADMIN
from app.db import User
from app.modules.m03_team.schemas import (
    DeactivatedUserOut,
    DeactivateIn,
    TempPasswordOut,
    UserCreate,
    UserDetailOut,
    UserOut,
    UserUpdate,
    UserWithTempPasswordOut,
)
from app.modules.m03_team.service import USER_STATUSES, TeamService

router = APIRouter(
    prefix="/api/v1/team/users",
    tags=["team"],
    dependencies=[Depends(require_feature(flags.TEAM))],
)

Admin = Annotated[TenantContext, Depends(require_role(ORG_ADMIN))]
service = TeamService()


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


_COMMON = {
    401: _err("UNAUTHENTICATED", "Missing or invalid access token."),
    403: _err(
        "FORBIDDEN", "Caller is not an organization admin, or the team feature is off (FEATURE_DISABLED)."
    ),
}
_NOT_FOUND = {404: _err("NOT_FOUND", "No such user in the caller's organization (foreign ids are also 404).")}
_ADMIN_ONLY = {
    403: _err("ADMIN_ONLY_ACTION", "The target is an organization admin: only the super admin may act on it.")
}


def _detail(user: User, roles: list[dict], model=UserDetailOut, **extra) -> Any:
    out = model.model_validate(user)
    out.owned_roles = roles
    for key, value in extra.items():
        setattr(out, key, value)
    return out


@router.get(
    "",
    response_model=Page[UserOut],
    summary="List team members",
    description="Users of the caller's organization: search (name, email), status filter, last login.",
    responses={**_COMMON},
)
async def list_users(
    ctx: Admin,
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=200, description="Matches name or email.")] = None,
    status: Annotated[str | None, Query(description="active or inactive")] = None,
    role: Annotated[str | None, Query(description="org_admin or hr_user")] = None,
):
    if status is not None and status not in USER_STATUSES:
        raise validation_error([{"field": "status", "code": "invalid_choice"}])
    if role is not None and role not in ("org_admin", "hr_user"):
        raise validation_error([{"field": "role", "code": "invalid_choice"}])
    users, total = await service.list_users(ctx, session, params, q=q, status=status, role=role)
    return Page.build([UserOut.model_validate(u) for u in users], total=total, params=params)


@router.post(
    "",
    status_code=201,
    response_model=UserWithTempPasswordOut,
    summary="Create an HR user",
    description=(
        "Role is fixed to HR user. Returns a one-time temporary password (A-07): first login forces a "
        "password change. Creating an organization admin is refused."
    ),
    responses={
        **_COMMON,
        **_ADMIN_ONLY,
        409: _err("CONFLICT", "Email already in use.", {"field": "email", "reason": "email_taken"}),
    },
)
async def create_user(body: UserCreate, ctx: Admin, session: DbSession):
    user, temp = await service.create_user(ctx, session, body)
    return UserWithTempPasswordOut(user=UserOut.model_validate(user), temporary_password=temp)


@router.get(
    "/{user_id}",
    response_model=UserDetailOut,
    summary="Read a team member",
    description="Profile plus the roles the user owns, each with an `owner_inactive` marker.",
    responses={**_COMMON, **_NOT_FOUND},
)
async def get_user(user_id: UUID, ctx: Admin, session: DbSession):
    user, roles = await service.get(ctx, session, user_id)
    return _detail(user, roles)


@router.patch(
    "/{user_id}",
    response_model=UserDetailOut,
    summary="Update name, locale or email of an HR user",
    description="Email can change only before the user's first login. Role changes are never possible here.",
    responses={**_COMMON, **_NOT_FOUND, **_ADMIN_ONLY},
)
async def update_user(user_id: UUID, body: UserUpdate, ctx: Admin, session: DbSession):
    user, roles = await service.update_user(ctx, session, user_id, body)
    return _detail(user, roles)


@router.post(
    "/{user_id}/deactivate",
    response_model=DeactivatedUserOut,
    summary="Deactivate an HR user",
    description=(
        "Never deletes. Ends all sessions. Optional `reassign_roles_to` moves owned roles to another active "
        "user of the same organization; otherwise they stay visible with `owner_inactive` until reassigned."
    ),
    responses={**_COMMON, **_NOT_FOUND, **_ADMIN_ONLY, 409: _err("CONFLICT", "User is already inactive.")},
)
async def deactivate_user(user_id: UUID, ctx: Admin, session: DbSession, body: DeactivateIn | None = None):
    user, roles, moved = await service.deactivate(ctx, session, user_id, body or DeactivateIn())
    return _detail(user, roles, DeactivatedUserOut, reassigned_roles=moved)


@router.post(
    "/{user_id}/reactivate",
    response_model=UserDetailOut,
    summary="Reactivate an HR user",
    description="Restores access. Roles still owned by the user stop being marked `owner_inactive`.",
    responses={**_COMMON, **_NOT_FOUND, **_ADMIN_ONLY, 409: _err("CONFLICT", "User is already active.")},
)
async def reactivate_user(user_id: UUID, ctx: Admin, session: DbSession):
    user, roles = await service.reactivate(ctx, session, user_id)
    return _detail(user, roles)


@router.post(
    "/{user_id}/reset-password",
    response_model=TempPasswordOut,
    summary="Issue a new temporary password",
    description="Replaces the password, forces a change at next login, signs the user out. Shown once.",
    responses={**_COMMON, **_NOT_FOUND, **_ADMIN_ONLY},
)
async def reset_password(user_id: UUID, ctx: Admin, session: DbSession):
    user, temp = await service.reset_password(ctx, session, user_id)
    return TempPasswordOut(user_id=user.id, temporary_password=temp)


@router.post(
    "/{user_id}/unlock",
    response_model=UserOut,
    summary="Unlock an account",
    description="Clears the failed-login counter and lockout.",
    responses={**_COMMON, **_NOT_FOUND, **_ADMIN_ONLY},
)
async def unlock_user(user_id: UUID, ctx: Admin, session: DbSession):
    return UserOut.model_validate(await service.unlock(ctx, session, user_id))


@router.post(
    "/{user_id}/sign-out-everywhere",
    response_model=UserOut,
    summary="Sign a user out everywhere",
    description="Bumps the token version and revokes refresh tokens, so every existing token stops working.",
    responses={**_COMMON, **_NOT_FOUND, **_ADMIN_ONLY},
)
async def sign_out_everywhere(user_id: UUID, ctx: Admin, session: DbSession):
    return UserOut.model_validate(await service.sign_out_everywhere(ctx, session, user_id))
