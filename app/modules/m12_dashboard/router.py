"""M12 routes (spec 6/M12). Gadget routes need ``dashboard_gadgets``, notifications need ``dashboard``."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.core import flags
from app.core.db import TenantContext
from app.core.deps import DbSession, require_feature, require_role
from app.core.errors import ERROR_CODES
from app.core.pagination import Page, PageParams, page_params
from app.core.roles import HR_USER, ORG_ADMIN, SUPER_ADMIN
from app.modules.m12_dashboard.gadgets import GADGETS  # noqa: F401  (registers every gadget)
from app.modules.m12_dashboard.schemas import (
    CatalogOut,
    GadgetDataOut,
    LayoutIn,
    LayoutOut,
    NotificationOut,
    ReadAllOut,
    UnreadCountOut,
)
from app.modules.m12_dashboard.service import DashboardService

Staff = Annotated[TenantContext, Depends(require_role(SUPER_ADMIN, ORG_ADMIN, HR_USER))]


def _errors(*codes: str) -> dict[int | str, dict]:
    out: dict[int | str, dict] = {}
    for code in codes:
        spec = ERROR_CODES[code]
        example = {
            "error": {"code": spec.code, "message_key": spec.message_key, "details": {}, "request_id": "r1"}
        }
        out[spec.status] = {"description": code, "content": {"application/json": {"example": example}}}
    return out


BASE = ("UNAUTHENTICATED", "FORBIDDEN")

gadgets = APIRouter(
    prefix="/api/v1/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_feature(flags.DASHBOARD_GADGETS))],
)


@gadgets.get(
    "/gadgets/catalog",
    response_model=CatalogOut,
    summary="Gadget catalog",
    description="Gadgets the caller's role may use. Gadgets for other roles are absent.",
    responses=_errors(*BASE),
)
async def catalog(ctx: Staff, session: DbSession):
    return await DashboardService.catalog(ctx, session)


@gadgets.get(
    "/layout",
    response_model=LayoutOut,
    summary="Saved layout",
    description="Order, size and visibility per gadget; the role's defaults until the user saves one.",
    responses=_errors(*BASE),
)
async def get_layout(ctx: Staff, session: DbSession):
    return await DashboardService.layout(ctx, session)


@gadgets.put(
    "/layout",
    response_model=LayoutOut,
    summary="Save the layout",
    description="Replaces the caller's layout. A gadget the role may not use or a duplicate is 422.",
    responses=_errors(*BASE, "VALIDATION_ERROR"),
)
async def put_layout(body: LayoutIn, ctx: Staff, session: DbSession):
    return await DashboardService.save_layout(ctx, session, body)


@gadgets.get(
    "/gadgets/{key}/data",
    response_model=GadgetDataOut,
    summary="Gadget data",
    description="Scoped to the caller: HR own roles, org admin the organization, super admin platform counts "
    "only. A gadget the role may not use is 403, an unknown key 404.",
    responses=_errors(*BASE, "NOT_FOUND"),
)
async def gadget_data(key: str, ctx: Staff, session: DbSession):
    return await DashboardService.gadget_data(ctx, session, key)


notifications = APIRouter(
    prefix="/api/v1/notifications",
    tags=["notifications"],
    dependencies=[Depends(require_feature(flags.DASHBOARD))],
)


@notifications.get(
    "",
    response_model=Page[NotificationOut],
    summary="List notifications",
    description="The caller's own notifications, newest first; `unread=true` filters.",
    responses=_errors(*BASE),
)
async def list_notifications(
    ctx: Staff,
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    unread: Annotated[bool, Query()] = False,
):
    items, total = await DashboardService.notifications(ctx, session, params, unread=unread)
    return Page[NotificationOut].build(items, total=total, params=params)


@notifications.get(
    "/unread-count",
    response_model=UnreadCountOut,
    summary="Unread count",
    description="Number for the bell.",
    responses=_errors(*BASE),
)
async def unread_count(ctx: Staff, session: DbSession):
    return UnreadCountOut(unread=await DashboardService.unread_count(ctx, session))


@notifications.post(
    "/read-all",
    response_model=ReadAllOut,
    summary="Mark all read",
    description="Marks every unread notification of the caller as read.",
    responses=_errors(*BASE),
)
async def read_all(ctx: Staff, session: DbSession):
    return ReadAllOut(updated=await DashboardService.mark_all_read(ctx, session))


@notifications.post(
    "/{notification_id}/read",
    status_code=204,
    summary="Mark one read",
    description="Another user's notification is 404.",
    responses=_errors(*BASE, "NOT_FOUND"),
)
async def read_one(notification_id: UUID, ctx: Staff, session: DbSession):
    await DashboardService.mark_read(ctx, session, notification_id)
    return Response(status_code=204)


router = APIRouter()
router.include_router(gadgets)
router.include_router(notifications)
