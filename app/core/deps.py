"""Request dependencies: authentication, tenant context, DB session and RBAC/flag guards.

Every guard carries a ``__guard__`` marker; ``install_openapi_extensions(app)`` walks each
route's dependency tree and writes ``x-roles`` and ``x-required-flag`` into the operation.
Routes with no guard get ``x-roles: []`` and ``x-required-flag: null`` (public).
"""

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import flags
from app.core.db import TenantContext, tenant_session
from app.core.errors import AppError
from app.core.openapi_meta import enrich
from app.core.roles import CANDIDATE, HR_USER, ORG_ADMIN, STAFF_ROLES
from app.core.security import EXAM_AUDIENCE, Claims, decode_token

__all__ = [
    "AppError",
    "DbSession",
    "ExamContext",
    "Principal",
    "current_ctx",
    "current_ctx_allow_password_change",
    "current_exam_ctx",
    "db_session",
    "exam_db_session",
    "install_openapi_extensions",
    "public_ctx",
    "public_db_session",
    "require_feature",
    "require_owner_or_admin",
    "require_role",
]


@dataclass(frozen=True)
class GuardInfo:
    roles: tuple[str, ...] | None = None
    flag: str | None = None


def _mark(fn: Callable, roles: tuple[str, ...] | None = None, flag: str | None = None) -> Callable:
    fn.__guard__ = GuardInfo(roles=roles, flag=flag)  # type: ignore[attr-defined]
    return fn


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def _client(request: Request) -> tuple[str | None, str | None]:
    return (request.client.host if request.client else None, request.headers.get("user-agent"))


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AppError("UNAUTHENTICATED")
    return token.strip()


# --------------------------------------------------------------------------- staff authentication


@dataclass
class Principal:
    user_id: UUID
    org_id: UUID | None
    role: str
    status: str
    token_version: int
    must_change_password: bool
    name: str | None
    org_status: str | None


_PRINCIPAL_SQL = text(
    """
    SELECT u.id AS user_id, u.org_id AS org_id, u.role AS role, u.status AS status,
           u.token_version AS token_version, u.must_change_password AS must_change_password,
           u.name AS name, o.status AS org_status
    FROM users u LEFT JOIN organizations o ON o.id = u.org_id
    WHERE u.id = :user_id
    """
)


async def load_principal(claims: Claims) -> Principal | None:
    """Load the user behind a token (platform-mode read; the row is matched by id only)."""
    lookup_ctx = TenantContext(
        org_id=None,
        user_id=None,
        role="system",
        is_platform=True,
        request_id="auth",
        ip=None,
        user_agent=None,
    )
    async with tenant_session(lookup_ctx) as session:
        row = (await session.execute(_PRINCIPAL_SQL, {"user_id": UUID(claims.sub)})).mappings().first()
    return Principal(**row) if row else None


async def _authenticate(request: Request, *, allow_password_change: bool) -> TenantContext:
    claims = decode_token(_bearer(request))
    principal = await load_principal(claims)
    if (
        principal is None
        or principal.status != "active"
        or principal.token_version != claims.tv
        or str(principal.org_id or "") != str(claims.org_id or "")
    ):
        raise AppError("UNAUTHENTICATED")
    if principal.org_id is not None and principal.org_status != "active":
        raise AppError("ORG_DISABLED")
    if principal.must_change_password and not allow_password_change:
        raise AppError("MUST_CHANGE_PASSWORD")
    ip, user_agent = _client(request)
    ctx = TenantContext(
        org_id=principal.org_id,
        user_id=principal.user_id,
        role=principal.role,
        is_platform=principal.org_id is None,
        request_id=_request_id(request),
        ip=ip,
        user_agent=user_agent,
        actor_type="user",
        actor_name=principal.name,
        session_id=claims.sid,
    )
    request.state.ctx = ctx
    return ctx


async def current_ctx(request: Request) -> TenantContext:
    """Authenticated staff context: valid access token, active user, matching ``tv``, active org,
    and no pending forced password change. Super admins get ``is_platform=True``."""
    return await _authenticate(request, allow_password_change=False)


async def current_ctx_allow_password_change(request: Request) -> TenantContext:
    """Like ``current_ctx`` but allowed while ``must_change_password`` is set (change-password, me)."""
    return await _authenticate(request, allow_password_change=True)


_mark(current_ctx, roles=STAFF_ROLES)
_mark(current_ctx_allow_password_change, roles=STAFF_ROLES)


async def db_session(ctx: TenantContext = Depends(current_ctx)) -> AsyncIterator[AsyncSession]:
    """Request transaction with RLS set from the staff context. Use via ``DbSession``."""
    async with tenant_session(ctx) as session:
        yield session


# scope="function": commit happens before the response is sent, so a failed commit is a 500.
DbSession = Annotated[AsyncSession, Depends(db_session, scope="function")]


# --------------------------------------------------------------------------- public and candidate


def public_ctx(request: Request) -> TenantContext:
    """Anonymous context for public endpoints (login, exam open). Platform mode: services MUST
    filter by org explicitly, and switch to an org-scoped context once the org is known."""
    ip, user_agent = _client(request)
    return TenantContext(
        org_id=None,
        user_id=None,
        role="anonymous",
        is_platform=True,
        request_id=_request_id(request),
        ip=ip,
        user_agent=user_agent,
    )


async def public_db_session(ctx: TenantContext = Depends(public_ctx)) -> AsyncIterator[AsyncSession]:
    async with tenant_session(ctx) as session:
        yield session


