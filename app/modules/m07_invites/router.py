"""M7 routes (spec 6/M7). Owner or org admin act; the super admin has no access to candidate lists."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.core import flags
from app.core.db import TenantContext
from app.core.deps import DbSession, current_ctx, require_feature, require_owner_or_admin, require_role
from app.core.errors import ERROR_CODES, AppError, validation_error
from app.core.pagination import Page, PageParams, page_params
from app.core.roles import HR_USER, ORG_ADMIN
from app.modules.m07_invites import csv_import, repo
from app.modules.m07_invites.schemas import (
    AccommodationsIn,
    ApproveIn,
    BulkResult,
    DeclineIn,
    EmailSentOut,
    ExtendIn,
    HelpRequestOut,
    InviteCreate,
    InviteCreated,
    InviteDetail,
    InviteRow,
    InviteRowLimited,
    LinkOut,
    ReasonIn,
)
from app.modules.m07_invites.service import HelpRequestService, InviteService


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
Staff = Annotated[TenantContext, Depends(require_role(ORG_ADMIN, HR_USER))]


def _staff_only(ctx: TenantContext) -> None:
    if ctx.role not in (ORG_ADMIN, HR_USER):
        raise AppError("FORBIDDEN")


async def role_owner(
    role_id: UUID, ctx: Annotated[TenantContext, Depends(current_ctx)], session: DbSession
) -> UUID | None:
    _staff_only(ctx)
    return await repo.role_owner(session, ctx.org_id, role_id)


async def invite_owner(
    invite_id: UUID, ctx: Annotated[TenantContext, Depends(current_ctx)], session: DbSession
) -> UUID | None:
    _staff_only(ctx)
    return await repo.invite_role_owner(session, ctx.org_id, invite_id)


async def help_owner(
    help_request_id: UUID, ctx: Annotated[TenantContext, Depends(current_ctx)], session: DbSession
) -> UUID | None:
    _staff_only(ctx)
    return await repo.help_request_role_owner(session, ctx.org_id, help_request_id)


RoleEditor = Annotated[TenantContext, Depends(require_owner_or_admin(role_owner))]
InviteEditor = Annotated[TenantContext, Depends(require_owner_or_admin(invite_owner))]
HelpEditor = Annotated[TenantContext, Depends(require_owner_or_admin(help_owner))]

invites = APIRouter(
    prefix="/api/v1", tags=["invites"], dependencies=[Depends(require_feature(flags.INVITES))]
)


@invites.post(
    "/roles/{role_id}/invites",
    response_model=InviteCreated,
    status_code=201,
    summary="Create an invite",
    description="Owner or org admin. The role must be open with a linked assessment (`ROLE_NOT_OPEN`, "
    "`ROLE_HAS_NO_ASSESSMENT`). Returns the exam URL **once**; only a hash is stored. A duplicate email in "
    "the role is `DUPLICATE_INVITE` unless `allow_duplicate`.",
    responses=_errors(
        *EDIT, "ROLE_NOT_OPEN", "ROLE_HAS_NO_ASSESSMENT", "DUPLICATE_INVITE", "VALIDATION_ERROR"
    ),
)
async def create_invite(role_id: UUID, body: InviteCreate, ctx: RoleEditor, session: DbSession):
    return await InviteService.create_out(ctx, session, role_id, body)


@invites.post(
    "/roles/{role_id}/invites/bulk",
    response_model=BulkResult,
    status_code=201,
    summary="Create invites from a CSV",
    description="Multipart field `file`: header `name,email,phone`, UTF-8 (BOM allowed), at most 200 rows. "
    "Returns a result per row (exam URLs shown once). Rows with duplicates, bad cells or leading formula "
    "characters fail individually; a file over the limit fails whole.",
    dependencies=[Depends(require_feature(flags.CSV_BULK_INVITE))],
    responses=_errors(*EDIT, "ROLE_NOT_OPEN", "ROLE_HAS_NO_ASSESSMENT", "VALIDATION_ERROR"),
)
async def bulk_invites(
    role_id: UUID, ctx: RoleEditor, session: DbSession, file: Annotated[UploadFile, File()]
):
    data = await file.read(csv_import.MAX_BYTES + 1)
    if not data:
        raise validation_error([{"field": "file", "code": "empty_file"}])
    return await InviteService.bulk(ctx, session, role_id, data)


@invites.get(
    "/roles/{role_id}/invites",
    response_model=Page[InviteRow] | Page[InviteRowLimited],
    summary="List a role's invites",
    description="Owner and org admin see status, score and flag counts. Another HR user sees name, status "
    "and decision only. Super admins have no access (counts are on the role).",
    responses=_errors(*EDIT),
)
async def list_invites(
    role_id: UUID,
    ctx: Staff,
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    status: Annotated[str | None, Query(pattern="^(invited|started|submitted|expired|revoked)$")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
):
    owner = await repo.role_owner(session, ctx.org_id, role_id)
    full = ctx.role == ORG_ADMIN or owner == ctx.user_id
    items, total = await InviteService.list(ctx, session, role_id, params, status=status, q=q, full=full)
    page = InviteRow if full else InviteRowLimited
    return Page[page].build(items, total=total, params=params)  # type: ignore[valid-type]


@invites.get(
    "/invites/{invite_id}",
    response_model=InviteDetail,
    summary="Invite detail",
    description="Owner or org admin.",
    responses=_errors(*EDIT),
)
async def get_invite(invite_id: UUID, ctx: InviteEditor, session: DbSession):
    return await InviteService.get(ctx.org_id, session, invite_id)


@invites.post(
    "/invites/{invite_id}/regenerate-link",
    response_model=LinkOut,
    summary="Regenerate the exam link",
    description="Unstarted invites only. The old link stops working; the new URL is returned once.",
    responses=_errors(*EDIT, "CONFLICT"),
)
async def regenerate(invite_id: UUID, ctx: InviteEditor, session: DbSession):
    return await InviteService.regenerate_link(ctx, session, invite_id)


@invites.post(
    "/invites/{invite_id}/extend",
    response_model=InviteDetail,
    summary="Extend the expiry",
    description="New `expires_at` (future, at most 30 days out) with a reason. Revives an expired invite.",
    responses=_errors(*EDIT, "CONFLICT", "VALIDATION_ERROR"),
)
async def extend(invite_id: UUID, body: ExtendIn, ctx: InviteEditor, session: DbSession):
    return await InviteService.extend(ctx, session, invite_id, body)


@invites.post(
    "/invites/{invite_id}/revoke",
    response_model=InviteDetail,
    summary="Revoke an unstarted invite",
    description="Only invites that have not been opened for an attempt.",
    responses=_errors(*EDIT, "CONFLICT"),
)
async def revoke(invite_id: UUID, ctx: InviteEditor, session: DbSession):
    return await InviteService.revoke(ctx, session, invite_id)


@invites.post(
    "/invites/{invite_id}/reset-binding",
    response_model=InviteDetail,
    summary="Release the browser binding",
    description="Lets the candidate open the link in another browser. Reason required; audited.",
    responses=_errors(*EDIT, "CONFLICT", "VALIDATION_ERROR"),
)
async def reset_binding(invite_id: UUID, body: ReasonIn, ctx: InviteEditor, session: DbSession):
    return await InviteService.reset_binding(ctx, session, invite_id, body.reason)


@invites.patch(
    "/invites/{invite_id}/accommodations",
    response_model=InviteDetail,
    summary="Set accommodations",
    description="Camera exemption, time multiplier (0.5 to 4), extra minutes and a required reason.",
    responses=_errors(*EDIT, "CONFLICT", "VALIDATION_ERROR"),
)
async def accommodations(invite_id: UUID, body: AccommodationsIn, ctx: InviteEditor, session: DbSession):
    return await InviteService.set_accommodations(ctx, session, invite_id, body)


@invites.post(
    "/invites/{invite_id}/send-email",
    response_model=EmailSentOut,
    summary="Email the exam link",
    description="Needs verified SMTP (`SMTP_NOT_CONFIGURED`). The link is rotated and the new one emailed; "
    "it is not returned. Rate limited and audited.",
    dependencies=[Depends(require_feature(flags.EMAIL_SENDING))],
    responses=_errors(*EDIT, "SMTP_NOT_CONFIGURED", "CONFLICT", "RATE_LIMITED"),
)
async def send_email(invite_id: UUID, ctx: InviteEditor, session: DbSession):
    return await InviteService.send_email(ctx, session, invite_id)


help_router = APIRouter(
    prefix="/api/v1/help-requests",
    tags=["help-requests"],
    dependencies=[Depends(require_feature(flags.HELP_REQUESTS))],
)


@help_router.get(
    "",
    response_model=Page[HelpRequestOut],
    summary="Help-request inbox",
    description="HR users see requests for their own roles, org admins see all. Open requests first.",
    responses=_errors(*EDIT),
)
async def list_help_requests(
    ctx: Staff,
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    status: Annotated[str | None, Query(pattern="^(open|approved|declined)$")] = None,
):
    items, total = await HelpRequestService.list(ctx, session, params, status=status)
    return Page[HelpRequestOut].build(items, total=total, params=params)


@help_router.post(
    "/{help_request_id}/approve",
    response_model=HelpRequestOut,
    summary="Approve a help request",
    description="Optionally grants a camera exemption and extra minutes on the invite. Audited.",
    responses=_errors(*EDIT, "CONFLICT"),
)
async def approve(help_request_id: UUID, body: ApproveIn, ctx: HelpEditor, session: DbSession):
    return await HelpRequestService.approve(ctx, session, help_request_id, body)


@help_router.post(
    "/{help_request_id}/decline",
    response_model=HelpRequestOut,
    summary="Decline a help request",
    description="A note is required. Audited.",
    responses=_errors(*EDIT, "CONFLICT"),
)
async def decline(help_request_id: UUID, body: DeclineIn, ctx: HelpEditor, session: DbSession):
    return await HelpRequestService.decline(ctx, session, help_request_id, body)


router = APIRouter()
router.include_router(invites)
router.include_router(help_router)
