import asyncpg
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import __version__
from app.core.config import settings

router = APIRouter(tags=["health"])

# Public, unauthenticated endpoints still declare the OpenAPI extensions every endpoint carries.
_PUBLIC = {"x-roles": [], "x-required-flag": None}


@router.get("/health", openapi_extra=_PUBLIC)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", openapi_extra=_PUBLIC)
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


@router.get("/version", openapi_extra=_PUBLIC)
async def version() -> dict[str, str]:
    return {"name": settings.app_slug, "version": __version__, "env": settings.app_env}
