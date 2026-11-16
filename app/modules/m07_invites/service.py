"""M7 rules: invites (exam URL once, hashed token, blind-indexed email), accommodations, help requests."""

import html
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, security
from app.core.config import settings
from app.core.db import TenantContext
from app.core.email import OutgoingEmail, get_email_sender
from app.core.errors import AppError
from app.core.pagination import PageParams
from app.core.ratelimit import check_rate_limit
from app.db import Attempt, HelpRequest, Invite
from app.modules.m04_org_settings.service import EmailSettingsService
from app.modules.m06_roles.hooks import on_role_close
from app.modules.m06_roles.service import RoleService
from app.modules.m07_invites import csv_import, repo
from app.modules.m07_invites.schemas import (
    AccommodationsIn,
    ApproveIn,
    AttemptSummary,
    BulkResult,
    BulkRowResult,
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
)

MAX_EXPIRY = timedelta(hours=720)


def _now() -> datetime:
    return datetime.now(UTC)


def exam_url(token: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/exam/{token}"


def _normalize_email(email: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKC", email).strip()


def _attempt(a: Attempt | None) -> AttemptSummary | None:
    if a is None:
        return None
    return AttemptSummary(
        id=a.id, status=a.status,
        score_percent=float(a.score_percent) if a.score_percent is not None else None,
        passed=a.passed, decision=a.decision, flag_counts=a.flag_counts or {}, submitted_at=a.submitted_at,
    )  # fmt: skip


def _decrypt(blob: str | None) -> str | None:
    if not blob:
        return None
    try:
        return security.decrypt_field(blob)
    except ValueError:
        return None


def detail(invite: Invite, attempt: Attempt | None) -> InviteDetail:
    return InviteDetail(
        id=invite.id, role_id=invite.role_id, assessment_version_id=invite.assessment_version_id,
        candidate_name=invite.candidate_name, candidate_email=_decrypt(invite.candidate_email_enc),
        candidate_phone=_decrypt(invite.candidate_phone_enc), status=invite.status,
        expires_at=invite.expires_at,
        camera_exempt=invite.camera_exempt, time_multiplier=float(invite.time_multiplier),
        extra_minutes=invite.extra_minutes, accommodation_reason=invite.accommodation_reason,
        duplicate_override=invite.duplicate_override, session_bound=invite.session_binding_hash is not None,
        created_at=invite.created_at, attempt=_attempt(attempt),
    )  # fmt: skip


async def _invite(session: AsyncSession, org_id: UUID, invite_id: UUID, *, lock: bool = False) -> Invite:
    invite = await repo.get(session, org_id, invite_id, lock=lock)
    if invite is None:
        raise AppError("NOT_FOUND")
    return invite


def _apply_accommodations(invite: Invite, acc: AccommodationsIn, by: UUID | None) -> dict:
    changes = {}
    for field in ("camera_exempt", "time_multiplier", "extra_minutes"):
        value = getattr(acc, field)
        if value is not None:
            setattr(invite, field, value)
            changes[field] = float(value) if isinstance(value, Decimal) else value
    invite.accommodation_reason, invite.accommodation_by = acc.reason, by
    return changes


class InviteService:
    @staticmethod
    async def create(
        ctx: TenantContext, session: AsyncSession, role_id: UUID, body: InviteCreate, *, bulk: bool = False
    ) -> tuple[Invite, str]:
        """``(invite, exam_url)``. The role must be open with a linked published assessment (RF-1: the
        email is normalized before the blind index, so case and whitespace variants collide)."""
        _, version_id = await RoleService.get_open_linked_role(session, ctx.org_id, role_id)
        email = _normalize_email(str(body.email))
        email_hash = security.blind_index(email)
        duplicate = await repo.email_exists(session, ctx.org_id, role_id, email_hash)
        if duplicate and not body.allow_duplicate:
            raise AppError("DUPLICATE_INVITE")
        hours = body.expires_in_hours or await repo.default_expiry_hours(session, ctx.org_id)
        token = security.new_opaque_token()
        invite = Invite(
            org_id=ctx.org_id, role_id=role_id, assessment_version_id=version_id,
            candidate_name=body.candidate_name, candidate_email_enc=security.encrypt_field(email),
            candidate_email_hash=email_hash,
            candidate_phone_enc=security.encrypt_field(body.phone) if body.phone else None,
            token_hash=security.hmac_hash(token), status="invited",
            expires_at=_now() + timedelta(hours=hours),
            duplicate_override=duplicate, created_by=ctx.user_id,
        )  # fmt: skip
        if body.accommodations:
            _apply_accommodations(invite, body.accommodations, ctx.user_id)
        session.add(invite)
        await repo.flush(session)
        if not bulk:
            await audit.record(
                ctx, session, audit.INVITE_CREATED, "invite", invite.id,
                after={"role_id": role_id, "assessment_version_id": version_id,
                       "expires_at": invite.expires_at,
                       "duplicate_override": duplicate},
            )  # fmt: skip
        return invite, exam_url(token)

    @staticmethod
    async def create_out(
        ctx: TenantContext, session: AsyncSession, role_id: UUID, body: InviteCreate
    ) -> InviteCreated:
        invite, url = await InviteService.create(ctx, session, role_id, body)
        sent = False
        if body.send_email:
            await InviteService._email(ctx, session, invite, url)
            sent = True
        return InviteCreated(invite=detail(invite, None), exam_url=url, email_sent=sent)

    @staticmethod
    async def bulk(ctx: TenantContext, session: AsyncSession, role_id: UUID, data: bytes) -> BulkResult:
        await RoleService.get_open_linked_role(session, ctx.org_id, role_id)
        rows = csv_import.parse(data)
        results: list[BulkRowResult] = []
        for row in rows:
            if row.error:
                results.append(BulkRowResult(row=row.row, status="error", code=row.error))
                continue
            try:
                async with session.begin_nested():
                    invite, url = await InviteService.create(
                        ctx, session, role_id,
                        InviteCreate(candidate_name=row.name, email=row.email, phone=row.phone), bulk=True,
                    )  # fmt: skip
            except AppError as exc:
                results.append(BulkRowResult(row=row.row, status="error", code=exc.code))
                continue
            results.append(BulkRowResult(row=row.row, status="created", invite_id=invite.id, exam_url=url))
        created = sum(r.status == "created" for r in results)
        await audit.record(
            ctx, session, audit.INVITE_BULK_CREATED, "open_role", role_id,
            after={"rows": len(rows), "created": created, "failed": len(rows) - created},
        )  # fmt: skip
        return BulkResult(total=len(rows), created=created, failed=len(rows) - created, rows=results)

    @staticmethod
    async def list(
        ctx: TenantContext, session: AsyncSession, role_id: UUID, params: PageParams,
        *, status: str | None, q: str | None, full: bool,
    ) -> tuple[list[InviteRow] | list[InviteRowLimited], int]:  # fmt: skip
        if await repo.role_owner(session, ctx.org_id, role_id) is None:
            raise AppError("NOT_FOUND")
        pairs, total = await repo.search(
            session, ctx.org_id, role_id, status=status, q=q, offset=params.offset, limit=params.limit
        )
        if not full:
            return [
                InviteRowLimited(id=i.id, candidate_name=i.candidate_name, status=i.status,
                                 decision=a.decision if a else None)
                for i, a in pairs
            ], total  # fmt: skip
        return [
            InviteRow(
                id=i.id, candidate_name=i.candidate_name, candidate_email=_decrypt(i.candidate_email_enc),
                status=i.status, expires_at=i.expires_at, created_at=i.created_at,
                assessment_version_id=i.assessment_version_id,
                accommodations=bool(i.accommodation_reason), attempt=_attempt(a),
            )
            for i, a in pairs
        ], total  # fmt: skip

    @staticmethod
    async def get(org_id: UUID, session: AsyncSession, invite_id: UUID) -> InviteDetail:
        invite = await _invite(session, org_id, invite_id)
        return detail(invite, await repo.attempt_for(session, org_id, invite_id))

    @staticmethod
    async def _rotate(ctx: TenantContext, session: AsyncSession, invite_id: UUID) -> tuple[Invite, str]:
        invite = await _invite(session, ctx.org_id, invite_id, lock=True)
        if invite.status != "invited":
            raise AppError("CONFLICT", details={"reason": "invite_not_unstarted", "status": invite.status})
        token = security.new_opaque_token()
        invite.token_hash = security.hmac_hash(token)
        await session.flush()
        return invite, exam_url(token)

    @staticmethod
    async def regenerate_link(ctx: TenantContext, session: AsyncSession, invite_id: UUID) -> LinkOut:
        invite, url = await InviteService._rotate(ctx, session, invite_id)
        await audit.record(ctx, session, audit.INVITE_REGENERATED, "invite", invite.id)
        return LinkOut(invite_id=invite.id, exam_url=url)

    @staticmethod
    async def _email(ctx: TenantContext, session: AsyncSession, invite: Invite, url: str) -> None:
        smtp = await EmailSettingsService(session).smtp_for_sending(ctx.org_id)
        to = _decrypt(invite.candidate_email_enc)
        if not to:
            raise AppError("CONFLICT", details={"reason": "candidate_email_unavailable"})
        check_rate_limit(settings.rate_limit_invite_email, "invite_email", "org", str(ctx.org_id))
        title = html.escape(await repo.role_title(session, ctx.org_id, invite.role_id) or "")
        name = html.escape(invite.candidate_name)
        expires = invite.expires_at.strftime("%Y-%m-%d %H:%M UTC")
        await get_email_sender().send(
            smtp,
            OutgoingEmail(
                to=to,
                subject=f"{settings.app_name}: your assessment",
                html=f"<p>Hello {name},</p><p>You are invited to complete an assessment for {title}.</p>"
                f'<p><a href="{url}">Start the assessment</a></p><p>The link expires {expires}.</p>',
                text=f"Hello {invite.candidate_name}, open {url} to complete your assessment. "
                f"Expires {expires}.",
            ),
        )
        await audit.record(ctx, session, audit.INVITE_EMAIL_SENT, "invite", invite.id)

    @staticmethod
    async def send_email(ctx: TenantContext, session: AsyncSession, invite_id: UUID) -> EmailSentOut:
        """Needs a verified SMTP setup. The raw token is not stored, so the link is rotated and the new
        one is emailed (it is not returned)."""
        await EmailSettingsService(session).smtp_for_sending(ctx.org_id)  # fail early, before rotating
        invite, url = await InviteService._rotate(ctx, session, invite_id)
        await audit.record(ctx, session, audit.INVITE_REGENERATED, "invite", invite.id, reason="email")
        await InviteService._email(ctx, session, invite, url)
        return EmailSentOut(invite_id=invite.id)

    @staticmethod
    async def extend(
        ctx: TenantContext, session: AsyncSession, invite_id: UUID, body: ExtendIn
    ) -> InviteDetail:
        invite = await _invite(session, ctx.org_id, invite_id, lock=True)
        if invite.status not in ("invited", "expired", "started"):
            raise AppError("CONFLICT", details={"reason": "invite_not_extendable", "status": invite.status})
        new = body.expires_at if body.expires_at.tzinfo else body.expires_at.replace(tzinfo=UTC)
        if new <= _now() or new > _now() + MAX_EXPIRY:
            raise AppError(
                "VALIDATION_ERROR", 422, {"fields": [{"field": "expires_at", "code": "out_of_range"}]}
            )
        before = {"expires_at": invite.expires_at, "status": invite.status}
        invite.expires_at = new
        if invite.status == "expired":
            invite.status = "invited"
        await session.flush()
        await audit.record(
            ctx, session, audit.INVITE_EXTENDED, "invite", invite.id, before=before,
            after={"expires_at": new, "status": invite.status}, reason=body.reason,
        )  # fmt: skip
        return detail(invite, await repo.attempt_for(session, ctx.org_id, invite.id))

    @staticmethod
    async def revoke(ctx: TenantContext, session: AsyncSession, invite_id: UUID) -> InviteDetail:
        invite = await _invite(session, ctx.org_id, invite_id, lock=True)
        if invite.status != "invited":
            raise AppError("CONFLICT", details={"reason": "invite_not_unstarted", "status": invite.status})
        invite.status = "revoked"
        await session.flush()
        await audit.record(
            ctx, session, audit.INVITE_REVOKED, "invite", invite.id,
            before={"status": "invited"}, after={"status": "revoked"},
        )  # fmt: skip
        return detail(invite, None)

    @staticmethod
    async def revoke_unstarted(ctx: TenantContext, session: AsyncSession, role_id: UUID) -> int:
        """Role-close hook: revoke every unstarted invite of the role. In-progress attempts finish."""
        ids = await repo.revoke_unstarted(session, ctx.org_id, role_id)
        for invite_id in ids:
            await audit.record(
                ctx, session, audit.INVITE_REVOKED, "invite", invite_id,
                before={"status": "invited"}, after={"status": "revoked"}, reason="role_closed",
            )  # fmt: skip
        return len(ids)

    @staticmethod
    async def reset_binding(
        ctx: TenantContext, session: AsyncSession, invite_id: UUID, reason: str
    ) -> InviteDetail:
        invite = await _invite(session, ctx.org_id, invite_id, lock=True)
        if invite.status not in ("invited", "started"):
            raise AppError("CONFLICT", details={"reason": "invite_closed", "status": invite.status})
        invite.session_binding_hash = None
        await session.flush()
        await audit.record(ctx, session, audit.INVITE_BINDING_RESET, "invite", invite.id, reason=reason)
        return detail(invite, await repo.attempt_for(session, ctx.org_id, invite.id))

    @staticmethod
    async def set_accommodations(
        ctx: TenantContext, session: AsyncSession, invite_id: UUID, body: AccommodationsIn
    ) -> InviteDetail:
        invite = await _invite(session, ctx.org_id, invite_id, lock=True)
        if invite.status in ("submitted", "revoked"):
            raise AppError("CONFLICT", details={"reason": "invite_closed", "status": invite.status})
        changes = _apply_accommodations(invite, body, ctx.user_id)
        await session.flush()
        await audit.record(
            ctx, session, audit.ACCOMMODATION_SET, "invite", invite.id, after=changes, reason=body.reason
        )
        return detail(invite, await repo.attempt_for(session, ctx.org_id, invite.id))


# Close and the revocations commit together: M6 calls this after the role row changes.
async def _on_close(ctx: TenantContext, session: AsyncSession, role_id: UUID) -> None:
    await InviteService.revoke_unstarted(ctx, session, role_id)


if _on_close not in on_role_close:
    on_role_close.append(_on_close)


class HelpRequestService:
    @staticmethod
    async def list(
        ctx: TenantContext, session: AsyncSession, params: PageParams, *, status: str | None
    ) -> tuple[list[HelpRequestOut], int]:
        owner = ctx.user_id if ctx.role == "hr_user" else None
        rows, total = await repo.list_help_requests(
            session, ctx.org_id, owner_id=owner, status=status, offset=params.offset, limit=params.limit
        )
        return [HelpRequestOut(**r) for r in rows], total

    @staticmethod
    async def _decide(ctx: TenantContext, session: AsyncSession, hr_id: UUID) -> HelpRequest:
        hr = await repo.get_help_request(session, ctx.org_id, hr_id, lock=True)
        if hr is None:
            raise AppError("NOT_FOUND")
        if hr.status != "open":
            raise AppError("CONFLICT", details={"reason": "already_decided", "status": hr.status})
        hr.decided_by, hr.decided_at = ctx.user_id, _now()
        return hr

    @staticmethod
    async def approve(
        ctx: TenantContext, session: AsyncSession, hr_id: UUID, body: ApproveIn
    ) -> HelpRequestOut:
        hr = await HelpRequestService._decide(ctx, session, hr_id)
        invite = await _invite(session, ctx.org_id, hr.invite_id, lock=True)
        hr.status, hr.decision_note = "approved", body.note
        hr.granted_camera_exempt, hr.granted_extra_minutes = body.camera_exempt, body.extra_minutes
        if body.camera_exempt:
            invite.camera_exempt = True
        invite.extra_minutes += body.extra_minutes
        await session.flush()
        await audit.record(
            ctx, session, audit.HELP_REQUEST_APPROVED, "help_request", hr.id,
            after={"camera_exempt": body.camera_exempt, "extra_minutes": body.extra_minutes,
                   "invite_id": invite.id}, reason=body.note,
        )  # fmt: skip
        return HelpRequestOut(**await repo.help_request_row(session, ctx.org_id, hr.id))

    @staticmethod
    async def decline(
        ctx: TenantContext, session: AsyncSession, hr_id: UUID, body: DeclineIn
    ) -> HelpRequestOut:
        hr = await HelpRequestService._decide(ctx, session, hr_id)
        hr.status, hr.decision_note = "declined", body.note
        await session.flush()
        await audit.record(
            ctx, session, audit.HELP_REQUEST_DECLINED, "help_request", hr.id,
            after={"invite_id": hr.invite_id}, reason=body.note,
        )  # fmt: skip
        return HelpRequestOut(**await repo.help_request_row(session, ctx.org_id, hr.id))
