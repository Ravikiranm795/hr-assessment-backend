import re
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
