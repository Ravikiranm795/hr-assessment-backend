"""A scripted candidate: drives ``ExamService`` exactly as the exam routes do (open, system check,
consent, baseline photo, start, answers, events, snapshots, help request, submit), without HTTP."""

import io
import struct
import zlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from starlette.datastructures import Headers, UploadFile

from app.core import security
from app.core.db import TenantContext, tenant_session
from app.core.deps import ExamContext
from app.core.roles import CANDIDATE
from app.db import ProctorEvent
from app.modules.m08_exam.schemas import (
    AnswerIn,
    ConsentIn,
    EventIn,
    EventsIn,
    HelpIn,
    StateOut,
    SystemCheckIn,
)
from app.modules.m08_exam.service import ExamService

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


def placeholder_png(seed: int, width: int = 160, height: int = 120) -> bytes:
    """A small generated gradient PNG (stands in for a camera frame; no real person is shown)."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type: none
        for x in range(width):
            rows += bytes(((x * 255 // width + seed * 37) % 256, y * 255 // height, (seed * 71 + 90) % 256))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def png_upload(seed: int) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(placeholder_png(seed)),
        filename="placeholder.png",
        headers=Headers({"content-type": "image/png"}),
    )


class Candidate:
    """One candidate browser session for one invite token."""

    def __init__(self, token: str, label: str, ip: str) -> None:
        self.token, self.label, self.ip = token, label, ip
        self.exam: ExamContext | None = None

    def _ctx(self, **overrides) -> TenantContext:
        values = {
            "org_id": None,
            "user_id": None,
            "role": "anonymous",
            "is_platform": True,
            "request_id": f"seed-{self.label}",
            "ip": self.ip,
            "user_agent": DESKTOP_UA,
        }
        values.update(overrides)
        return TenantContext(**values)

    async def open(self) -> None:
        ctx = self._ctx()
        async with tenant_session(ctx) as session:
            out, _cookie = await ExamService.open(ctx, session, self.token, None)
        claims = security.decode_token(out.session_token, audience="exam")
        org_id = UUID(claims.org_id)
        self.exam = ExamContext(
            ctx=self._ctx(
                org_id=org_id,
                role=CANDIDATE,
                is_platform=False,
                actor_type="candidate",
                session_id=claims.sid,
            ),
            attempt_id=UUID(claims.sub),
            invite_id=UUID(claims.sid),
            binding=claims.binding,
        )

    @property
    def attempt_id(self) -> UUID:
        assert self.exam is not None
        return self.exam.attempt_id

    async def state(self) -> StateOut:
        assert self.exam is not None
        async with tenant_session(self.exam.ctx) as session:
            return await ExamService.state(session, self.exam)

    async def prepare(self, photo_seed: int = 1) -> StateOut:
        """System check, consent and (when required) the baseline photo."""
        assert self.exam is not None
        state = await self.state()
        camera = state.camera_required
        async with tenant_session(self.exam.ctx) as session:
            await ExamService.system_check(
                session, self.exam, SystemCheckIn(viewport_w=1440, viewport_h=900, camera_permission=camera)
            )
        consent_id = state.consent_document.document_version_id if state.consent_document else None
        async with tenant_session(self.exam.ctx) as session:
            await ExamService.consent(session, self.exam, ConsentIn(document_version_id=consent_id))
        if camera:
            async with tenant_session(self.exam.ctx) as session:
                await ExamService.baseline_photo(session, self.exam, png_upload(photo_seed))
        return state

    async def ask_help(self, reason: str, note: str | None = None) -> None:
        assert self.exam is not None
        async with tenant_session(self.exam.ctx) as session:
            await ExamService.create_help_request(session, self.exam, HelpIn(reason=reason, note=note))

    async def start(self) -> None:
        assert self.exam is not None
        async with tenant_session(self.exam.ctx) as session:
            await ExamService.start(session, self.exam)

    async def answer(self, question_id: UUID, answer: dict) -> None:
        assert self.exam is not None
        async with tenant_session(self.exam.ctx) as session:
            await ExamService.save_answer(session, self.exam, question_id, AnswerIn(answer=answer))

    async def send_events(self, events: list[tuple[str, int | None]]) -> list[ProctorEvent]:
        """Ingest events (server-assigned severity); returns the stored rows (``meta.seq`` = send order)."""
        assert self.exam is not None
        body = EventsIn(
            events=[
                EventIn(type=kind, duration_ms=duration, occurred_at=datetime.now(UTC), meta={"seq": index})
                for index, (kind, duration) in enumerate(events)
            ]
        )
        async with tenant_session(self.exam.ctx) as session:
            await ExamService.ingest_events(session, self.exam, body)
            rows = await session.execute(
                select(ProctorEvent).where(ProctorEvent.attempt_id == self.exam.attempt_id)
            )
            return list(rows.scalars())

    async def snapshot(self, event_id: UUID, seed: int) -> None:
        assert self.exam is not None
        async with tenant_session(self.exam.ctx) as session:
            await ExamService.snapshot(session, self.exam, png_upload(seed), event_id)

    async def submit(self) -> None:
        assert self.exam is not None
        async with tenant_session(self.exam.ctx) as session:
            await ExamService.submit(session, self.exam)
