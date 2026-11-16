"""Upload validation for logos, favicons and policy PDFs (spec 4.9, 6/M4).

The content is trusted over the file name and the declared type: the leading bytes decide the
type, the header must parse, and the declared ``Content-Type`` must agree. SVG is refused (it can
carry script). Size limits come from settings (``MAX_UPLOAD_MB``).
"""

import struct
from dataclasses import dataclass

from fastapi import UploadFile

from app.core.config import settings
from app.core.errors import AppError

MIN_DIMENSION = 16
MAX_DIMENSION = 4096
PDF_ACTIVE_TOKENS = (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile", b"/RichMedia")

_ICO_TYPES = {"image/x-icon", "image/vnd.microsoft.icon"}
_NEUTRAL_TYPES = {"", "application/octet-stream"}
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/x-icon": "ico"}


@dataclass(frozen=True)
class ImageInfo:
    content_type: str
    width: int
    height: int

    @property
    def extension(self) -> str:
        return _EXTENSIONS[self.content_type]


def _invalid(reason: str) -> AppError:
    return AppError("UPLOAD_INVALID", 422, {"reason": reason})


async def read_limited(upload: UploadFile | None) -> tuple[bytes, str]:
    """Read at most ``MAX_UPLOAD_MB`` (+1 byte to detect overflow). Returns (data, declared type)."""
    if upload is None:
        raise _invalid("missing_file")
    limit = settings.max_upload_mb * 1024 * 1024
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise _invalid("too_large")
    if not data:
        raise _invalid("empty_file")
    return data, (upload.content_type or "").split(";")[0].strip().lower()


def _png(data: bytes) -> tuple[int, int]:
    if len(data) < 33 or data[12:16] != b"IHDR" or b"IEND" not in data[-16:]:
        raise _invalid("image_corrupt")
    return struct.unpack(">II", data[16:24])


def _jpeg(data: bytes) -> tuple[int, int]:
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            raise _invalid("image_corrupt")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > n:
                break
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        if length < 2:
            break
        i += 2 + length
    raise _invalid("image_corrupt")


def _webp(data: bytes) -> tuple[int, int]:
    kind = data[12:16]
    try:
        if kind == b"VP8 ":
            width, height = struct.unpack("<HH", data[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if kind == b"VP8L":
            bits = struct.unpack("<I", data[21:25])[0]
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if kind == b"VP8X":
            return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    except struct.error:
        pass
    raise _invalid("image_corrupt")


def _ico(data: bytes) -> tuple[int, int]:
    if len(data) < 22 or struct.unpack("<H", data[4:6])[0] < 1:
        raise _invalid("image_corrupt")
    width, height = data[6] or 256, data[7] or 256
    return width, height


def inspect_image(data: bytes, *, declared: str = "", allow_ico: bool = False) -> ImageInfo:
    """Identify an image by its magic bytes and check its header and dimensions.
    ``UPLOAD_INVALID`` (``details.reason``) otherwise."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type, dims = "image/png", _png
    elif data.startswith(b"\xff\xd8\xff"):
        content_type, dims = "image/jpeg", _jpeg
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        content_type, dims = "image/webp", _webp
    elif allow_ico and data[:4] == b"\x00\x00\x01\x00":
        content_type, dims = "image/x-icon", _ico
    else:
        raise _invalid("unsupported_type")
    if (
        declared not in _NEUTRAL_TYPES
        and declared != content_type
        and not (content_type == "image/x-icon" and declared in _ICO_TYPES)
    ):
        raise _invalid("content_type_mismatch")
    width, height = dims(data)
    if not (MIN_DIMENSION <= width <= MAX_DIMENSION and MIN_DIMENSION <= height <= MAX_DIMENSION):
        raise _invalid("bad_dimensions")
    return ImageInfo(content_type, width, height)


def inspect_pdf(data: bytes, *, declared: str = "application/pdf") -> str:
    """A PDF by magic bytes and trailer, without script or launch actions."""
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
        raise _invalid("unsupported_type")
    if declared not in _NEUTRAL_TYPES and declared != "application/pdf":
        raise _invalid("content_type_mismatch")
    if any(token in data for token in PDF_ACTIVE_TOKENS):
        raise _invalid("active_content")
    return "application/pdf"
