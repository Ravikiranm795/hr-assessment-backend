"""M8 rules: session, checks, timer, answers, proctoring events, help requests, submit."""

import json
import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, flags, notifications, security
from app.core.branding_text import watermark
from app.core.config import settings
from app.core.db import TenantContext
from app.core.deps import ExamContext
from app.core.errors import AppError, validation_error
from app.core.ratelimit import check_rate_limit
from app.core.storage import get_storage
from app.db import Asset, Attempt, HelpRequest, Invite, ProctorEvent
from app.modules.m04_org_settings.service import BrandingService, DocumentService
from app.modules.m04_org_settings.uploads import inspect_image, read_limited
from app.modules.m05_assessments.service import PublishedVersion, candidate_questions, get_published_version
from app.modules.m08_exam import hooks, repo
from app.modules.m08_exam.device import check_device
from app.modules.m08_exam.schemas import (
    AnswerIn,
    AnswerOut,
    ConsentDocOut,
    ConsentIn,
    EventsIn,
    EventsOut,
    HeartbeatOut,
    HelpCurrentOut,
    HelpIn,
    HelpOut,
    QuestionsOut,
    SessionOut,
    StateOut,
    SubmitOut,
    SystemCheckIn,
    SystemCheckOut,
    TimingOut,
)
from app.modules.m08_exam.session import new_binding
from app.modules.m08_exam.severity_rules import EVENT_TYPES, TAB_TYPES, severity_for

SAVE_GRACE = timedelta(seconds=5)
MAX_ANSWER_CHARS = 20_000
MAX_SLOT_CHARS = 2_000
MAX_META_BYTES = 2_048
LIVE = ("opened", "consented", "ready")


def _now() -> datetime:
    return datetime.now(UTC)


def attempt_remaining_seconds(attempt: Attempt, now: datetime | None = None) -> int | None:
    """Server-computed seconds left; ``None`` before start, ``0`` once the deadline has passed."""
    if attempt.deadline_at is None:
        return None
    return max(0, math.ceil((attempt.deadline_at - (now or _now())).total_seconds()))


def _bad(field: str, code: str) -> AppError:
    return validation_error([{"field": field, "code": code}])


async def _load(session: AsyncSession, exam: ExamContext, *, lock: bool = False) -> tuple[Attempt, Invite]:
    attempt = await repo.get_attempt(session, exam.ctx.org_id, exam.attempt_id, lock=lock)
    invite = await repo.get_invite(session, exam.ctx.org_id, exam.invite_id)
    if attempt is None or invite is None:
        raise AppError("UNAUTHENTICATED")
    # the token's binding claim must still match the stored one (reset-binding clears it), and a revoked
    # invite loses access at once
    if invite.session_binding_hash is None or invite.session_binding_hash != exam.binding:
        raise AppError("LINK_BOUND_TO_OTHER_SESSION")
    if invite.status == "revoked":
        raise AppError("INVITE_REVOKED")
    return attempt, invite


def _camera_required_sync(camera_flag: bool, invite: Invite) -> bool:
    return camera_flag and not invite.camera_exempt


async def _camera_required(session: AsyncSession, invite: Invite) -> bool:
    return _camera_required_sync(
        await flags.is_enabled(session, invite.org_id, flags.CAMERA_PROCTORING), invite
    )


def _not_submitted(attempt: Attempt) -> None:
    if attempt.status == "submitted":
        raise AppError("EXAM_ALREADY_SUBMITTED")


