import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import logconfig, metrics

REQUEST_ID_HEADER = "X-Request-ID"
# Used with fullmatch: "$" alone would also accept a trailing newline (header injection).
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def safe_request_id(incoming: str) -> str:
    """Reuse the client's id only if it is pure ASCII and fully matches the safe pattern."""
    if incoming.isascii() and _SAFE_ID.fullmatch(incoming):
        return incoming
    return str(uuid.uuid4())


class RequestIDMiddleware:
    """Reads or generates a request id, exposes it as ``request.state.request_id``,
    and sets ``X-Request-ID`` on every response (including errors)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        incoming = MutableHeaders(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = safe_request_id(incoming)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_id)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Browsers ignore HSTS on plain http, so it is safe to send everywhere (spec 10: HTTPS only).
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class SecurityHeadersMiddleware:
    """Adds the spec 10 transport security headers to every HTTP response (errors included)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


access_log = logging.getLogger("app.access")


class AccessLogMiddleware:
    """One structured log line and the HTTP counters per request: request id, org id, user id, route
    template, status and latency. Must sit inside ``RequestIDMiddleware`` (it reads the request id)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        logconfig.start_request(state.get("request_id", "-"))
        status = 500  # stays 500 when the app raises before sending a response
        started = time.perf_counter()

        async def send_capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capture)
        finally:
            elapsed = time.perf_counter() - started
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope["method"]
            ctx = state.get("ctx")
            logconfig.bind(org_id=getattr(ctx, "org_id", None), user_id=getattr(ctx, "user_id", None))
            metrics.inc("http_requests_total", method=method, route=route, status=status)
            metrics.inc("http_request_duration_seconds_sum", elapsed, method=method, route=route)
            metrics.inc("http_request_duration_seconds_count", method=method, route=route)
            quiet = route in ("/health", "/health/ready")
            access_log.log(
                logging.DEBUG if quiet else logging.INFO,
                "request",
                extra={
                    "method": method,
                    "route": route,
                    "status": status,
                    "latency_ms": round(elapsed * 1000, 1),
                },
            )
