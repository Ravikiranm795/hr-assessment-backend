import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import AppError, install_error_handlers
from app.core.ratelimit import (
    InMemoryRateLimiter,
    RateLimiter,
    check_rate_limit,
    get_rate_limiter,
    rate_limit,
    set_rate_limiter,
)


@pytest.fixture(autouse=True)
def fresh_limiter():
    limiter = InMemoryRateLimiter()
    set_rate_limiter(limiter)
    yield limiter
    set_rate_limiter(None)


def test_in_memory_limiter_implements_interface(fresh_limiter):
    assert isinstance(fresh_limiter, RateLimiter)
    assert get_rate_limiter() is fresh_limiter


def test_limit_blocks_after_quota_per_key(fresh_limiter):
    assert fresh_limiter.hit("2/minute", "login", "1.1.1.1")
    assert fresh_limiter.hit("2/minute", "login", "1.1.1.1")
    assert not fresh_limiter.hit("2/minute", "login", "1.1.1.1")
    assert fresh_limiter.hit("2/minute", "login", "2.2.2.2")  # other key unaffected
    fresh_limiter.reset()
    assert fresh_limiter.hit("2/minute", "login", "1.1.1.1")


def test_check_rate_limit_raises_rate_limited():
    check_rate_limit("1 per 20 seconds", "snapshot", "attempt-1")
    with pytest.raises(AppError) as exc:
        check_rate_limit("1 per 20 seconds", "snapshot", "attempt-1")
    assert exc.value.code == "RATE_LIMITED" and exc.value.status == 429
    assert exc.value.details["retry_after_sec"] >= 1


async def test_rate_limit_dependency_per_ip():
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/login", dependencies=[Depends(rate_limit(lambda: "3/minute", scope="ip", name="login"))])
    async def login():
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        codes = [(await http.post("/login")).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
