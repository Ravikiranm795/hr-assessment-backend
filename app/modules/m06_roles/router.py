"""M6 routes (spec 6/M6). Staff only; the super admin reads one organization via ``org_id``."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.core.db import TenantContext
from app.core.deps import DbSession, current_ctx, require_feature, require_owner_or_admin, require_role
from app.core.errors import ERROR_CODES, AppError, validation_error
from app.core.flags import ROLES
from app.core.pagination import Page, PageParams, page_params
from app.core.roles import HR_USER, ORG_ADMIN, SUPER_ADMIN
from app.modules.m06_roles import repo
from app.modules.m06_roles.schemas import (
    LinkHistory,
    LinkIn,
    LinkOut,
    ReassignIn,
    RoleCreate,
    RoleDetail,
    RoleOut,
    RolePatch,
)
from app.modules.m06_roles.service import RoleService

router = APIRouter(prefix="/api/v1/roles", tags=["roles"], dependencies=[Depends(require_feature(ROLES))])


def _errors(*codes: str) -> dict[int | str, dict]:
    grouped: dict[int, list[str]] = {}
    for code in codes:
        grouped.setdefault(ERROR_CODES[code].status, []).append(code)
    out: dict[int | str, dict] = {}
    for status, names in grouped.items():
        spec = ERROR_CODES[names[0]]
        example = {
            "error": {"code": spec.code, "message_key": spec.message_key, "details": {}, "request_id": "r1"}
        }
        out[status] = {"description": ", ".join(names), "content": {"application/json": {"example": example}}}
    return out


COMMON = ("UNAUTHENTICATED", "FEATURE_DISABLED", "NOT_FOUND")
EDIT = (*COMMON, "FORBIDDEN")

Writer = Annotated[TenantContext, Depends(require_role(ORG_ADMIN, HR_USER))]


@dataclass(frozen=True)
class Scope:
    ctx: TenantContext
    org_id: UUID


async def read_scope(
    ctx: Annotated[TenantContext, Depends(require_role(SUPER_ADMIN, ORG_ADMIN, HR_USER))],
    org_id: Annotated[
        UUID | None, Query(description="Super admin only (read-only): the organization.")
    ] = None,
) -> Scope:
    if ctx.org_id is not None:
        return Scope(ctx, ctx.org_id)
    if org_id is None:
        raise validation_error([{"field": "org_id", "code": "required"}])
    return Scope(ctx, org_id)


Reader = Annotated[Scope, Depends(read_scope)]


async def role_owner(
    role_id: UUID, ctx: Annotated[TenantContext, Depends(current_ctx)], session: DbSession
) -> UUID | None:
    if ctx.role not in (ORG_ADMIN, HR_USER):
        raise AppError("FORBIDDEN")
    return await repo.owner_id(session, ctx.org_id, role_id)


Editor = Annotated[TenantContext, Depends(require_owner_or_admin(role_owner))]


@router.get(
    "",
    response_model=Page[RoleOut],
    summary="List roles",
    description="Filter by `status`, `owner_id`, `department`, title search `q` and `mine`. "
    "Super admins read one organization via `org_id`.",
    responses=_errors(*COMMON),
)
async def list_roles(
    scope: Reader,
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: Annotated[str | None, Query(pattern="^(draft|open|closed)$")] = None,
    owner_id: UUID | None = None,
    department: Annotated[str | None, Query(max_length=200)] = None,
    mine: bool = False,
):
    owner = scope.ctx.user_id if mine else owner_id
    items, total = await RoleService.search(
        scope.org_id, session, params, q=q, status=status, owner=owner, department=department
    )
    return Page[RoleOut].build(items, total=total, params=params)


@router.post(
    "",
    response_model=RoleOut,
    status_code=201,
    summary="Create a role",
    description="Created as a draft; the caller becomes the owner.",
    responses=_errors(*EDIT, "VALIDATION_ERROR"),
)
async def create_role(body: RoleCreate, ctx: Writer, session: DbSession):
    return await RoleService.create(ctx, session, body)


@router.get(
    "/{role_id}",
    response_model=RoleDetail,
    summary="Role detail",
    description="Role with progress counts by invite status and decision, and the linked assessment version.",
    responses=_errors(*COMMON),
)
async def get_role(role_id: UUID, scope: Reader, session: DbSession):
    return await RoleService.get(scope.org_id, session, role_id)


@router.patch(
    "/{role_id}",
    response_model=RoleOut,
    summary="Update a role",
    description="Owner or org admin.",
    responses=_errors(*EDIT, "VALIDATION_ERROR"),
)
async def patch_role(role_id: UUID, body: RolePatch, ctx: Editor, session: DbSession):
    return await RoleService.update(ctx, session, role_id, body)


@router.delete(
    "/{role_id}",
    status_code=204,
    summary="Delete a draft role",
    description="Owner or org admin. Only a draft with no invites: otherwise `CONFLICT` or "
    "`ROLE_HAS_INVITES`.",
    responses=_errors(*EDIT, "CONFLICT", "ROLE_HAS_INVITES"),
)
async def delete_role(role_id: UUID, ctx: Editor, session: DbSession):
    await RoleService.delete(ctx, session, role_id)
    return Response(status_code=204)


@router.post(
    "/{role_id}/open",
    response_model=RoleOut,
    summary="Open a draft role",
    description="Draft to open. Invites can only be created for an open role with a linked assessment.",
    responses=_errors(*EDIT, "CONFLICT"),
)
async def open_role(role_id: UUID, ctx: Editor, session: DbSession):
    return await RoleService.open(ctx, session, role_id)


@router.post(
    "/{role_id}/close",
    response_model=RoleOut,
    summary="Close a role",
    description="Open to closed. Unstarted invites are revoked; in-progress attempts may finish.",
    responses=_errors(*EDIT, "CONFLICT"),
)
async def close_role(role_id: UUID, ctx: Editor, session: DbSession):
    return await RoleService.close(ctx, session, role_id)


@router.post(
    "/{role_id}/reopen",
    response_model=RoleOut,
    summary="Reopen a closed role",
    description="Closed to open. Revoked invites stay revoked.",
    responses=_errors(*EDIT, "CONFLICT"),
)
async def reopen_role(role_id: UUID, ctx: Editor, session: DbSession):
    return await RoleService.reopen(ctx, session, role_id)


@router.post(
    "/{role_id}/reassign",
    response_model=RoleOut,
    summary="Change the owner",
    description="`owner_id` must be an active HR user or org admin of the same organization (else 404).",
    responses=_errors(*EDIT, "VALIDATION_ERROR"),
)
async def reassign_role(role_id: UUID, body: ReassignIn, ctx: Editor, session: DbSession):
    return await RoleService.reassign(ctx, session, role_id, body.owner_id)


@router.get(
    "/{role_id}/assessment",
    response_model=LinkOut,
    summary="Current linked version",
    description="`ROLE_HAS_NO_ASSESSMENT` when nothing is linked.",
    responses=_errors(*COMMON, "ROLE_HAS_NO_ASSESSMENT"),
)
async def get_link(role_id: UUID, scope: Reader, session: DbSession):
    return await RoleService.get_assessment(scope.org_id, session, role_id)


@router.put(
    "/{role_id}/assessment",
    response_model=LinkOut,
    summary="Link or replace the assessment version",
    description="Body `assessment_version_id` must be a published version of this organization (404 "
    "otherwise, `ASSESSMENT_NOT_PUBLISHED` for a draft). Replacing keeps pending invites on their version.",
    responses=_errors(*EDIT, "ASSESSMENT_NOT_PUBLISHED"),
)
async def put_link(role_id: UUID, body: LinkIn, ctx: Editor, session: DbSession):
    return await RoleService.link_assessment(ctx, session, role_id, body)


@router.delete(
    "/{role_id}/assessment",
    status_code=204,
    summary="Unlink the assessment",
    description="Existing invites keep their version.",
    responses=_errors(*EDIT, "ROLE_HAS_NO_ASSESSMENT"),
)
async def delete_link(role_id: UUID, ctx: Editor, session: DbSession):
    await RoleService.unlink_assessment(ctx, session, role_id)
    return Response(status_code=204)


@router.get(
    "/{role_id}/assessment/history",
    response_model=LinkHistory,
    summary="Link history",
    description="Every link of the role, newest first, including replaced and removed ones.",
    responses=_errors(*COMMON),
)
async def link_history(role_id: UUID, scope: Reader, session: DbSession):
    return await RoleService.history(scope.org_id, session, role_id)
