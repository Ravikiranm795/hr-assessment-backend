"""Authentication and sessions (spec 4.2, 6/M1).

Public flows (login, TOTP login, Google, refresh) manage their own transactions: a failed login
must still commit its counter, audit row and lockout, so the service records the failure, lets
the transaction commit, and raises afterwards. Authenticated flows receive the request session
so the change and its audit row commit together.
"""

import base64
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, flags, notifications
from app.core.config import settings
from app.core.db import TenantContext, tenant_session
from app.core.errors import AppError
from app.core.roles import ORG_ADMIN, SUPER_ADMIN
from app.core.security import (
    create_access_token,
    decrypt_field,
    dummy_verify,
    encrypt_field,
    hash_password,
    hmac_hash,
    new_opaque_token,
    password_needs_rehash,
    password_policy_violations,
    verify_password,
)
from app.db import Organization, User
from app.modules.m01_auth import repo, totp
from app.modules.m01_auth.google import GoogleVerifier
from app.modules.m01_auth.schemas import (
    BrandingRef,
    MeOut,
    OrganizationOut,
    SessionOut,
    TotpSetupOut,
    TotpVerifyOut,
    UserOut,
)

LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15
TOTP_CHALLENGE_TTL_SEC = 300
DEFAULT_SESSION_TIMEOUT_MIN = 30  # super admins have no organization to configure this
DEFAULT_POLICY = {"min_length": 12, "history": 5, "blocklist": True}
POLICY_MIN_LENGTH_FLOOR = 8

REFRESH_REUSE_REASON = "refresh_reuse_detected"


@dataclass(frozen=True)
class AuthResult:
    """Body plus the raw refresh token the router puts in the cookie (``None``: no cookie change)."""

    body: SessionOut
    refresh_token: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,  # type: ignore[arg-type]
        org_id=user.org_id,
        locale=user.locale,
        must_change_password=user.must_change_password,
        totp_enabled=user.totp_enabled,
        is_default_super_admin=user.is_default_super_admin,
        last_login_at=user.last_login_at,
    )


def _actor_ctx(ctx: TenantContext, user: User) -> TenantContext:
    """Context describing ``user`` as the actor of an audit row (public requests have no identity yet)."""
    return TenantContext(
        org_id=user.org_id,
        user_id=user.id,
        role=user.role,
        is_platform=user.org_id is None,
        request_id=ctx.request_id,
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        actor_type="user",
        actor_name=user.name,
        session_id=ctx.session_id,
    )


def _totp_setup_required(user: User, org: Organization | None) -> bool:
    return bool(
        org is not None and org.totp_required_for_admins and user.role == ORG_ADMIN and not user.totp_enabled
    )


def _policy(org: Organization | None) -> tuple[int, int, bool]:
    raw = {**DEFAULT_POLICY, **((org.password_policy if org else None) or {})}
    return (
        max(int(raw["min_length"]), POLICY_MIN_LENGTH_FLOOR),
        max(int(raw["history"]), 1),
        bool(raw["blocklist"]),
    )


# --------------------------------------------------------------------------- TOTP challenge token


def _sign_challenge(user: User, method: str, now: datetime) -> str:
    body = json.dumps(
        {
            "u": str(user.id),
            "tv": user.token_version,
            "m": method,
            "e": int(now.timestamp()) + TOTP_CHALLENGE_TTL_SEC,
        }
    )
    payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return f"{payload}.{hmac_hash('totp-challenge:' + payload)}"


