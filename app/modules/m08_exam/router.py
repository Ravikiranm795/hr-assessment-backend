"""M8 routes under ``/api/v1/exam`` (spec 6/M8). Candidate session auth (audience ``exam``) plus the
browser-binding cookie; only ``session/open`` is public."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import TenantContext, tenant_session
from app.core.deps import ExamContext, public_ctx, public_db_session
from app.core.errors import ERROR_CODES, AppError
from app.core.ratelimit import rate_limit
from app.modules.m08_exam.schemas import (
    AnswerIn,
    AnswerOut,
    ConsentIn,
    EventsIn,
    EventsOut,
    HeartbeatOut,
    HelpCurrentOut,
    HelpIn,
    HelpOut,
    OpenIn,
    QuestionsOut,
    SessionOut,
    SnapshotOut,
    StateOut,
    SubmitOut,
    SystemCheckIn,
    SystemCheckOut,
    TimingOut,
)
from app.modules.m08_exam.service import ExamService
from app.modules.m08_exam.session import BINDING_COOKIE, cookie_options, exam_session

router = APIRouter(prefix="/api/v1/exam", tags=["exam"])
Exam = Annotated[ExamContext, Depends(exam_session)]


async def exam_db(exam: Exam):
    """The candidate's transaction (org-scoped, role ``candidate``)."""
    async with tenant_session(exam.ctx) as session:
        yield session


Db = Annotated[AsyncSession, Depends(exam_db)]


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


AUTH = ("UNAUTHENTICATED", "LINK_BOUND_TO_OTHER_SESSION")


@router.post(
    "/session/open",
    response_model=SessionOut,
    summary="Open an exam session",
    description="Exchanges the invite token for an exam session token (audience `exam`, at most 12 hours) "
    "and binds the invite to this browser with an HttpOnly cookie. Another browser gets "
    "`LINK_BOUND_TO_OTHER_SESSION` until HR resets the binding. Reopening in the same browser resumes.",
    dependencies=[Depends(rate_limit(lambda: settings.rate_limit_exam_link, scope="ip", name="exam_link"))],
    responses=_errors("NOT_FOUND", "INVITE_EXPIRED", "INVITE_REVOKED", "LINK_BOUND_TO_OTHER_SESSION",
                      "ORG_DISABLED", "ROLE_NOT_OPEN", "EXAM_ALREADY_SUBMITTED", "RATE_LIMITED"),
)  # fmt: skip
async def open_session(
    body: OpenIn,
    request: Request,
    response: Response,
    ctx: Annotated[TenantContext, Depends(public_ctx)],
    session: Annotated[AsyncSession, Depends(public_db_session)],
):
    out, new_cookie = await ExamService.open(ctx, session, body.token, request.cookies.get(BINDING_COOKIE))
    if new_cookie:
        response.set_cookie(value=new_cookie, **cookie_options())
    return out


@router.get(
    "/session/state",
    response_model=StateOut,
    summary="Exam state",
    description="Status, instructions, branding, watermark text (null when the flag is off), the published "
    "consent document, camera requirement, accommodation badges and server-computed remaining time.",
    responses=_errors(*AUTH),
)
async def state(exam: Exam, session: Db):
    return await ExamService.state(session, exam)


@router.post(
    "/session/system-check",
    response_model=SystemCheckOut,
    summary="Report the device",
    description="Viewport and camera permission. The server checks the user agent itself: phones and tablets "
    "and viewports under 900 px are `DEVICE_NOT_SUPPORTED`; a missing camera permission is "
    "`CAMERA_REQUIRED`.",
    responses=_errors(*AUTH, "DEVICE_NOT_SUPPORTED", "CAMERA_REQUIRED", "EXAM_ALREADY_SUBMITTED"),
)
async def system_check(body: SystemCheckIn, exam: Exam, session: Db):
    return await ExamService.system_check(session, exam, body)


@router.post(
    "/session/consent",
    response_model=TimingOut,
    summary="Record consent",
    description="Records the time and the consent document version (must be the current published one).",
    responses=_errors(*AUTH, "VALIDATION_ERROR", "EXAM_ALREADY_SUBMITTED"),
)
async def consent(body: ConsentIn, exam: Exam, session: Db):
    return await ExamService.consent(session, exam, body)


@router.post(
    "/session/baseline-photo",
    response_model=TimingOut,
    summary="Upload the baseline photo",
    description="Multipart field `file` (PNG, JPEG or WebP). Stored encrypted.",
    responses=_errors(*AUTH, "UPLOAD_INVALID", "EXAM_ALREADY_SUBMITTED"),
)
async def baseline_photo(exam: Exam, session: Db, file: Annotated[UploadFile | None, File()] = None):
    return await ExamService.baseline_photo(session, exam, file)


