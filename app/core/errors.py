"""Error codes and the response envelope (spec 4.6, Appendix A).

Every error response is ``{"error": {"code", "message_key", "details", "request_id"}}``.
Raise ``AppError("CODE")`` anywhere; the handlers installed by ``install_error_handlers``
turn it, request validation errors, HTTP exceptions and unhandled exceptions into the envelope.
"""

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.i18n_codes import (
    ERROR_CATALOG,
    INTERNAL_ERROR_CODE,
    INTERNAL_ERROR_MESSAGE_KEY,
    VALIDATION_FIELD_CODES,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    message_key: str
    status: int


ERROR_CODES: dict[str, ErrorSpec] = {
    code: ErrorSpec(code, key, status) for code, (key, status) in ERROR_CATALOG.items()
}
VALIDATION_ERROR = "VALIDATION_ERROR"

# HTTPException status -> code, for framework-raised errors (404 route, 405 method, ...).
_STATUS_TO_CODE = {
    400: VALIDATION_ERROR,
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "NOT_FOUND",
    409: "CONFLICT",
    413: "UPLOAD_INVALID",
    415: "UPLOAD_INVALID",
    422: VALIDATION_ERROR,
    429: "RATE_LIMITED",
}


class AppError(Exception):
    """A business error. ``status`` defaults to the catalog status for ``code``."""

    def __init__(self, code: str, status: int | None = None, details: dict[str, Any] | None = None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}; add it to spec Appendix A first")
        self.code = code
        self.status = status if status is not None else ERROR_CODES[code].status
        self.details = details or {}
        super().__init__(code)

    @property
    def message_key(self) -> str:
        return ERROR_CODES[self.code].message_key


def validation_error(fields: list[dict[str, str]]) -> AppError:
    """Build a ``VALIDATION_ERROR`` from ``[{"field": ..., "code": ...}]`` (service-side checks)."""
    return AppError(VALIDATION_ERROR, 422, {"fields": fields})


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def envelope(code: str, message_key: str, details: dict | None, request_id: str | None) -> dict:
    return {
        "error": {
            "code": code,
            "message_key": message_key,
            "details": details or {},
            "request_id": request_id,
        }
    }


def error_response(request: Request, exc: AppError) -> JSONResponse:
    headers = {}
    if exc.code == "RATE_LIMITED" and "retry_after_sec" in exc.details:
        headers["Retry-After"] = str(exc.details["retry_after_sec"])
    return JSONResponse(
        envelope(exc.code, exc.message_key, exc.details, _request_id(request)),
        status_code=exc.status,
        headers=headers,
    )


_LOCATION_ROOTS = {"body", "query", "path", "header", "cookie"}


def _field_errors(errors: list[dict]) -> list[dict[str, str]]:
    fields = []
    for err in errors:
        loc = [str(part) for part in err.get("loc", ())]
        if loc and loc[0] in _LOCATION_ROOTS:
            loc = loc[1:]
        fields.append(
            {
                "field": ".".join(loc) or "body",
                "code": VALIDATION_FIELD_CODES.get(err.get("type", ""), err.get("type", "invalid")),
            }
        )
    return fields


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return error_response(request, exc)


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(request, validation_error(_field_errors(list(exc.errors()))))


async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _STATUS_TO_CODE.get(exc.status_code)
    if code is None:
        code = "FORBIDDEN" if 400 <= exc.status_code < 500 else None
    if code is None:
        return _internal(request, exc.status_code)
    return error_response(request, AppError(code, exc.status_code))


def _internal(request: Request, status: int = 500) -> JSONResponse:
    request_id = _request_id(request)
    # Unhandled exceptions are answered by Starlette's outermost ServerErrorMiddleware, which
    # sits outside RequestIDMiddleware, so the header is set here explicitly.
    headers = {"X-Request-ID": request_id} if request_id else {}
    return JSONResponse(
        envelope(INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE_KEY, None, request_id),
        status_code=status,
        headers=headers,
    )


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error", extra={"request_id": _request_id(request)})
    return _internal(request)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_handler)
