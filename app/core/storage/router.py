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
    description="The token is the authorization: it is issued only after the caller passed the owning "
    "module's own access check and audit entry, and it expires after about a minute. Images and PDFs are "
    "served inline, everything else as an attachment. Encrypted snapshots are decrypted server-side. An "
    "invalid or expired token is `NOT_FOUND`.",
    responses={
        200: {
            "description": "The object bytes with its content type.",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        },
        404: {
            "description": "NOT_FOUND: bad signature, malformed or expired token.",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": "NOT_FOUND",
                            "message_key": "errors.common.not_found",
                            "details": {},
                            "request_id": "6f1c1b9e-2d7a-4c44-9c52-1f0f1d7a9a10",
                        }
                    }
                }
            },
        },
    },
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
