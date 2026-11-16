"""``GET /metrics``: the counters of ``app.core.metrics`` for the platform (super admin) only."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.core.db import TenantContext
from app.core.deps import require_role
from app.core.metrics import render
from app.core.roles import SUPER_ADMIN

router = APIRouter(tags=["health"])
Platform = Annotated[TenantContext, Depends(require_role(SUPER_ADMIN))]


@router.get(
    "/metrics",
    summary="Metrics counters",
    description="Prometheus text format: HTTP requests, AI calls, exam starts and audit events of this "
    "API process. Super admin only (bearer token); not exposed to organizations.",
    response_class=PlainTextResponse,
    responses={
        200: {"content": {"text/plain": {"example": "exam_starts_total 3"}}},
        401: {"description": "UNAUTHENTICATED"},
        403: {"description": "FORBIDDEN (not a super admin)"},
    },
)  # fmt: skip
async def metrics(_: Platform) -> PlainTextResponse:
    return PlainTextResponse(render(), media_type="text/plain; version=0.0.4")
