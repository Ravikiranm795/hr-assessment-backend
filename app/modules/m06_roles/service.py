"""M6 rules: lifecycle (draft, open, closed), ownership, and the single active assessment link."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import TenantContext
from app.core.errors import AppError, validation_error
from app.core.pagination import PageParams, parse_sort
from app.db import OpenRole, RoleAssessmentLink
from app.modules.m05_assessments.service import get_published_version
from app.modules.m06_roles import hooks, repo
from app.modules.m06_roles.schemas import (
    LinkHistory,
    LinkIn,
    LinkOut,
    Progress,
    RoleCreate,
    RoleDetail,
    RoleOut,
    RolePatch,
)

SORT_FIELDS = {"title", "created_at", "updated_at"}
_FIELDS = ("title", "department", "experience_level", "vacancies", "description")


def _snap(role: OpenRole) -> dict:
    return {f: getattr(role, f) for f in (*_FIELDS, "owner_id", "status")}


async def _role(session: AsyncSession, org_id: UUID, role_id: UUID, *, lock: bool = False) -> OpenRole:
    role = await repo.get_role(session, org_id, role_id, lock=lock)
    if role is None:
        raise AppError("NOT_FOUND")
    return role


async def _outs(session: AsyncSession, org_id: UUID, roles: list[OpenRole]) -> list[RoleOut]:
    owners = await repo.owners(session, org_id, list({r.owner_id for r in roles}))
    linked = await repo.active_links_by_role(session, org_id, [r.id for r in roles])
    out = []
    for r in roles:
        owner = owners.get(r.owner_id, {})
        out.append(
            RoleOut(
                id=r.id, title=r.title, department=r.department, experience_level=r.experience_level,
                vacancies=r.vacancies, description=r.description, owner_id=r.owner_id,
                owner_name=owner.get("name"), owner_inactive=owner.get("inactive", True), status=r.status,
                closed_at=r.closed_at, linked_version_id=linked.get(r.id), created_at=r.created_at,
                updated_at=r.updated_at,
            )
        )  # fmt: skip
    return out


class RoleService:
    # ------------------------------------------------------------------ CRUD

    @staticmethod
    async def create(ctx: TenantContext, session: AsyncSession, body: RoleCreate) -> RoleOut:
        role = OpenRole(
            org_id=ctx.org_id, owner_id=ctx.user_id, created_by=ctx.user_id, status="draft",
            **body.model_dump(),
        )  # fmt: skip
        session.add(role)
        await session.flush()
        await audit.record(ctx, session, audit.ROLE_CREATED, "open_role", role.id, after=_snap(role))
        return (await _outs(session, ctx.org_id, [role]))[0]

    @staticmethod
    async def search(
        org_id: UUID,
        session: AsyncSession,
        params: PageParams,
        *,
        q: str | None,
        status: str | None,
        owner: UUID | None,
        department: str | None,
    ) -> tuple[list[RoleOut], int]:
        sort = parse_sort(params.sort, SORT_FIELDS, default="created_at:desc")
        rows, total = await repo.search(
            session, org_id, q=q, status=status, owner=owner, department=department, sort=sort,
            offset=params.offset, limit=params.limit,
        )  # fmt: skip
        return await _outs(session, org_id, rows), total

    @staticmethod
    async def get(org_id: UUID, session: AsyncSession, role_id: UUID) -> RoleDetail:
        role = await _role(session, org_id, role_id)
        base = (await _outs(session, org_id, [role]))[0]
        invites, decisions = await repo.progress(session, org_id, role.id)
        link = await RoleService._link_out(session, org_id, role.id)
        return RoleDetail(
            **base.model_dump(),
            progress=Progress(
                invites={
                    k: invites.get(k, 0) for k in ("invited", "started", "submitted", "expired", "revoked")
                },
                decisions={k: decisions.get(k, 0) for k in ("waiting", "approved", "rejected")},
            ),
            assessment=link,
        )

    @staticmethod
    async def update(ctx: TenantContext, session: AsyncSession, role_id: UUID, body: RolePatch) -> RoleOut:
        role = await _role(session, ctx.org_id, role_id, lock=True)
        changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if getattr(role, k) != v}
        if "title" in changes and changes["title"] is None:
            raise validation_error([{"field": "title", "code": "required"}])
        before = {k: getattr(role, k) for k in changes}
        for k, v in changes.items():
            setattr(role, k, v)
        if changes:
            await session.flush()
            await audit.record(
                ctx, session, audit.ROLE_UPDATED, "open_role", role.id, before=before, after=changes
            )
        return (await _outs(session, ctx.org_id, [role]))[0]

    @staticmethod
    async def delete(ctx: TenantContext, session: AsyncSession, role_id: UUID) -> None:
        role = await _role(session, ctx.org_id, role_id, lock=True)
        if role.status != "draft":
            raise AppError("CONFLICT", details={"reason": "only_draft_roles_can_be_deleted"})
        if await repo.has_invites(session, ctx.org_id, role.id):
            raise AppError("ROLE_HAS_INVITES")
        link = await repo.active_link(session, ctx.org_id, role.id)
        if link is not None:
            await repo.end_link(session, link, ctx.user_id)
        role.deleted_at = datetime.now(UTC)
        await session.flush()
        await audit.record(ctx, session, audit.ROLE_UPDATED, "open_role", role.id, after={"deleted": True})

    # ------------------------------------------------------------------ lifecycle

    @staticmethod
    async def _transition(
        ctx: TenantContext,
        session: AsyncSession,
        role_id: UUID,
        *,
        allowed_from: tuple[str, ...],
        to: str,
        action: str,
    ) -> RoleOut:
        role = await _role(session, ctx.org_id, role_id, lock=True)
        if role.status not in allowed_from:
            raise AppError(
                "CONFLICT", details={"reason": "invalid_transition", "from": role.status, "to": to}
            )
        before = role.status
        role.status = to
        role.closed_at = datetime.now(UTC) if to == "closed" else None
        await session.flush()
        await audit.record(
            ctx, session, action, "open_role", role.id, before={"status": before}, after={"status": to}
        )
        return (await _outs(session, ctx.org_id, [role]))[0]

    @staticmethod
    async def open(ctx: TenantContext, session: AsyncSession, role_id: UUID) -> RoleOut:
        return await RoleService._transition(
            ctx, session, role_id, allowed_from=("draft",), to="open", action=audit.ROLE_OPENED
        )

    @staticmethod
    async def reopen(ctx: TenantContext, session: AsyncSession, role_id: UUID) -> RoleOut:
        return await RoleService._transition(
            ctx, session, role_id, allowed_from=("closed",), to="open", action=audit.ROLE_OPENED
        )

    @staticmethod
    async def close(ctx: TenantContext, session: AsyncSession, role_id: UUID) -> RoleOut:
        """Close the role; hooks (M7: revoke unstarted invites) run in the same transaction.
        In-progress attempts are left to finish."""
        out = await RoleService._transition(
            ctx, session, role_id, allowed_from=("open",), to="closed", action=audit.ROLE_CLOSED
        )
        for hook in hooks.on_role_close:
            await hook(ctx, session, role_id)
        return out

    @staticmethod
    async def reassign(ctx: TenantContext, session: AsyncSession, role_id: UUID, new_owner: UUID) -> RoleOut:
        role = await _role(session, ctx.org_id, role_id, lock=True)
        owners = await repo.owners(session, ctx.org_id, [new_owner])
        target = owners.get(new_owner)
        if target is None:  # unknown id or another organization's user (RF-4)
            raise AppError("NOT_FOUND")
        if target["inactive"] or target["role"] not in ("hr_user", "org_admin"):
            raise validation_error([{"field": "owner_id", "code": "not_an_active_staff_member"}])
        before = role.owner_id
        role.owner_id = new_owner
        await session.flush()
        await audit.record(
            ctx, session, audit.ROLE_REASSIGNED, "open_role", role.id,
            before={"owner_id": before}, after={"owner_id": new_owner},
        )  # fmt: skip
        return (await _outs(session, ctx.org_id, [role]))[0]

    # ------------------------------------------------------------------ assessment link

    @staticmethod
    async def _link_out(session: AsyncSession, org_id: UUID, role_id: UUID) -> LinkOut | None:
        rows = await repo.links(session, org_id, [role_id], active_only=True)
        return LinkOut(**rows[0]) if rows else None

    @staticmethod
    async def get_assessment(org_id: UUID, session: AsyncSession, role_id: UUID) -> LinkOut:
        await _role(session, org_id, role_id)
        link = await RoleService._link_out(session, org_id, role_id)
        if link is None:
            raise AppError("ROLE_HAS_NO_ASSESSMENT")
        return link

    @staticmethod
    async def history(org_id: UUID, session: AsyncSession, role_id: UUID) -> LinkHistory:
        await _role(session, org_id, role_id)
        return LinkHistory(
            items=[LinkOut(**r) for r in await repo.links(session, org_id, [role_id], active_only=False)]
        )

    @staticmethod
    async def link_assessment(
        ctx: TenantContext, session: AsyncSession, role_id: UUID, body: LinkIn
    ) -> LinkOut:
        """Link or replace. A foreign or unknown version is 404 (RF-4), a draft is
        ``ASSESSMENT_NOT_PUBLISHED``.
        Pending invites keep the version they were created with."""
        role = await _role(session, ctx.org_id, role_id, lock=True)
        await get_published_version(session, ctx.org_id, body.assessment_version_id)
        current = await repo.active_link(session, ctx.org_id, role.id)
        if current is not None and current.assessment_version_id == body.assessment_version_id:
            return await RoleService.get_assessment(ctx.org_id, session, role.id)
        if current is not None:
            await repo.end_link(session, current, ctx.user_id)
        session.add(
            RoleAssessmentLink(
                org_id=ctx.org_id, role_id=role.id, assessment_version_id=body.assessment_version_id,
                linked_by=ctx.user_id,
            )
        )  # fmt: skip
        await session.flush()
        await audit.record(
            ctx, session, audit.ROLE_ASSESSMENT_REPLACED if current else audit.ROLE_ASSESSMENT_LINKED,
            "open_role", role.id,
            before={"assessment_version_id": current.assessment_version_id} if current else None,
            after={"assessment_version_id": body.assessment_version_id},
        )  # fmt: skip
        return await RoleService.get_assessment(ctx.org_id, session, role.id)

    @staticmethod
    async def unlink_assessment(ctx: TenantContext, session: AsyncSession, role_id: UUID) -> None:
        role = await _role(session, ctx.org_id, role_id, lock=True)
        link = await repo.active_link(session, ctx.org_id, role.id)
        if link is None:
            raise AppError("ROLE_HAS_NO_ASSESSMENT")
        await repo.end_link(session, link, ctx.user_id)
        await audit.record(
            ctx, session, audit.ROLE_ASSESSMENT_UNLINKED, "open_role", role.id,
            before={"assessment_version_id": link.assessment_version_id},
        )  # fmt: skip

    # ------------------------------------------------------------------ interface for M7 (and M8)

    @staticmethod
    async def get_open_linked_role(
        session: AsyncSession, org_id: UUID, role_id: UUID
    ) -> tuple[OpenRole, UUID]:
        """``(role, assessment_version_id)`` of an open role with a linked published version."""
        role = await _role(session, org_id, role_id)
        if role.status != "open":
            raise AppError("ROLE_NOT_OPEN")
        link = await repo.active_link(session, org_id, role.id)
        if link is None:
            raise AppError("ROLE_HAS_NO_ASSESSMENT")
        return role, link.assessment_version_id
