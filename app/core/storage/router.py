"""Signed-token file endpoint (spec 4.9). The token is the authorization: it is issued only by
``ObjectStorage.signed_url`` after the caller's own authorization check and audit entry."""

from fastapi import APIRouter
from fastapi.responses import Response

from app.core.storage import get_storage
from app.core.storage.base import FILES_PATH

router = APIRouter(tags=["files"])

# Served inline; anything else (HTML, SVG, ...) is forced to an octet-stream download.
INLINE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/x-icon", "application/pdf"})


@router.get(
    FILES_PATH + "/{token}",
    response_class=Response,
    summary="Download an object through a short-lived signed token",
    openapi_extra={"x-roles": [], "x-required-flag": None},
)
async def get_signed_file(token: str) -> Response:
    storage = get_storage()
    key = storage.verify_signed_token(token)
    data = await storage.get(key)
    media_type = await storage.content_type(key)
    inline = media_type in INLINE_TYPES
    return Response(
        content=data,
        media_type=media_type if inline else "application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": "inline" if inline else "attachment",
        },
    )
