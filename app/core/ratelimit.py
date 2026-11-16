"""Rate limiting (spec 4.7) behind a small interface.

The default implementation is in-memory, built on ``limits`` (the engine slowapi uses), so a
Redis-backed limiter can replace it for multi-instance deployments via ``set_rate_limiter``.
Limit strings are slowapi/limits syntax: ``"10/minute"``, ``"1 per 20 seconds"``.
"""

import math
import time
from collections.abc import Callable
from typing import Literal, Protocol, runtime_checkable

from fastapi import Request
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

from app.core.errors import AppError


@runtime_checkable
class RateLimiter(Protocol):
    def hit(self, limit: str, *keys: str) -> bool:
        """Consume one unit for ``keys`` under ``limit``. ``False`` means the limit is exceeded."""
        ...

    def retry_after(self, limit: str, *keys: str) -> int:
        """Seconds until the next unit is available (0 if available now)."""
        ...

    def reset(self) -> None: ...


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._storage = MemoryStorage()
        self._strategy = MovingWindowRateLimiter(self._storage)

    def hit(self, limit: str, *keys: str) -> bool:
        return self._strategy.hit(parse(limit), *keys)

    def retry_after(self, limit: str, *keys: str) -> int:
        stats = self._strategy.get_window_stats(parse(limit), *keys)
        if stats.remaining > 0:
            return 0
        return max(1, math.ceil(stats.reset_time - time.time()))

    def reset(self) -> None:
        self._storage.reset()


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = InMemoryRateLimiter()
    return _limiter


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    """Swap the process-wide limiter (``None`` restores a fresh in-memory one on next use)."""
    global _limiter
    _limiter = limiter


def check_rate_limit(limit: str, *keys: str) -> None:
    """Raise ``RATE_LIMITED`` (429, ``details.retry_after_sec``) when the limit is exceeded.
    Use directly in services for keys only known there (attempt id, org id)."""
    limiter = get_rate_limiter()
    if not limiter.hit(limit, *keys):
        raise AppError("RATE_LIMITED", 429, {"retry_after_sec": limiter.retry_after(limit, *keys)})


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


Scope = Literal["ip", "user", "org"]


def rate_limit(limit: str | Callable[[], str], *, scope: Scope = "ip", name: str) -> Callable:
    """FastAPI dependency factory. ``limit`` may be a callable (read settings at request time),
    e.g. ``Depends(rate_limit(lambda: settings.rate_limit_login, scope="ip", name="login"))``.
    ``user``/``org`` scopes read the authenticated context (``request.state.ctx``, set by
    ``core.deps.current_ctx``), so put the auth guard before this dependency."""

    async def dependency(request: Request) -> None:
        value = limit() if callable(limit) else limit
        if scope == "ip":
            key = client_ip(request)
        else:
            ctx = getattr(request.state, "ctx", None)
            ident = getattr(ctx, "user_id" if scope == "user" else "org_id", None) if ctx else None
            key = str(ident) if ident else client_ip(request)
        check_rate_limit(value, name, scope, key)

    return dependency