def _read_challenge(token: str, now: datetime) -> tuple[UUID, int, str]:
    try:
        payload, _, signature = token.partition(".")
        if not hmac.compare_digest(signature, hmac_hash("totp-challenge:" + payload)):
            raise ValueError("bad signature")
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if int(data["e"]) < int(now.timestamp()):
            raise ValueError("expired")
        return UUID(data["u"]), int(data["tv"]), str(data["m"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AppError("UNAUTHENTICATED") from exc


class AuthService:
    # ======================================================================= shared helpers

    def _locked(self, user: User, now: datetime) -> bool:
        return user.locked_until is not None and user.locked_until > now

    def _locked_error(self, user: User) -> AppError:
        until = user.locked_until.isoformat() if user.locked_until else None
        return AppError("ACCOUNT_LOCKED", details={"locked_until": until})

    def _release_expired_lock(self, user: User, now: datetime) -> None:
        if user.locked_until is not None and user.locked_until <= now:
            user.locked_until = None
            user.failed_login_count = 0

    async def _audit_failed(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        user: User | None,
        reason: str,
        *,
        method: str = "password",
        result: str = "failure",
        extra: dict | None = None,
    ) -> None:
        after: dict = {"reason": reason, "method": method, **(extra or {})}
        if user is None:
            actor = ctx
        else:
            actor = _actor_ctx(ctx, user)
            if user.is_default_super_admin:
                after["is_default_super_admin"] = True
        await audit.record(
            actor,
            session,
            audit.AUTH_LOGIN_FAILED,
            "user",
            user.id if user else None,
            after=after,
            result=result,
        )

    async def _register_failure(
        self, session: AsyncSession, ctx: TenantContext, user: User, reason: str, method: str, now: datetime
    ) -> AppError:
        """Count a failed attempt, lock at the threshold, audit, notify admins."""
        user.failed_login_count += 1
        await self._audit_failed(session, ctx, user, reason, method=method)
        if user.failed_login_count < LOCKOUT_THRESHOLD:
            return AppError("INVALID_CREDENTIALS")
        user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
        await audit.record(
            _actor_ctx(ctx, user),
            session,
            audit.AUTH_ACCOUNT_LOCKED,
            "user",
            user.id,
            after={"failed_login_count": user.failed_login_count, "locked_until": user.locked_until},
            result="denied",
        )
        if user.org_id is not None:
            for admin_id in await repo.active_org_admin_ids(session, user.org_id):
                await notifications.notify(
                    session,
                    user.org_id,
                    admin_id,
                    notifications.ACCOUNT_LOCKED,
                    {"user_id": str(user.id), "locked_until": user.locked_until.isoformat()},
                    entity_type="user",
                    entity_id=user.id,
                )
        return self._locked_error(user)

    async def _check_account(
        self, session: AsyncSession, ctx: TenantContext, user: User, method: str
    ) -> tuple[Organization | None, AppError | None]:
        """Active user in an active organization. Called after the credential itself is proven."""
        if user.status != "active":
            await self._audit_failed(session, ctx, user, "inactive_user", method=method)
            return None, AppError("INVALID_CREDENTIALS")
        org = await repo.get_org(session, user.org_id) if user.org_id else None
        if org is not None and org.status != "active":
            await self._audit_failed(session, ctx, user, "org_disabled", method=method)
            return org, AppError("ORG_DISABLED")
        return org, None

    async def _issue_session(
        self, session: AsyncSession, ctx: TenantContext, user: User, now: datetime
    ) -> tuple[str, str, UUID]:
        """New refresh-token family. Returns (access token, raw refresh token, family id)."""
        family_id = uuid4()
        raw = new_opaque_token()
        await repo.add_refresh_token(
            session,
            user_id=user.id,
            family_id=family_id,
            token_hash=hmac_hash(raw),
            expires_at=now + timedelta(days=settings.refresh_ttl_days),
            ip=ctx.ip,
            user_agent=ctx.user_agent,
        )
        return create_access_token(user, family_id), raw, family_id

    def _session_body(self, user: User, org: Organization | None, access_token: str) -> SessionOut:
        return SessionOut(
            access_token=access_token,
            expires_in=settings.access_ttl_min * 60,
            must_change_password=user.must_change_password,
            totp_required=False,
            totp_token=None,
            totp_setup_required=_totp_setup_required(user, org),
            user=_user_out(user),
        )

    async def _complete_login(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        user: User,
        org: Organization | None,
        method: str,
        now: datetime,
    ) -> AuthResult:
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        access, raw, family_id = await self._issue_session(session, ctx, user, now)
        actor = _actor_ctx(ctx, user)
        after = {"method": method, "session_id": str(family_id)}
        await audit.record(actor, session, audit.AUTH_LOGIN_SUCCESS, "user", user.id, after=after)
        if user.is_default_super_admin:
            await audit.record(
                actor,
                session,
                audit.AUTH_DEFAULT_SA_LOGIN,
                "user",
                user.id,
                after={**after, "is_default_super_admin": True},
            )
        return AuthResult(self._session_body(user, org, access), raw)

    async def _after_primary(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        user: User,
        org: Organization | None,
        method: str,
        now: datetime,
    ) -> AuthResult:
        """Primary credential accepted: ask for the second factor, or finish the login."""
        if user.totp_enabled:
            body = SessionOut(
                access_token=None,
                expires_in=None,
                must_change_password=user.must_change_password,
                totp_required=True,
                totp_token=_sign_challenge(user, method, now),
                totp_setup_required=False,
                user=None,
            )
            return AuthResult(body, None)
        return await self._complete_login(session, ctx, user, org, method, now)

    def _second_factor_ok(self, user: User, code: str) -> bool:
        """A current TOTP code, or an unused recovery code (consumed)."""
        if user.totp_secret_enc:
            try:
                secret = decrypt_field(user.totp_secret_enc)
            except ValueError:
                secret = None
            if secret and totp.verify(secret, code):
                return True
        digest = hmac_hash("recovery:" + totp.normalize_recovery_code(code))
        stored = list(user.totp_recovery_codes_hash or [])
        for item in stored:
            if hmac.compare_digest(item, digest):
                user.totp_recovery_codes_hash = [h for h in stored if h != item]
                return True
        return False

    # ======================================================================= public flows

    async def login(self, email: str, password: str, ctx: TenantContext) -> AuthResult:
        failure: AppError | None = None
        result: AuthResult | None = None
        now = _now()
        async with tenant_session(ctx) as session:
            user = await repo.get_user_by_email(session, email)
            if user is None:
                dummy_verify(password)
                await self._audit_failed(
                    session, ctx, None, "unknown_user", extra={"email": email.strip()[:254]}
                )
                failure = AppError("INVALID_CREDENTIALS")
            elif self._locked(user, now):
                dummy_verify(password)
                await self._audit_failed(session, ctx, user, "account_locked", result="denied")
                failure = self._locked_error(user)
            else:
                self._release_expired_lock(user, now)
                if not verify_password(user.password_hash, password):
                    failure = await self._register_failure(
                        session, ctx, user, "wrong_password", "password", now
                    )
                else:
                    org, failure = await self._check_account(session, ctx, user, "password")
                    if failure is None:
                        if not user.is_default_super_admin and password_needs_rehash(user.password_hash):
                            user.password_hash = hash_password(password)  # default SA row is DB-protected
                        result = await self._after_primary(session, ctx, user, org, "password", now)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def login_totp(self, totp_token: str, code: str, ctx: TenantContext) -> AuthResult:
        now = _now()
        user_id, tv, method = _read_challenge(totp_token, now)
        failure: AppError | None = None
        result: AuthResult | None = None
        async with tenant_session(ctx) as session:
            user = await repo.get_user_any_org(session, user_id)
            if user is None or user.token_version != tv or not user.totp_enabled:
                failure = AppError("UNAUTHENTICATED")
            elif self._locked(user, now):
                await self._audit_failed(session, ctx, user, "account_locked", method="totp", result="denied")
                failure = self._locked_error(user)
            else:
                self._release_expired_lock(user, now)
                if not self._second_factor_ok(user, code):
                    failure = await self._register_failure(session, ctx, user, "wrong_totp_code", "totp", now)
                else:
                    org, failure = await self._check_account(session, ctx, user, "totp")
                    if failure is None:
                        result = await self._complete_login(session, ctx, user, org, method, now)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def login_google(self, id_token: str, ctx: TenantContext, verifier: GoogleVerifier) -> AuthResult:
        """Existing active user whose email domain is in the organization's allowed list (A-08).
        No auto-provisioning; an empty allowed list means Google sign-in is off for the organization."""
        now = _now()
        try:
            identity = await verifier.verify(id_token)
            token_error: AppError | None = None
        except AppError as exc:
            identity, token_error = None, exc
        failure: AppError | None = None
        result: AuthResult | None = None
        async with tenant_session(ctx) as session:
            if identity is None:
                await self._audit_failed(session, ctx, None, "invalid_google_token", method="google")
                failure = token_error or AppError("UNAUTHENTICATED")
            else:
                result, failure = await self._google_login(session, ctx, identity, now)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def _google_login(self, session, ctx, identity, now) -> tuple[AuthResult | None, AppError | None]:
        extra = {"email": identity.email[:254]}
        user = await repo.get_user_by_email(session, identity.email) if identity.email else None
        if not identity.email_verified or user is None or user.org_id is None:
            reason = "email_not_verified" if not identity.email_verified else "unknown_user"
            await self._audit_failed(session, ctx, user, reason, method="google", extra=extra)
            return None, AppError("INVALID_CREDENTIALS")
        if self._locked(user, now):
            await self._audit_failed(session, ctx, user, "account_locked", method="google", result="denied")
            return None, self._locked_error(user)
        self._release_expired_lock(user, now)
        org, failure = await self._check_account(session, ctx, user, "google")
        if failure is not None:
            return None, failure
        assert org is not None
        domain = identity.email.rsplit("@", 1)[-1].lower()
        allowed = {d.strip().lower() for d in org.allowed_google_domains or []}
        if domain not in allowed:  # empty list: Google sign-in is off for this organization
            await self._audit_failed(session, ctx, user, "domain_not_allowed", method="google", extra=extra)
            return None, AppError("INVALID_CREDENTIALS")
        if user.google_sub and user.google_sub != identity.sub:
            await self._audit_failed(session, ctx, user, "google_account_mismatch", method="google")
            return None, AppError("INVALID_CREDENTIALS")
        user.google_sub = identity.sub
        return await self._after_primary(session, ctx, user, org, "google", now), None

    async def refresh(self, cookie: str | None, ctx: TenantContext) -> AuthResult:
        if not cookie:
            raise AppError("UNAUTHENTICATED")
        now = _now()
        failure: AppError | None = None
        result: AuthResult | None = None
        async with tenant_session(ctx) as session:
            token = await repo.get_refresh_token_for_update(session, hmac_hash(cookie))
            if token is None:
                failure = AppError("UNAUTHENTICATED")
            else:
                result, failure = await self._rotate(session, ctx, token, now)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def _rotate(self, session, ctx, token, now) -> tuple[AuthResult | None, AppError | None]:
        user = await repo.get_user_any_org(session, token.user_id)
        if token.revoked_at is not None:
            if token.replaced_by is not None and user is not None:
                # An already-rotated token came back: someone holds a copy. Kill the family.
                await repo.revoke_family(session, token.family_id, now)
                await audit.record(
                    _actor_ctx(ctx, user),
                    session,
                    audit.AUTH_LOGOUT,
                    "user",
                    user.id,
                    after={"session_id": str(token.family_id)},
                    reason=REFRESH_REUSE_REASON,
                    result="denied",
                )
            return None, AppError("UNAUTHENTICATED")
        if token.expires_at <= now or user is None or user.status != "active":
            await repo.revoke_family(session, token.family_id, now)
            return None, AppError("UNAUTHENTICATED")
        org = await repo.get_org(session, user.org_id) if user.org_id else None
        if org is not None and org.status != "active":
            return None, AppError("ORG_DISABLED")
        timeout = org.session_timeout_minutes if org else DEFAULT_SESSION_TIMEOUT_MIN
        last_activity = token.last_used_at or token.created_at
        if now - last_activity > timedelta(minutes=timeout):
            await repo.revoke_family(session, token.family_id, now)
            return None, AppError("UNAUTHENTICATED")
        raw = new_opaque_token()
        new = await repo.add_refresh_token(
            session,
            user_id=user.id,
            family_id=token.family_id,
            token_hash=hmac_hash(raw),
            expires_at=token.expires_at,  # the session keeps its original lifetime
            ip=ctx.ip,
            user_agent=ctx.user_agent,
        )
        token.revoked_at = now
        token.replaced_by = new.id
        token.last_used_at = now
        new.last_used_at = now
        access = create_access_token(user, token.family_id)
        return AuthResult(self._session_body(user, org, access), raw), None

    # ======================================================================= authenticated flows

    async def _current_user(self, ctx: TenantContext, session: AsyncSession) -> User:
        user = await repo.get_user(session, ctx.org_id, ctx.user_id) if ctx.user_id else None
        if user is None:
            raise AppError("UNAUTHENTICATED")
        return user

    def _require_editable(self, user: User) -> None:
        if user.is_default_super_admin:
            raise AppError("PROTECTED_ACCOUNT")

    async def logout(self, ctx: TenantContext, session: AsyncSession) -> None:
        user = await self._current_user(ctx, session)
        now = _now()
        revoked = 0
        try:
            family = UUID(ctx.session_id) if ctx.session_id else None
        except ValueError:
            family = None
        if family is not None:
            revoked = await repo.revoke_family(session, family, now, user_id=user.id)
        await audit.record(
            ctx,
            session,
            audit.AUTH_LOGOUT,
            "user",
            user.id,
            after={"session_id": ctx.session_id, "revoked": revoked},
        )

    async def logout_all(self, ctx: TenantContext, session: AsyncSession) -> None:
        user = await self._current_user(ctx, session)
        before = user.token_version
        user.token_version = before + 1
        await repo.revoke_all_for_user(session, user.id, _now())
        await audit.record(
            ctx,
            session,
            audit.AUTH_SIGNED_OUT_EVERYWHERE,
            "user",
            user.id,
            before={"token_version": before},
            after={"token_version": user.token_version},
        )

    async def change_password(
        self, ctx: TenantContext, session: AsyncSession, current_password: str, new_password: str
    ) -> AuthResult:
        user = await self._current_user(ctx, session)
        self._require_editable(user)
        if not verify_password(user.password_hash, current_password):
            raise AppError("INVALID_CREDENTIALS")
        org = await repo.get_org(session, user.org_id) if user.org_id else None
        min_length, history, use_blocklist = _policy(org)
        kwargs = {} if use_blocklist else {"blocklist": frozenset()}
        violations = password_policy_violations(new_password, min_length=min_length, **kwargs)
        previous = [user.password_hash, *await repo.recent_password_hashes(session, user.id, history)]
        if any(verify_password(old, new_password) for old in previous):
            violations.append("reused_password")
        if violations:
            raise AppError("PASSWORD_POLICY_FAILED", details={"violations": violations})

        now = _now()
        # Remember the outgoing password if it was never recorded, then the new one, newest last.
        if user.password_hash not in previous[1:]:
            await repo.add_password_history(session, user.id, user.password_hash, now)
        new_hash = hash_password(new_password)
        await repo.add_password_history(session, user.id, new_hash, now + timedelta(microseconds=1))
        await repo.prune_password_history(session, user.id, history)

        was_forced = user.must_change_password
        user.password_hash = new_hash
        user.must_change_password = False
        user.token_version += 1  # kills every existing access token
        await repo.revoke_all_for_user(session, user.id, now)
        await audit.record(
            ctx,
            session,
            audit.AUTH_PASSWORD_CHANGED,
            "user",
            user.id,
            before={"must_change_password": was_forced},
            after={"must_change_password": False, "token_version": user.token_version},
        )
        access, raw, _ = await self._issue_session(session, ctx, user, now)
        return AuthResult(self._session_body(user, org, access), raw)

    async def get_me(self, ctx: TenantContext, session: AsyncSession) -> MeOut:
        user = await self._current_user(ctx, session)
        org = await repo.get_org(session, user.org_id) if user.org_id else None
        effective = await flags.effective_map(session, ctx.org_id, include_hidden=user.role == SUPER_ADMIN)
        timeout = org.session_timeout_minutes if org else DEFAULT_SESSION_TIMEOUT_MIN
        branding = (
            BrandingRef(scope="org", endpoint="/api/v1/org/branding")
            if org
            else BrandingRef(scope="platform", endpoint="/api/v1/platform/branding")
        )
        return MeOut(
            user=_user_out(user),
            organization=OrganizationOut.model_validate(org, from_attributes=True) if org else None,
            flags=effective,
            locale=user.locale or (org.locale if org else None) or "en",
            branding=branding,
            session_timeout_minutes=timeout,
            totp_setup_required=_totp_setup_required(user, org),
        )

    # ----------------------------------------------------------------------- TOTP management

    async def totp_setup(self, ctx: TenantContext, session: AsyncSession) -> TotpSetupOut:
        user = await self._current_user(ctx, session)
        self._require_editable(user)
        if user.totp_enabled:
            raise AppError("CONFLICT", details={"reason": "totp_already_enabled"})
        secret = totp.generate_secret()
        user.totp_secret_enc = encrypt_field(secret)  # pending until /auth/totp/verify
        return TotpSetupOut(
            secret=secret, otpauth_uri=totp.otpauth_uri(secret, user.email, settings.app_name)
        )

    async def totp_verify(self, ctx: TenantContext, session: AsyncSession, code: str) -> TotpVerifyOut:
        user = await self._current_user(ctx, session)
        self._require_editable(user)
        if user.totp_enabled or not user.totp_secret_enc:
            raise AppError("CONFLICT", details={"reason": "totp_not_pending"})
        if not totp.verify(decrypt_field(user.totp_secret_enc), code):
            raise AppError("INVALID_CREDENTIALS")
        codes = totp.generate_recovery_codes()
        user.totp_enabled = True
        user.totp_recovery_codes_hash = [
            hmac_hash("recovery:" + totp.normalize_recovery_code(c)) for c in codes
        ]
        await audit.record(
            ctx,
            session,
            audit.AUTH_TOTP_ENABLED,
            "user",
            user.id,
            before={"totp_enabled": False},
            after={"totp_enabled": True},
        )
        return TotpVerifyOut(enabled=True, recovery_codes=codes)

    async def totp_disable(self, ctx: TenantContext, session: AsyncSession, password: str, code: str) -> None:
        user = await self._current_user(ctx, session)
        self._require_editable(user)
        org = await repo.get_org(session, user.org_id) if user.org_id else None
        if not user.totp_enabled:
            raise AppError("CONFLICT", details={"reason": "totp_not_enabled"})
        if org is not None and org.totp_required_for_admins and user.role == ORG_ADMIN:
            raise AppError("FORBIDDEN", details={"reason": "totp_required_by_org"})
        if not verify_password(user.password_hash, password) or not self._second_factor_ok(user, code):
            raise AppError("INVALID_CREDENTIALS")
        user.totp_enabled = False
        user.totp_secret_enc = None
        user.totp_recovery_codes_hash = None
        await audit.record(
            ctx,
            session,
            audit.AUTH_TOTP_DISABLED,
            "user",
            user.id,
            before={"totp_enabled": True},
            after={"totp_enabled": False},
        )


service = AuthService()