@router.post(
    "/session/start",
    response_model=TimingOut,
    summary="Start the exam",
    description="Needs consent, a passed device check, the camera photo when required and no open help "
    "request. Sets `started_at` and `deadline_at = start + duration x multiplier + extra minutes`. "
    "Idempotent: a repeat call does not restart the clock.",
    responses=_errors(*AUTH, "CONSENT_REQUIRED", "CAMERA_REQUIRED", "HELP_REQUEST_PENDING",
                      "DEVICE_NOT_SUPPORTED", "INVITE_EXPIRED", "EXAM_ALREADY_SUBMITTED"),
)  # fmt: skip
async def start(exam: Exam, session: Db):
    return await ExamService.start(session, exam)


@router.get(
    "/questions",
    response_model=QuestionsOut,
    summary="Questions",
    description="Candidate-safe questions (no keys, model answers or scoring modes), the saved answers and "
    "remaining seconds. Only while the exam is running.",
    responses=_errors(*AUTH, "EXAM_NOT_STARTED", "EXAM_ALREADY_SUBMITTED"),
)
async def questions(exam: Exam, session: Db):
    return await ExamService.questions(session, exam)


@router.put(
    "/answers/{question_id}",
    response_model=AnswerOut,
    summary="Save an answer",
    description="Idempotent, last write wins. Shapes: `{option_id}`, `{option_ids}`, `{blanks}`, `{gaps}`, "
    "`{text}`. Accepted until the deadline plus 5 seconds (`EXAM_TIME_OVER` after).",
    responses=_errors(*AUTH, "VALIDATION_ERROR", "NOT_FOUND", "EXAM_TIME_OVER", "EXAM_NOT_STARTED",
                      "EXAM_ALREADY_SUBMITTED"),
)  # fmt: skip
async def save_answer(question_id: UUID, body: AnswerIn, exam: Exam, session: Db):
    return await ExamService.save_answer(session, exam, question_id, body)


@router.post(
    "/heartbeat",
    response_model=HeartbeatOut,
    summary="Heartbeat",
    description="Server-computed remaining seconds. The clock never pauses on a disconnect.",
    responses=_errors(*AUTH),
)
async def heartbeat(exam: Exam, session: Db):
    return await ExamService.heartbeat(session, exam)


@router.post(
    "/events",
    response_model=EventsOut,
    summary="Proctoring events",
    description="Up to 50 per call. Severity is assigned by the server; events never stop the exam.",
    responses=_errors(*AUTH, "RATE_LIMITED", "EXAM_NOT_STARTED", "EXAM_ALREADY_SUBMITTED"),
)
async def events(body: EventsIn, exam: Exam, session: Db):
    return await ExamService.ingest_events(session, exam, body)


@router.post(
    "/snapshots",
    response_model=SnapshotOut,
    status_code=201,
    summary="Upload a snapshot",
    description="Multipart `file` plus optional `event_id` to attach it to an event; without one it is a "
    "periodic capture. Rate limited per attempt. Stored encrypted.",
    responses=_errors(*AUTH, "UPLOAD_INVALID", "RATE_LIMITED", "NOT_FOUND", "EXAM_NOT_STARTED"),
)
async def snapshots(
    exam: Exam,
    session: Db,
    file: Annotated[UploadFile | None, File()] = None,
    event_id: Annotated[UUID | None, Form()] = None,
):
    if file is not None and file.size is not None and file.size > settings.max_snapshot_kb * 1024:
        raise AppError("UPLOAD_INVALID", 422, {"reason": "too_large"})  # MAX_SNAPSHOT_KB (spec: input limit)
    return SnapshotOut(event_id=await ExamService.snapshot(session, exam, file, event_id))


@router.post(
    "/help-requests",
    response_model=HelpOut,
    status_code=201,
    summary="Ask for help",
    description="One open request per invite (`HELP_REQUEST_PENDING`). Starting is blocked while it is open.",
    responses=_errors(*AUTH, "HELP_REQUEST_PENDING", "EXAM_ALREADY_SUBMITTED"),
)
async def create_help_request(body: HelpIn, exam: Exam, session: Db):
    return await ExamService.create_help_request(session, exam, body)


@router.get(
    "/help-requests/current",
    response_model=HelpCurrentOut,
    summary="Latest help request",
    description="Poll the status of the latest request (`request` is null when none).",
    responses=_errors(*AUTH),
)
async def current_help_request(exam: Exam, session: Db):
    return await ExamService.current_help_request(session, exam)


@router.post(
    "/submit",
    response_model=SubmitOut,
    summary="Submit the exam",
    description="Always accepted, also after the deadline. A repeat call returns the same result (exactly "
    "one submission). The session is then read-only.",
    responses=_errors(*AUTH, "EXAM_NOT_STARTED"),
)
async def submit(exam: Exam, session: Db):
    return await ExamService.submit(session, exam)
