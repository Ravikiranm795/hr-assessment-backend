"""Structured JSON logging (spec 4.12).

One JSON object per line on stderr: ``ts``, ``level``, ``logger``, ``msg`` plus the request context
(``request_id``, ``org_id``, ``user_id``) and any ``extra=`` fields. The request log line (logger
``app.access``) adds ``method``, ``route`` (the route template, never the raw path or query string, which
can carry tokens), ``status`` and ``latency_ms``. Bodies, headers, secrets and candidate answers are never
logged."""

import contextvars
import json
import logging
from datetime import UTC, datetime
from typing import Any

# One dict per request, created by the access-log middleware and filled by the auth dependencies.
_log_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("log_ctx", default=None)

_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)))
_STANDARD |= {"message", "asctime", "taskName", "color_message"}
_HANDLER_MARK = "_app_json_handler"


def start_request(request_id: str) -> dict[str, Any]:
    ctx: dict[str, Any] = {"request_id": request_id}
    _log_ctx.set(ctx)
    return ctx


def bind(**fields: Any) -> None:
    """Add fields (``org_id``, ``user_id``) to every log line of the current request."""
    ctx = _log_ctx.get()
    if ctx is not None:
        ctx.update({k: str(v) for k, v in fields.items() if v is not None})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data.update(_log_ctx.get() or {})
        data.update(
            {k: v for k, v in record.__dict__.items() if k not in _STANDARD and not k.startswith("_")}
        )
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Idempotent: replaces only the handler a previous call installed."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn: its error log goes through our handler; its access log is replaced by ``app.access``.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