@dataclass(frozen=True)
class ExamContext:
    ctx: TenantContext  # org-scoped, role "candidate", actor_type "candidate"
    attempt_id: UUID
    invite_id: UUID
    binding: str | None


async def current_exam_ctx(request: Request) -> ExamContext:
    """Candidate exam session (audience ``exam``). The binding-cookie comparison is done by M8."""
    claims = decode_token(_bearer(request), audience=EXAM_AUDIENCE)
    if claims.role != CANDIDATE or not claims.org_id:
        raise AppError("UNAUTHENTICATED")
    ip, user_agent = _client(request)
    ctx = TenantContext(
        org_id=UUID(claims.org_id),
        user_id=None,
        role=CANDIDATE,
        is_platform=False,
        request_id=_request_id(request),
        ip=ip,
        user_agent=user_agent,
        actor_type="candidate",
        session_id=claims.sid,
    )
    request.state.ctx = ctx
    return ExamContext(
        ctx=ctx, attempt_id=UUID(claims.sub), invite_id=UUID(claims.sid), binding=claims.binding
    )


_mark(current_exam_ctx, roles=(CANDIDATE,))


async def exam_db_session(exam: ExamContext = Depends(current_exam_ctx)) -> AsyncIterator[AsyncSession]:
    async with tenant_session(exam.ctx) as session:
        yield session


# --------------------------------------------------------------------------- guards


def require_role(*roles: str) -> Callable[..., Any]:
    """Allow only the given roles; returns the ``TenantContext``. 403 ``FORBIDDEN`` otherwise."""
    allowed = tuple(roles)

    async def guard(ctx: TenantContext = Depends(current_ctx)) -> TenantContext:
        if ctx.role not in allowed:
            raise AppError("FORBIDDEN")
        return ctx

    return _mark(guard, roles=allowed)


def require_owner_or_admin(get_owner_id: Callable[..., Any]) -> Callable[..., Any]:
    """Allow org admins and the resource owner. ``get_owner_id`` is itself a FastAPI dependency
    (it may take path params and ``DbSession``) returning the owner's UUID, or ``None`` when the
    resource does not exist in the caller's org (then 404, so foreign ids cannot be probed)."""

    async def guard(
        ctx: TenantContext = Depends(current_ctx), owner_id: UUID | None = Depends(get_owner_id)
    ) -> TenantContext:
        if owner_id is None:
            raise AppError("NOT_FOUND")
        if ctx.role == ORG_ADMIN or (ctx.role == HR_USER and ctx.user_id == owner_id):
            return ctx
        raise AppError("FORBIDDEN")

    return _mark(guard, roles=(ORG_ADMIN, HR_USER))


def require_feature(key: str) -> Callable[..., Any]:
    """403 ``FEATURE_DISABLED`` (``details.flag``) when the flag is off for the caller's org.
    Platform callers (no org) are not flag-gated."""
    if key not in flags.FLAG_KEYS:
        raise ValueError(f"unknown feature flag {key!r} (spec Appendix E)")

    async def guard(session: DbSession, ctx: Annotated[TenantContext, Depends(current_ctx)]) -> TenantContext:
        if ctx.org_id is not None and not await flags.is_enabled(session, ctx.org_id, key):
            raise AppError("FEATURE_DISABLED", 403, {"flag": key})
        return ctx

    return _mark(guard, flag=key)


# --------------------------------------------------------------------------- OpenAPI


def _collect(dependant: Any, roles: list[str] | None, found_flags: list[str]) -> list[str] | None:
    for sub in dependant.dependencies:
        info: GuardInfo | None = getattr(sub.call, "__guard__", None)
        if info is not None:
            if info.roles is not None:
                roles = list(info.roles) if roles is None else [r for r in roles if r in info.roles]
            if info.flag and info.flag not in found_flags:
                found_flags.append(info.flag)
        roles = _collect(sub, roles, found_flags)
    return roles


def guard_extensions(route: Any) -> tuple[list[str] | None, str | list[str] | None]:
    """(x-roles, x-required-flag) for a route (``APIRoute`` or FastAPI ``RouteContext``);
    roles ``None`` means no guard found."""
    found_flags: list[str] = []
    roles = _collect(route.dependant, None, found_flags)
    flag: str | list[str] | None = None
    if found_flags:
        flag = found_flags[0] if len(found_flags) == 1 else found_flags
    return roles, flag


def install_openapi_extensions(app: FastAPI) -> None:
    """Wrap ``app.openapi`` so every operation declares ``x-roles`` and ``x-required-flag``."""
    original = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = original()
        # FastAPI >= 0.16x keeps included routers lazy (_IncludedRouter); iter_route_contexts
        # flattens them with the router-level dependencies applied, as get_openapi does.
        for route in iter_route_contexts(app.routes):
            if not isinstance(route.original_route, APIRoute) or not route.include_in_schema:
                continue
            roles, flag = guard_extensions(route)
            path = schema.get("paths", {}).get(route.path_format, {})
            for method in route.methods or ():
                operation = path.get(method.lower())
                if operation is None:
                    continue
                if roles is not None:
                    operation["x-roles"] = roles
                    operation["x-required-flag"] = flag
                else:
                    operation.setdefault("x-roles", [])
                    operation.setdefault("x-required-flag", flag)
        enrich(schema)  # security schemes, per-operation security, generated response examples
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
