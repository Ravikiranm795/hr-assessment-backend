"""Fixtures for M4 tests: HTTP client on the real app, org + OA headers, fake storage, SMTP and AI."""

import io
import struct
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import db as core_db
from app.core.ai import MockProvider, set_provider_override
from app.core.email import MemoryEmailSender, set_email_sender
from app.core.ratelimit import set_rate_limiter
from app.core.storage import set_storage
from app.core.storage.local import LocalStorage
from app.main import create_app
from tests import factories as f


@pytest_asyncio.fixture(autouse=True)
async def _isolation(tmp_path) -> AsyncIterator[None]:
    """Engine per event loop, fresh rate limiter, local storage in tmp_path, no leaked overrides."""
    core_db._engine.cache_clear()
    set_rate_limiter(None)
    set_storage(LocalStorage(root=str(tmp_path / "storage"), base_url="http://api.test"))
    set_email_sender(None)
    set_provider_override(None)
    yield
    set_email_sender(None)
    set_provider_override(None)
    set_storage(None)
    set_rate_limiter(None)
    engine = core_db.get_engine()
    await engine.dispose()
    core_db._engine.cache_clear()


@pytest_asyncio.fixture
async def http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@dataclass
class Tenant:
    org: Any
    admin: Any
    headers: dict[str, str]


@pytest_asyncio.fixture
async def tenant(db_session: AsyncSession) -> Tenant:
    org = await f.make_org(db_session)
    admin = await f.make_user(db_session, "org_admin", org)
    headers = await f.auth_headers(db_session, "org_admin", org, user=admin)
    await db_session.commit()
    return Tenant(org=org, admin=admin, headers=headers)


@pytest_asyncio.fixture
async def other_tenant(db_session: AsyncSession) -> Tenant:
    org = await f.make_org(db_session)
    admin = await f.make_user(db_session, "org_admin", org)
    headers = await f.auth_headers(db_session, "org_admin", org, user=admin)
    await db_session.commit()
    return Tenant(org=org, admin=admin, headers=headers)


@pytest.fixture
def role_headers(db_session: AsyncSession, tenant: Tenant) -> Callable[[str], Any]:
    async def make(role: str) -> dict[str, str]:
        if role == "org_admin":
            return tenant.headers
        org = None if role == "super_admin" else tenant.org
        headers = await f.auth_headers(db_session, role, org)
        await db_session.commit()
        return headers

    return make


@pytest.fixture
def outbox() -> MemoryEmailSender:
    sender = MemoryEmailSender()
    set_email_sender(sender)
    return sender


@pytest.fixture
def mock_ai() -> MockProvider:
    provider = MockProvider({"ok": True})
    set_provider_override(provider)
    return provider


async def audit_rows(session: AsyncSession, org_id, action: str) -> list[dict]:
    result = await session.execute(
        text("SELECT * FROM audit_logs WHERE org_id = :o AND action = :a ORDER BY created_at"),
        {"o": org_id, "a": action},
    )
    return [dict(r) for r in result.mappings().all()]


async def scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    # fresh snapshot (the app wrote through its own connection); commit, not rollback, keeps ORM objects loaded
    await session.commit()
    return (await session.execute(text(sql), params)).scalar_one_or_none()


async def row(session: AsyncSession, sql: str, **params: Any) -> dict | None:
    await session.commit()
    found = (await session.execute(text(sql), params)).mappings().first()
    return dict(found) if found else None


@pytest.fixture
def enable_flag(db_session: AsyncSession) -> Callable[..., Any]:
    async def set_flag(org_id, key: str, enabled: bool) -> None:
        await db_session.execute(
            text(
                "INSERT INTO org_feature_flags (org_id, flag_key, enabled) VALUES (:o, :k, :e) "
                "ON CONFLICT (org_id, flag_key) DO UPDATE SET enabled = :e"
            ),
            {"o": org_id, "k": key, "e": enabled},
        )
        await db_session.commit()

    return set_flag


# ------------------------------------------------------------------ tiny valid image files


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png_bytes(width: int = 64, height: int = 64) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def jpeg_bytes(width: int = 40, height: int = 30) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + b"\xff\xd9"


def ico_bytes() -> bytes:
    return (
        b"\x00\x00\x01\x00\x01\x00" + b"\x10\x10\x00\x00\x01\x00\x20\x00" + b"\x00" * 8 + b"\x16\x00\x00\x00"
    )


def pdf_bytes(extra: bytes = b"") -> bytes:
    return (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        + extra
        + b"\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def multipart(name: str, filename: str, data: bytes, content_type: str) -> dict:
    return {"files": {name: (filename, io.BytesIO(data), content_type)}}
