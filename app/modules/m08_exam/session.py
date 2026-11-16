"""Browser binding and exam-session dependency (spec 8.1)."""

from typing import Annotated

from fastapi import Depends, Request

from app.core import security
from app.core.config import settings
from app.core.deps import ExamContext, current_exam_ctx
from app.core.errors import AppError

BINDING_COOKIE = "exam_binding"
COOKIE_PATH = "/api/v1/exam"


def new_binding() -> tuple[str, str]:
    """``(cookie value, hash)``: only the hash is stored and put in the session token."""
    value = security.new_opaque_token()
    return value, security.hmac_hash(value)


def matches(request: Request, stored_hash: str | None) -> bool:
    cookie = request.cookies.get(BINDING_COOKIE)
    return bool(cookie and stored_hash and security.hmac_hash(cookie) == stored_hash)


def cookie_options() -> dict:
    return {
        "key": BINDING_COOKIE, "httponly": True, "samesite": "lax", "secure": settings.app_env == "prod",
        "path": COOKIE_PATH, "max_age": security.EXAM_TOKEN_MAX_HOURS * 3600,
    }  # fmt: skip


async def exam_session(
    request: Request, exam: Annotated[ExamContext, Depends(current_exam_ctx)]
) -> ExamContext:
    """The exam token plus the binding cookie: a forwarded token from another browser is refused."""
    if not matches(request, exam.binding):
        raise AppError("LINK_BOUND_TO_OTHER_SESSION")
    return exam
