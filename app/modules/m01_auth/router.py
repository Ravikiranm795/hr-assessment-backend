"""M1 routes: ``/api/v1/auth/*`` (spec 6/M1). Core module: no feature flag."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import TenantContext, tenant_session
from app.core.deps import (
    DbSession,
    current_ctx_allow_password_change,
    public_ctx,
    require_role,
)
from app.core.errors import AppError, error_response
from app.core.ratelimit import rate_limit
from app.core.roles import ORG_ADMIN, STAFF_ROLES, SUPER_ADMIN
from app.modules.m01_auth.google import GoogleVerifier, get_google_verifier
from app.modules.m01_auth.schemas import (
    ChangePasswordIn,
    GoogleLoginIn,
    LoginIn,
    MeOut,
    OkOut,
    SessionOut,
    TotpDisableIn,
    TotpLoginIn,
    TotpSetupOut,
    TotpVerifyIn,
    TotpVerifyOut,
    error_responses,
)
from app.modules.m01_auth.service import AuthResult, service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

REFRESH_COOKIE = settings.refresh_cookie_name
COOKIE_PATH = "/api/v1/auth"
_PUBLIC = {"x-roles": [], "x-required-flag": None}

_login_limit = Depends(rate_limit(lambda: settings.rate_limit_login, scope="ip", name="login"))
_user_limit = rate_limit(lambda: settings.rate_limit_login, scope="user", name="auth_sensitive")

Public = Annotated[TenantContext, Depends(public_ctx)]
# Allowed while a forced password change is pending (change-password, me, logout).
Staff = Annotated[TenantContext, Depends(current_ctx_allow_password_change)]
AnyStaff = Annotated[TenantContext, Depends(require_role(*STAFF_ROLES))]
Admin = Annotated[TenantContext, Depends(require_role(SUPER_ADMIN, ORG_ADMIN))]
RefreshCookie = Annotated[
    str | None,
    Cookie(alias=REFRESH_COOKIE, description="HttpOnly refresh token set by login; rotated on every use"),
]


async def _allow_change_session(ctx: Staff) -> AsyncIterator[AsyncSession]:
    async with tenant_session(ctx) as session:
        yield session


AuthSession = Annotated[AsyncSession, Depends(_allow_change_session, scope="function")]


def _set_refresh_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=settings.refresh_ttl_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path=COOKIE_PATH,
        domain=settings.cookie_domain or None,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=COOKIE_PATH,
        domain=settings.cookie_domain or None,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _respond(result: AuthResult, response: Response) -> SessionOut:
    response.headers["Cache-Control"] = "no-store"
    if result.refresh_token:
        _set_refresh_cookie(response, result.refresh_token)
    return result.body


@router.post(
    "/login",
    response_model=SessionOut,
    summary="Sign in with email and password",
    description=(
        "Constant-time credential check with a generic `INVALID_CREDENTIALS` for an unknown email or a "
        "wrong password. Five consecutive failures lock the account for 15 minutes (`ACCOUNT_LOCKED`). "
        "On success the access token is returned in the body and the rotating refresh token is set as an "
        "HttpOnly, SameSite=Strict cookie. If the user has an authenticator app, `totp_required` is true, "
        "no token is issued, and the client completes the login at `/auth/login/totp` with `totp_token`. "
        "`must_change_password` true means only `/auth/change-password`, `/auth/me` and `/auth/logout` work."
    ),
    responses=error_responses("INVALID_CREDENTIALS", "ACCOUNT_LOCKED", "ORG_DISABLED", "RATE_LIMITED"),
    dependencies=[_login_limit],
    openapi_extra=_PUBLIC,
)
async def login(body: LoginIn, ctx: Public, response: Response) -> SessionOut:
    return _respond(await service.login(body.email, body.password, ctx), response)


@router.post(
    "/login/totp",
    response_model=SessionOut,
    summary="Complete a sign-in with the authenticator code",
    description=(
        "Second step after `/auth/login` or `/auth/google` returned `totp_required`. Accepts the 6-digit "
        "code or a single-use recovery code. Wrong codes count toward the same lockout as wrong passwords. "
        "The challenge `totp_token` expires after 5 minutes."
    ),
    responses=error_responses(
        "INVALID_CREDENTIALS", "ACCOUNT_LOCKED", "UNAUTHENTICATED", "ORG_DISABLED", "RATE_LIMITED"
    ),
    dependencies=[_login_limit],
    openapi_extra=_PUBLIC,
)
async def login_totp(body: TotpLoginIn, ctx: Public, response: Response) -> SessionOut:
    return _respond(await service.login_totp(body.totp_token, body.code, ctx), response)


@router.post(
    "/google",
    response_model=SessionOut,
    summary="Sign in with a Google ID token",
    description=(
        "The server verifies the ID token and requires a verified email that belongs to an existing, active "
        "user whose email domain is in the organization's allowed Google domains. There is no "
        "auto-provisioning. An organization with an empty allowed list has Google sign-in switched off. "
        "Response and cookie are the same as `/auth/login`."
    ),
    responses=error_responses(
        "INVALID_CREDENTIALS", "UNAUTHENTICATED", "ACCOUNT_LOCKED", "ORG_DISABLED", "RATE_LIMITED"
    ),
    dependencies=[_login_limit],
    openapi_extra=_PUBLIC,
)
async def google_login(
    body: GoogleLoginIn,
    ctx: Public,
    response: Response,
    verifier: Annotated[GoogleVerifier, Depends(get_google_verifier)],
) -> SessionOut:
    return _respond(await service.login_google(body.id_token, ctx, verifier), response)


@router.post(
    "/refresh",
    response_model=SessionOut,
    summary="Rotate the refresh token and get a new access token",
    description=(
        "Reads the refresh cookie, issues a new access token and replaces the cookie. Every refresh token "
        "works once: presenting an already-rotated token revokes the whole token family (the session ends "
        "everywhere it was copied). A session idle for longer than the organization's timeout (default 30 "
        "minutes) cannot be refreshed. Failures clear the cookie."
    ),
    responses=error_responses("UNAUTHENTICATED", "ORG_DISABLED"),
    openapi_extra=_PUBLIC,
)
async def refresh(request: Request, ctx: Public, response: Response, refresh_cookie: RefreshCookie = None):
    try:
        result = await service.refresh(refresh_cookie, ctx)
    except AppError as exc:
        failed = error_response(request, exc)
        _clear_refresh_cookie(failed)
        return failed
    return _respond(result, response)


@router.post(
    "/logout",
    response_model=OkOut,
    summary="Sign out this session",
    description=(
        "Revokes the refresh-token family of the calling session and clears the cookie. Allowed while a "
        "forced password change is pending. The access token stays valid until it expires (15 minutes); "
        "use `/auth/logout-all` to kill every token immediately."
    ),
    responses=error_responses("UNAUTHENTICATED"),
)
async def logout(ctx: Staff, session: AuthSession, response: Response) -> OkOut:
    await service.logout(ctx, session)
    _clear_refresh_cookie(response)
    return OkOut()


@router.post(
    "/logout-all",
    response_model=OkOut,
    summary="Sign out everywhere",
    description=(
        "Bumps the user's token version, which invalidates every access token at once, and revokes all "
        "refresh tokens."
    ),
    responses=error_responses("UNAUTHENTICATED", "MUST_CHANGE_PASSWORD"),
)
async def logout_all(ctx: AnyStaff, session: DbSession, response: Response) -> OkOut:
    await service.logout_all(ctx, session)
    _clear_refresh_cookie(response)
    return OkOut()


@router.post(
    "/change-password",
    response_model=SessionOut,
    summary="Change the password",
    description=(
        "Required on first login and after a temporary password was issued. Checks the current password, "
        "the organization's password policy (minimum length, common-password blocklist) and that none of the "
        "last 5 passwords is reused (`PASSWORD_POLICY_FAILED`, `details.violations`). Success bumps the "
        "token version (all old tokens die) and returns a fresh session so the user stays signed in. The "
        "default super admin password is protected (`PROTECTED_ACCOUNT`)."
    ),
    responses=error_responses(
        "INVALID_CREDENTIALS",
        "PASSWORD_POLICY_FAILED",
        "PROTECTED_ACCOUNT",
        "UNAUTHENTICATED",
        "RATE_LIMITED",
    ),
)
async def change_password(
    body: ChangePasswordIn,
    ctx: Staff,
    _limit: Annotated[None, Depends(_user_limit)],
    session: AuthSession,
    response: Response,
) -> SessionOut:
    return _respond(
        await service.change_password(ctx, session, body.current_password, body.new_password), response
    )


@router.post(
    "/totp/setup",
    response_model=TotpSetupOut,
    summary="Start authenticator-app setup",
    description=(
        "Generates a secret for the calling admin. It stays pending until `/auth/totp/verify` confirms a "
        "code. Calling it again before verifying replaces the pending secret."
    ),
    responses=error_responses("CONFLICT", "PROTECTED_ACCOUNT", "FORBIDDEN", "UNAUTHENTICATED"),
)
async def totp_setup(ctx: Admin, session: DbSession) -> TotpSetupOut:
    return await service.totp_setup(ctx, session)


@router.post(
    "/totp/verify",
    response_model=TotpVerifyOut,
    summary="Confirm authenticator-app setup",
    description="Checks a code from the app, turns the second factor on and returns recovery codes once.",
    responses=error_responses(
        "INVALID_CREDENTIALS", "CONFLICT", "PROTECTED_ACCOUNT", "RATE_LIMITED", "FORBIDDEN"
    ),
)
async def totp_verify(
    body: TotpVerifyIn,
    ctx: Admin,
    _limit: Annotated[None, Depends(_user_limit)],
    session: DbSession,
) -> TotpVerifyOut:
    return await service.totp_verify(ctx, session, body.code)


@router.post(
    "/totp/disable",
    response_model=OkOut,
    summary="Turn the authenticator app off",
    description=(
        "Needs the current password and a valid code (or a recovery code). Blocked with `FORBIDDEN` "
        "(`details.reason = totp_required_by_org`) for an org admin whose organization requires TOTP."
    ),
    responses=error_responses(
        "INVALID_CREDENTIALS", "CONFLICT", "FORBIDDEN", "PROTECTED_ACCOUNT", "RATE_LIMITED"
    ),
)
async def totp_disable(
    body: TotpDisableIn,
    ctx: Admin,
    _limit: Annotated[None, Depends(_user_limit)],
    session: DbSession,
) -> OkOut:
    await service.totp_disable(ctx, session, body.password, body.code)
    return OkOut()


@router.get(
    "/me",
    response_model=MeOut,
    summary="Current user, organization and effective feature flags",
    description=(
        "Profile, role, organization, the effective feature-flag map (flags with `visible_to_org = false`, "
        "such as the watermark, only for super admins), locale, and the branding reference telling the "
        "client where to read the theme. Allowed while a forced password change is pending."
    ),
    responses=error_responses("UNAUTHENTICATED", "ORG_DISABLED"),
)
async def me(ctx: Staff, session: AuthSession) -> MeOut:
    return await service.get_me(ctx, session)