class ExamService:
    # ------------------------------------------------------------------ open

    @staticmethod
    async def open(
        ctx: TenantContext, session: AsyncSession, token: str, binding_cookie: str | None
    ) -> tuple[SessionOut, str | None]:
        """Exchange an invite token for an exam session. ``(session, new cookie value or None)``."""
        invite = await repo.invite_by_token_hash(session, security.hmac_hash(token))
        if invite is None:
            raise AppError("NOT_FOUND")
        org_id = invite.org_id
        if await repo.org_status(session, org_id) != "active":
            raise AppError("ORG_DISABLED")
        if invite.status == "revoked":
            raise AppError("INVITE_REVOKED")
        attempt = await repo.attempt_for_invite(session, org_id, invite.id)
        if invite.status == "submitted" or (attempt and attempt.status == "submitted"):
            raise AppError("EXAM_ALREADY_SUBMITTED")
        if invite.status == "expired" or (invite.status == "invited" and invite.expires_at <= _now()):
            raise AppError("INVITE_EXPIRED")
        if invite.status == "invited":
            role = await repo.role_info(session, org_id, invite.role_id)
            if role is None or role["status"] != "open":
                raise AppError("ROLE_NOT_OPEN")
            if invite.assessment_version_id is None:
                raise AppError("ROLE_HAS_NO_ASSESSMENT")

        new_cookie: str | None = None
        if invite.session_binding_hash is None:
            new_cookie, invite.session_binding_hash = new_binding()
            binding = invite.session_binding_hash
        else:
            if not binding_cookie or security.hmac_hash(binding_cookie) != invite.session_binding_hash:
                raise AppError("LINK_BOUND_TO_OTHER_SESSION")
            binding = invite.session_binding_hash
        if attempt is None:
            attempt = Attempt(
                org_id=org_id, invite_id=invite.id, assessment_version_id=invite.assessment_version_id,
                status="opened", ip=ctx.ip, user_agent=ctx.user_agent,
            )  # fmt: skip
            session.add(attempt)
        await session.flush()
        token_out = security.create_exam_token(
            attempt_id=attempt.id, invite_id=invite.id, org_id=org_id, binding=binding
        )
        return (
            SessionOut(
                session_token=token_out, attempt_status=attempt.status,
                expires_in_sec=security.EXAM_TOKEN_MAX_HOURS * 3600,
            ),
            new_cookie,
        )  # fmt: skip

    # ------------------------------------------------------------------ state

    @staticmethod
    async def _version(session: AsyncSession, attempt: Attempt) -> PublishedVersion:
        return await get_published_version(session, attempt.org_id, attempt.assessment_version_id)

    @staticmethod
    async def state(session: AsyncSession, exam: ExamContext) -> StateOut:
        attempt, invite = await _load(session, exam)
        org_id = attempt.org_id
        version = await ExamService._version(session, attempt)
        role = await repo.role_info(session, org_id, invite.role_id)
        consent = await DocumentService(session).published_view(org_id, "consent")
        branding = await BrandingService(session).get(exam.ctx)
        show_watermark = await flags.is_enabled(session, org_id, flags.WATERMARK)
        pending = await repo.open_help_request(session, org_id, invite.id)
        return StateOut(
            status=attempt.status, role_title=role["title"] if role else None,
            duration_minutes=version.duration_minutes, instructions_html=version.instructions_html,
            header=version.header_config, footer_html=version.footer_html,
            watermark=watermark() if show_watermark else None, branding=branding.model_dump(mode="json"),
            consent_document=ConsentDocOut(
                document_version_id=consent.id, content_html=consent.content_html, pdf_url=consent.pdf_url
            ) if consent else None,
            consented=attempt.consent_at is not None, camera_required=await _camera_required(session, invite),
            baseline_photo_uploaded=attempt.baseline_asset_id is not None,
            help_request_pending=pending is not None,
            accommodations={
                "camera_exempt": invite.camera_exempt, "time_multiplier": float(invite.time_multiplier),
                "extra_minutes": invite.extra_minutes,
            },
            snapshot_interval_seconds=int(version.proctoring_config.get("snapshot_interval_seconds", 60)),
            started_at=attempt.started_at, deadline_at=attempt.deadline_at,
            remaining_seconds=attempt_remaining_seconds(attempt), question_count=len(version.questions),
        )  # fmt: skip

    # ------------------------------------------------------------------ checks, consent, photo

    @staticmethod
    async def _advance(session: AsyncSession, attempt: Attempt, invite: Invite) -> None:
        if (
            attempt.status in ("opened", "consented")
            and attempt.consent_at is not None
            and attempt.viewport_w is not None
            and (attempt.baseline_asset_id is not None or not await _camera_required(session, invite))
        ):
            attempt.status = "ready"
        elif attempt.status == "opened" and attempt.consent_at is not None:
            attempt.status = "consented"

    @staticmethod
    async def system_check(session: AsyncSession, exam: ExamContext, body: SystemCheckIn) -> SystemCheckOut:
        attempt, invite = await _load(session, exam, lock=True)
        _not_submitted(attempt)
        if attempt.status == "in_progress":
            raise AppError("CONFLICT", details={"reason": "already_started"})
        check_device(exam.ctx.user_agent, body.viewport_w)
        required = await _camera_required(session, invite)
        if required and not body.camera_permission:
            raise AppError("CAMERA_REQUIRED", details={"reason": "permission"})
        attempt.viewport_w, attempt.viewport_h = body.viewport_w, body.viewport_h
        attempt.user_agent = exam.ctx.user_agent
        await ExamService._advance(session, attempt, invite)
        await session.flush()
        return SystemCheckOut(device_ok=True, camera_required=required, status=attempt.status)

    @staticmethod
    async def consent(session: AsyncSession, exam: ExamContext, body: ConsentIn) -> TimingOut:
        attempt, invite = await _load(session, exam, lock=True)
        _not_submitted(attempt)
        document = await DocumentService(session).published_view(attempt.org_id, "consent")
        if document is not None and body.document_version_id != document.id:
            raise _bad("document_version_id", "not_current_consent_version")
        attempt.consent_at = _now()
        attempt.consent_document_version_id = document.id if document else None
        if attempt.status == "opened":
            attempt.status = "consented"
        await ExamService._advance(session, attempt, invite)
        await session.flush()
        return ExamService._timing(attempt)

    @staticmethod
    async def baseline_photo(
        session: AsyncSession, exam: ExamContext, upload: UploadFile | None
    ) -> TimingOut:
        attempt, invite = await _load(session, exam, lock=True)
        _not_submitted(attempt)
        data, declared = await read_limited(upload)
        info = inspect_image(data, declared=declared)
        storage = get_storage()
        stored = await storage.put(
            f"{attempt.org_id}/baseline/{uuid4()}.{info.extension}", data, info.content_type, encrypt=True
        )
        try:
            asset = await repo.add_asset(session, _asset(attempt.org_id, "baseline", stored))
            attempt.baseline_asset_id = asset.id
            await ExamService._advance(session, attempt, invite)
            await session.flush()
        except Exception:
            await storage.delete(stored.key)
            raise
        return ExamService._timing(attempt)

    @staticmethod
    def _timing(attempt: Attempt) -> TimingOut:
        return TimingOut(
            status=attempt.status, started_at=attempt.started_at, deadline_at=attempt.deadline_at,
            remaining_seconds=attempt_remaining_seconds(attempt),
        )  # fmt: skip

    # ------------------------------------------------------------------ start

    @staticmethod
    async def start(session: AsyncSession, exam: ExamContext) -> TimingOut:
        """Flags are read here, once (not continuously during the exam)."""
        attempt, invite = await _load(session, exam, lock=True)
        _not_submitted(attempt)
        if attempt.status == "in_progress":
            return ExamService._timing(attempt)  # idempotent: a refresh must not restart the clock
        if invite.status == "invited" and invite.expires_at <= _now():
            raise AppError("INVITE_EXPIRED")
        if attempt.consent_at is None:
            raise AppError("CONSENT_REQUIRED")
        if attempt.viewport_w is None:
            raise AppError("DEVICE_NOT_SUPPORTED", details={"reason": "system_check_missing"})
        if await _camera_required(session, invite) and attempt.baseline_asset_id is None:
            raise AppError("CAMERA_REQUIRED", details={"reason": "baseline_photo"})
        if await repo.open_help_request(session, attempt.org_id, invite.id):
            raise AppError("HELP_REQUEST_PENDING")
        version = await ExamService._version(session, attempt)
        started = _now()
        minutes = version.duration_minutes * float(invite.time_multiplier) + invite.extra_minutes
        attempt.started_at, attempt.deadline_at = started, started + timedelta(minutes=minutes)
        attempt.status, invite.status = "in_progress", "started"
        await session.flush()
        await audit.record(
            exam.ctx, session, audit.EXAM_STARTED, "attempt", attempt.id,
            after={"invite_id": invite.id, "deadline_at": attempt.deadline_at, "version_id": version.id},
        )  # fmt: skip
        return ExamService._timing(attempt)

    # ------------------------------------------------------------------ questions and answers

    @staticmethod
    def _live(attempt: Attempt) -> None:
        _not_submitted(attempt)
        if attempt.status != "in_progress":
            raise AppError("EXAM_NOT_STARTED")

    @staticmethod
    async def questions(session: AsyncSession, exam: ExamContext) -> QuestionsOut:
        attempt, _ = await _load(session, exam)
        ExamService._live(attempt)
        version = await ExamService._version(session, attempt)
        items = candidate_questions(version.questions)
        rng = random.Random(str(attempt.id))  # stable across refreshes
        if version.shuffle_options:
            for q in items:
                rng.shuffle(q.options)
        if version.shuffle_questions:
            rng.shuffle(items)
        return QuestionsOut(
            questions=items, answers=await repo.answers_map(session, attempt.id),
            remaining_seconds=attempt_remaining_seconds(attempt) or 0,
        )  # fmt: skip

    @staticmethod
    def _validate_answer(question: Any, answer: Any) -> Any:
        """RF-3: empty, wrong-shape and oversized payloads are 422 with a code, never a 500."""
        if not isinstance(answer, dict) or not answer:
            raise _bad("answer", "empty_or_wrong_shape")
        if len(json.dumps(answer, default=str)) > MAX_ANSWER_CHARS * 2:
            raise _bad("answer", "too_large")
        qtype = question.type
        known = {str(o.id) for o in question.options}
        if qtype == "single_choice":
            value = answer.get("option_id")
            if not isinstance(value, str) or value not in known:
                raise _bad("answer.option_id", "unknown_option")
            return {"option_id": value}
        if qtype == "multi_choice":
            values = answer.get("option_ids")
            if not isinstance(values, list) or not values or len(set(values)) != len(values):
                raise _bad("answer.option_ids", "empty_or_wrong_shape")
            if not all(isinstance(v, str) and v in known for v in values):
                raise _bad("answer.option_ids", "unknown_option")
            return {"option_ids": values}
        if qtype in ("fill_blank", "code_gap"):
            name = "blanks" if qtype == "fill_blank" else "gaps"
            values = answer.get(name)
            slots = len((question.answer_key or {}).get(name, []))
            if not isinstance(values, list) or not values or len(values) > max(slots, 1):
                raise _bad(f"answer.{name}", "wrong_length")
            if not all(isinstance(v, str) and len(v) <= MAX_SLOT_CHARS for v in values):
                raise _bad(f"answer.{name}", "wrong_type_or_too_long")
            if not any(v.strip() for v in values):
                raise _bad(f"answer.{name}", "empty")
            return {name: values}
        text = answer.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _bad("answer.text", "empty")
        if len(text) > MAX_ANSWER_CHARS:
            raise _bad("answer.text", "too_large")
        return {"text": text}

    @staticmethod
    async def save_answer(
        session: AsyncSession, exam: ExamContext, question_id: UUID, body: AnswerIn
    ) -> AnswerOut:
        """Idempotent, last write wins. Accepted until the deadline plus a 5 s grace (RF-2)."""
        attempt, _ = await _load(session, exam, lock=True)  # serialized against finalize
        ExamService._live(attempt)
        now = _now()
        if attempt.deadline_at is not None and now > attempt.deadline_at + SAVE_GRACE:
            raise AppError("EXAM_TIME_OVER")
        version = await ExamService._version(session, attempt)
        question = next((q for q in version.questions if q.id == question_id), None)
        if question is None:  # a question of another version or organization
            raise AppError("NOT_FOUND")
        await repo.upsert_answer(
            session, attempt.id, question_id, ExamService._validate_answer(question, body.answer)
        )
        return AnswerOut(remaining_seconds=attempt_remaining_seconds(attempt, now) or 0)

    @staticmethod
    async def heartbeat(session: AsyncSession, exam: ExamContext) -> HeartbeatOut:
        """Never pauses the clock; the client only displays what the server computes."""
        attempt, _ = await _load(session, exam)
        remaining = attempt_remaining_seconds(attempt)
        return HeartbeatOut(
            status=attempt.status, remaining_seconds=remaining or 0,
            time_over=attempt.status == "in_progress" and remaining == 0,
        )  # fmt: skip

    # ------------------------------------------------------------------ proctoring

    @staticmethod
    async def ingest_events(session: AsyncSession, exam: ExamContext, body: EventsIn) -> EventsOut:
        """Never stops or rejects the exam. Unknown types are ignored; severity is server-assigned."""
        attempt, _ = await _load(session, exam, lock=True)
        ExamService._live(attempt)
        check_rate_limit(settings.rate_limit_exam_events, "exam_events", "attempt", str(attempt.id))
        tab_events = await repo.count_tab_events(session, attempt.id)
        counts = {
            "types": dict(attempt.flag_counts.get("types", {})),
            "severity": dict(attempt.flag_counts.get("severity", {})),
        }
        accepted = 0
        for item in body.events:
            if item.type not in EVENT_TYPES or len(json.dumps(item.meta, default=str)) > MAX_META_BYTES:
                continue
            severity = severity_for(item.type, item.duration_ms, tab_events)
            await repo.add_event(
                session,
                ProctorEvent(
                    attempt_id=attempt.id, org_id=attempt.org_id, type=item.type, severity=severity,
                    occurred_at=item.occurred_at, duration_ms=item.duration_ms, meta=item.meta,
                ),
            )  # fmt: skip
            tab_events += item.type in TAB_TYPES
            counts["types"][item.type] = counts["types"].get(item.type, 0) + 1
            counts["severity"][severity] = counts["severity"].get(severity, 0) + 1
            accepted += 1
        attempt.flag_counts = counts
        await session.flush()
        return EventsOut(accepted=accepted, ignored=len(body.events) - accepted)

    @staticmethod
    async def snapshot(
        session: AsyncSession, exam: ExamContext, upload: UploadFile | None, event_id: UUID | None
    ) -> UUID:
        attempt, _ = await _load(session, exam, lock=True)
        ExamService._live(attempt)
        key = str(attempt.id)
        check_rate_limit(
            f"1/{settings.rate_limit_snapshot_interval_sec} second", "snapshot_gap", "attempt", key
        )
        check_rate_limit(
            f"{settings.rate_limit_snapshot_max_per_attempt}/day", "snapshot_max", "attempt", key
        )
        event = None
        if event_id is not None:
            event = await repo.get_event(session, attempt.org_id, attempt.id, event_id)
            if event is None:
                raise AppError("NOT_FOUND")
            if event.snapshot_asset_id is not None:
                raise AppError("CONFLICT", details={"reason": "snapshot_already_attached"})
        data, declared = await read_limited(upload)
        info = inspect_image(data, declared=declared)
        storage = get_storage()
        stored = await storage.put(
            f"{attempt.org_id}/snapshot/{uuid4()}.{info.extension}", data, info.content_type, encrypt=True
        )
        try:
            asset = await repo.add_asset(session, _asset(attempt.org_id, "snapshot", stored))
            if event is None:  # periodic capture
                event = await repo.add_event(
                    session,
                    ProctorEvent(
                        attempt_id=attempt.id, org_id=attempt.org_id, type="snapshot", severity="info"
                    ),
                )
            event.snapshot_asset_id = asset.id
            await session.flush()
        except Exception:
            await storage.delete(stored.key)
            raise
        return event.id

    # ------------------------------------------------------------------ help requests

    @staticmethod
    def _help_out(row: HelpRequest) -> HelpOut:
        return HelpOut(
            id=row.id, reason=row.reason, note=row.note, status=row.status, created_at=row.created_at,
            decided_at=row.decided_at, granted_camera_exempt=row.granted_camera_exempt,
            granted_extra_minutes=row.granted_extra_minutes,
        )  # fmt: skip

    @staticmethod
    async def create_help_request(session: AsyncSession, exam: ExamContext, body: HelpIn) -> HelpOut:
        attempt, invite = await _load(session, exam)
        _not_submitted(attempt)
        if await repo.open_help_request(session, attempt.org_id, invite.id):
            raise AppError("HELP_REQUEST_PENDING")
        row = HelpRequest(
            org_id=attempt.org_id, invite_id=invite.id, attempt_id=attempt.id, reason=body.reason,
            note=body.note, status="open",
        )  # fmt: skip
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            raise AppError("HELP_REQUEST_PENDING") from None
        role = await repo.role_info(session, attempt.org_id, invite.role_id)
        await audit.record(
            exam.ctx, session, audit.HELP_REQUEST_CREATED, "help_request", row.id,
            after={"invite_id": invite.id, "reason": body.reason},
        )  # fmt: skip
        if role:
            await notifications.notify(
                session, attempt.org_id, role["owner_id"], "help_request_created",
                {"help_request_id": str(row.id), "invite_id": str(invite.id), "reason": body.reason},
                "help_request", row.id,
            )  # fmt: skip
        return ExamService._help_out(row)

    @staticmethod
    async def current_help_request(session: AsyncSession, exam: ExamContext) -> HelpCurrentOut:
        row = await repo.latest_help_request(session, exam.ctx.org_id, exam.invite_id)
        return HelpCurrentOut(request=ExamService._help_out(row) if row else None)

    # ------------------------------------------------------------------ submit

    @staticmethod
    async def finalize(
        ctx: TenantContext, session: AsyncSession, attempt_id: UUID, reason: str
    ) -> tuple[Attempt, bool]:
        """Submit once. ``(attempt, newly_submitted)``. The row lock makes a double submit, or a submit
        racing the timeout sweeper, produce exactly one submission (RF-2)."""
        attempt = await repo.get_attempt(session, ctx.org_id, attempt_id, lock=True)
        if attempt is None:
            raise AppError("NOT_FOUND")
        if attempt.status == "submitted":
            return attempt, False
        if attempt.status != "in_progress":
            raise AppError("EXAM_NOT_STARTED")
        invite = await repo.get_invite(session, ctx.org_id, attempt.invite_id, lock=True)
        attempt.status, attempt.submitted_at, attempt.submit_reason = "submitted", _now(), reason
        if invite is not None:
            invite.status = "submitted"
        await session.flush()
        await audit.record(
            ctx, session, audit.EXAM_SUBMITTED, "attempt", attempt.id,
            after={"reason": reason, "invite_id": attempt.invite_id},
        )  # fmt: skip
        for hook in hooks.on_submit:
            await hook(ctx, session, attempt.id)
        if invite is not None:
            role = await repo.role_info(session, ctx.org_id, invite.role_id)
            if role:
                await notifications.notify(
                    session, ctx.org_id, role["owner_id"], "attempt_submitted",
                    {"attempt_id": str(attempt.id), "role_id": str(invite.role_id)}, "attempt", attempt.id,
                )  # fmt: skip
        return attempt, True

    @staticmethod
    async def submit(session: AsyncSession, exam: ExamContext) -> SubmitOut:
        """Always accepted, including after the deadline. A repeat call returns the same result."""
        attempt, _ = await ExamService.finalize(exam.ctx, session, exam.attempt_id, "candidate")
        return SubmitOut(
            status=attempt.status, submitted_at=attempt.submitted_at, submit_reason=attempt.submit_reason
        )


def _asset(org_id: UUID, kind: str, stored: Any) -> Asset:
    return Asset(
        org_id=org_id, kind=kind, storage_key=stored.key, content_type=stored.content_type,
        size_bytes=stored.size_bytes, sha256=stored.sha256, encrypted=True, created_by=None,
    )  # fmt: skip
