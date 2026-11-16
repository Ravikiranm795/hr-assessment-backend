import asyncpg
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import __version__
from app.core.config import settings

router = APIRouter(tags=["health"])

# Public, unauthenticated endpoints still declare the OpenAPI extensions every endpoint carries.
_PUBLIC = {"x-roles": [], "x-required-flag": None}


@router.get(
    "/health",
    summary="Liveness check",
    description="Returns `ok` while the process is running. It does not touch the database; use it for "
    "container liveness probes.",
    responses={
        200: {"content": {"application/json": {"example": {"status": "ok"}}}},
        500: {
            "description": "Unexpected server error (`INTERNAL_ERROR`).",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message_key": "errors.common.internal",
                            "details": {},
                            "request_id": "6f1c1b9e-2d7a-4c44-9c52-1f0f1d7a9a10",
                        }
                    }
                }
            },
        },
    },
    openapi_extra=_PUBLIC,
)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/health/ready",
    summary="Readiness check",
    description="Opens a database connection and runs `SELECT 1`. Returns 503 while the database is "
    "unreachable, so load balancers stop sending traffic.",
    responses={
        200: {"content": {"application/json": {"example": {"status": "ready", "database": "ok"}}}},
        503: {
            "description": "The database is not reachable.",
            "content": {"application/json": {"example": {"status": "unavailable", "database": "error"}}},
        },
    },
    openapi_extra=_PUBLIC,
)
async def ready() -> JSONResponse:
    dsn = settings.database_url.replace("+asyncpg", "", 1)
    try:
        conn = await asyncpg.connect(dsn, timeout=3)
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()
    except (OSError, asyncpg.PostgresError, TimeoutError):
        return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)
    return JSONResponse({"status": "ready", "database": "ok"})


@router.get(
    "/version",
    summary="Application version",
    description="Product slug, build version and runtime environment (`dev`, `test` or `prod`).",
    responses={
        200: {
            "content": {
                "application/json": {"example": {"name": "hr-assessment", "version": "0.1.0", "env": "dev"}}
            }
        },
        500: {
            "description": "Unexpected server error (`INTERNAL_ERROR`).",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message_key": "errors.common.internal",
                            "details": {},
                            "request_id": "6f1c1b9e-2d7a-4c44-9c52-1f0f1d7a9a10",
                        }
                    }
                }
            },
        },
    },
    openapi_extra=_PUBLIC,
)
async def version() -> dict[str, str]:
    return {"name": settings.app_slug, "version": __version__, "env": settings.app_env}
