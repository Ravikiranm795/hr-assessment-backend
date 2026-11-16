"""M3 team management plus the shared user-administration service.

``UserAdmin`` is the single place that creates users with a temporary password, deactivates,
reactivates, resets passwords, unlocks and signs users out. M2 (platform admin) calls it for
organization admins through this interface; ``TeamService`` wraps it with the org-admin rules
(role fixed to HR user, ``ADMIN_ONLY_ACTION`` on admins). ``UserAdmin.last_active_admin_guard``
is the A-04 guard other modules call before anything that could leave an org without an admin.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import TenantContext
from app.core.errors import AppError, validation_error
from app.core.pagination import PageParams, parse_sort
from app.core.roles import HR_USER, ORG_ADMIN, SUPER_ADMIN
from app.core.security import generate_temp_password, hash_password, normalize_email
from app.db import OpenRole, User
from app.modules.m03_team import repo
from app.modules.m03_team.schemas import DeactivateIn, UserCreate, UserUpdate

USER_STATUSES = ("active", "inactive")
USER_SORT_FIELDS = set(repo.SORT_COLUMNS)


def audit_ctx(ctx: TenantContext, org_id: UUID | None) -> TenantContext:
    """Audit rows about an organization carry that org id, also when the actor is the SA."""
    return ctx if ctx.org_id == org_id else ctx.for_org(org_id)


def snapshot(user: User) -> dict[str, Any]:
    """Audit-safe view of a user (no hashes, no secrets)."""
    return {
        "id": str(user.id),
        "org_id": str(user.org_id) if user.org_id else None,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "locale": user.locale,
    }


def owned_role_views(roles: list[OpenRole], owner: User) -> list[dict[str, Any]]:
    """Roles of ``owner`` with the ``owner_inactive`` marker (spec 6/M3)."""
    inactive = owner.status != "active"
    return [
        {"id": r.id, "title": r.title, "status": r.status, "owner_id": r.owner_id, "owner_inactive": inactive}
        for r in roles
    ]


class UserAdmin:
    """Account administration shared by M2 and M3. Callers have already authorized the actor."""

    async def last_active_admin_guard(
        self, session: AsyncSession, org_id: UUID, user_id: UUID, *, confirmed_by_sa: bool = False
    ) -> None:
        """A-04: raise ``LAST_ADMIN_PROTECTED`` if deactivating ``user_id`` would leave ``org_id`` with
        no active admin, unless the super admin performs it and confirms. Locks the admin rows."""
        admins = await repo.active_admin_ids(session, org_id)
        if user_id in admins and len(admins) == 1 and not confirmed_by_sa:
            raise AppError("LAST_ADMIN_PROTECTED")

    async def create_user(
        self,
        ctx: TenantContext,
        session: AsyncSession,
        *,
        org_id: UUID,
        role: str,
        name: str,
        email: str,
        locale: str | None,
        action: str,
    ) -> tuple[User, str]:
        """New active user with a one-time temporary password (returned, never stored in clear)."""
        temp = generate_temp_password()
        password_hash = await asyncio.to_thread(hash_password, temp)
        user = await repo.insert_user(
            session,
            org_id=org_id,
            email=normalize_email(email),
            name=name,
            role=role,
            password_hash=password_hash,
            must_change_password=True,
            locale=locale,
            created_by=ctx.user_id,
        )
        await audit.record(audit_ctx(ctx, org_id), session, action, "user", user.id, after=snapshot(user))
        return user, temp

    async def deactivate(
        self,
        ctx: TenantContext,
        session: AsyncSession,
        user: User,
        *,
        action: str,
        reassign_roles_to: UUID | None = None,
        confirmed_by_sa: bool = False,
        reason: str | None = None,
    ) -> int:
        """Deactivate (never delete), kill sessions, optionally reassign owned roles. Returns the
        number of roles reassigned."""
        self._protect(user)
        if user.status != "active":
            raise AppError("CONFLICT", details={"reason": "already_inactive"})
        org_id = user.org_id
        assert org_id is not None
        new_owner = await self._resolve_new_owner(session, org_id, user, reassign_roles_to)
        await self.last_active_admin_guard(session, org_id, user.id, confirmed_by_sa=confirmed_by_sa)

        before = snapshot(user)
        user.status = "inactive"
        user.token_version += 1
        await repo.revoke_refresh_tokens(session, user.id)
        await session.flush()
        after = snapshot(user)
        if confirmed_by_sa:
            after["confirmed_last_admin"] = True
        actx = audit_ctx(ctx, org_id)
        await audit.record(actx, session, action, "user", user.id, before=before, after=after, reason=reason)

        moved: list[UUID] = []
        if new_owner is not None:
            moved = await repo.reassign_roles(session, org_id, user.id, new_owner.id)
            for role_id in moved:
                await audit.record(
                    actx,
                    session,
                    audit.ROLE_REASSIGNED,
                    "open_role",
                    role_id,
                    before={"owner_id": str(user.id)},
                    after={"owner_id": str(new_owner.id)},
                    reason=reason,
                )
        return len(moved)

    async def reactivate(self, ctx: TenantContext, session: AsyncSession, user: User, *, action: str) -> None:
        self._protect(user)
        if user.status == "active":
            raise AppError("CONFLICT", details={"reason": "already_active"})
        before = snapshot(user)
        user.status = "active"
        await session.flush()
        await audit.record(
            audit_ctx(ctx, user.org_id), session, action, "user", user.id, before=before, after=snapshot(user)
        )

    async def reset_password(self, ctx: TenantContext, session: AsyncSession, user: User) -> str:
        """New temporary password: forced change at next login, all sessions and lockout cleared."""
        self._protect(user)
        temp = generate_temp_password()
        user.password_hash = await asyncio.to_thread(hash_password, temp)
        user.must_change_password = True
        user.token_version += 1
        user.failed_login_count = 0
        user.locked_until = None
        await repo.revoke_refresh_tokens(session, user.id)
        await session.flush()
        await audit.record(
            audit_ctx(ctx, user.org_id),
            session,
            audit.AUTH_TEMP_PASSWORD_ISSUED,
            "user",
            user.id,
            after={"must_change_password": True, "sessions_revoked": True},
        )
        return temp

    async def unlock(self, ctx: TenantContext, session: AsyncSession, user: User) -> None:
        self._protect(user)
        before = {"failed_login_count": user.failed_login_count, "locked_until": user.locked_until}
        user.failed_login_count = 0
        user.locked_until = None
        await session.flush()
        await audit.record(
            audit_ctx(ctx, user.org_id),
            session,
            audit.AUTH_ACCOUNT_UNLOCKED,
            "user",
            user.id,
            before=before,
            after={"failed_login_count": 0, "locked_until": None},
        )

    async def sign_out_everywhere(self, ctx: TenantContext, session: AsyncSession, user: User) -> None:
        self._protect(user)
        user.token_version += 1
        await repo.revoke_refresh_tokens(session, user.id)
        await session.flush()
        await audit.record(
            audit_ctx(ctx, user.org_id),
            session,
            audit.AUTH_SIGNED_OUT_EVERYWHERE,
            "user",
            user.id,
            after={"sessions_revoked": True},
        )

    # --- helpers

    @staticmethod
    def _protect(user: User) -> None:
        """A-05: the break-glass super admin cannot be targeted by any endpoint."""
        if user.is_default_super_admin:
            raise AppError("PROTECTED_ACCOUNT")

    @staticmethod
    async def _resolve_new_owner(
        session: AsyncSession, org_id: UUID, user: User, reassign_roles_to: UUID | None
    ) -> User | None:
        if reassign_roles_to is None:
            return None
        target = await repo.get_user(session, org_id, reassign_roles_to)
        if target is None:
            raise AppError("NOT_FOUND")  # RF-4: foreign or unknown ids in a body are 404
        if target.id == user.id or target.status != "active" or target.role not in (HR_USER, ORG_ADMIN):
            raise validation_error([{"field": "reassign_roles_to", "code": "invalid_owner"}])
        return target


class TeamService:
    """Org-admin view of user administration (spec 6/M3, matrix 4.3)."""

    def __init__(self, admin: UserAdmin | None = None) -> None:
        self.admin = admin or UserAdmin()

    @staticmethod
    def _org(ctx: TenantContext) -> UUID:
        assert ctx.org_id is not None  # guarded by require_role(ORG_ADMIN)
        return ctx.org_id

    async def _load(self, ctx: TenantContext, session: AsyncSession, user_id: UUID) -> User:
        user = await repo.get_user(session, self._org(ctx), user_id, for_update=True)
        if user is None:
            raise AppError("NOT_FOUND")
        return user

    async def _manageable(self, ctx: TenantContext, session: AsyncSession, user_id: UUID) -> User:
        """A-02: an OA can only act on HR users, never on an org admin (or any other role)."""
        user = await self._load(ctx, session, user_id)
        UserAdmin._protect(user)
        if user.role != HR_USER:
            raise AppError("ADMIN_ONLY_ACTION")
        return user

    async def list_users(
        self,
        ctx: TenantContext,
        session: AsyncSession,
        params: PageParams,
        *,
        q: str | None,
        status: str | None,
        role: str | None,
    ) -> tuple[list[User], int]:
        sort = parse_sort(params.sort, USER_SORT_FIELDS, default="name:asc")
        return await repo.list_users(
            session,
            self._org(ctx),
            q=q.strip() if q else None,
            status=status,
            role=role,
            sort=sort,
            offset=params.offset,
            limit=params.limit,
        )

    async def get(self, ctx: TenantContext, session: AsyncSession, user_id: UUID) -> tuple[User, list[dict]]:
        user = await repo.get_user(session, self._org(ctx), user_id)
        if user is None:
            raise AppError("NOT_FOUND")
        return user, await self.owned_roles(ctx, session, user)

    async def owned_roles(self, ctx: TenantContext, session: AsyncSession, user: User) -> list[dict]:
        return owned_role_views(await repo.owned_roles(session, self._org(ctx), user.id), user)

    async def create_user(
        self, ctx: TenantContext, session: AsyncSession, body: UserCreate
    ) -> tuple[User, str]:
        if body.role is not None and body.role != HR_USER:
            if body.role in (ORG_ADMIN, SUPER_ADMIN):
                raise AppError("ADMIN_ONLY_ACTION")
            raise validation_error([{"field": "role", "code": "invalid_choice"}])
        return await self.admin.create_user(
            ctx,
            session,
            org_id=self._org(ctx),
            role=HR_USER,
            name=body.name,
            email=body.email,
            locale=body.locale,
            action=audit.USER_CREATED,
        )

    async def update_user(
        self, ctx: TenantContext, session: AsyncSession, user_id: UUID, body: UserUpdate
    ) -> tuple[User, list[dict]]:
        user = await self._manageable(ctx, session, user_id)
        if body.role is not None and body.role != user.role:
            if body.role in (ORG_ADMIN, SUPER_ADMIN):
                raise AppError("ADMIN_ONLY_ACTION")
            raise validation_error([{"field": "role", "code": "invalid_choice"}])
        before = snapshot(user)
        if body.name is not None:
            user.name = body.name
        if body.locale is not None:
            user.locale = body.locale
        if body.email is not None:
            email = normalize_email(body.email)
            if email != normalize_email(user.email):
                if user.last_login_at is not None:
                    raise validation_error([{"field": "email", "code": "not_editable_after_login"}])
                user.email = email
        after = snapshot(user)
        if after != before:
            await repo.save_user(session)
            await audit.record(ctx, session, audit.USER_UPDATED, "user", user.id, before=before, after=snapshot(user))
        return user, await self.owned_roles(ctx, session, user)

    async def deactivate(
        self, ctx: TenantContext, session: AsyncSession, user_id: UUID, body: DeactivateIn
    ) -> tuple[User, list[dict], int]:
        user = await self._manageable(ctx, session, user_id)
        moved = await self.admin.deactivate(
            ctx,
            session,
            user,
            action=audit.USER_DEACTIVATED,
            reassign_roles_to=body.reassign_roles_to,
            reason=body.reason,
        )
        return user, await self.owned_roles(ctx, session, user), moved

    async def reactivate(
        self, ctx: TenantContext, session: AsyncSession, user_id: UUID
    ) -> tuple[User, list[dict]]:
        user = await self._manageable(ctx, session, user_id)
        await self.admin.reactivate(ctx, session, user, action=audit.USER_REACTIVATED)
        return user, await self.owned_roles(ctx, session, user)

    async def reset_password(self, ctx: TenantContext, session: AsyncSession, user_id: UUID) -> tuple[User, str]:
        user = await self._manageable(ctx, session, user_id)
        return user, await self.admin.reset_password(ctx, session, user)

    async def unlock(self, ctx: TenantContext, session: AsyncSession, user_id: UUID) -> User:
        user = await self._manageable(ctx, session, user_id)
        await self.admin.unlock(ctx, session, user)
        return user

    async def sign_out_everywhere(self, ctx: TenantContext, session: AsyncSession, user_id: UUID) -> User:
        user = await self._manageable(ctx, session, user_id)
        await self.admin.sign_out_everywhere(ctx, session, user)
        return user


def utcnow() -> datetime:
    return datetime.now(UTC)
