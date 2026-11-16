"""``ObjectStorage`` interface (spec 4.9). Drivers implement the ``_write/_read/_remove``
primitives; encryption, key validation and signed URLs live here so every driver behaves the same.

- Encrypted objects are stored as ``MAGIC + security.encrypt_bytes(data)`` and decrypted
  transparently by ``get``, so callers never handle ciphertext.
- ``signed_url`` points at the API endpoint ``GET /api/v1/files/{token}`` (core/storage/router.py)
  for every driver, so encrypted snapshots are decrypted server-side and HR never gets a bucket
  URL. The token is ``base64url(json{k,exp}) + "." + base64url(HMAC-SHA-256)``.
"""

import base64
import hashlib
import hmac
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core import security
from app.core.config import settings
from app.core.errors import AppError

ENCRYPTED_MAGIC = b"HRENC1\x00"
FILES_PATH = "/api/v1/files"
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")


@dataclass(frozen=True)
class StoredObject:
    key: str
    content_type: str
    size_bytes: int  # plaintext size
    sha256: str  # of the plaintext
    encrypted: bool


def validate_key(key: str) -> str:
    """Keys are relative, slash-separated, with no ``..``/empty segments. ``UPLOAD_INVALID`` otherwise.
    Convention: ``<org_id>/<kind>/<uuid>.<ext>``."""
    if not isinstance(key, str) or len(key) > 512 or not _KEY_RE.match(key) or ".." in key.split("/"):
        raise AppError("UPLOAD_INVALID", 422, {"reason": "invalid_key"})
    return key


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    if not settings.token_pepper:
        raise RuntimeError("TOKEN_PEPPER is not configured")
    key = hashlib.sha256(b"storage-url:" + settings.token_pepper.encode()).digest()
    return _b64e(hmac.new(key, payload.encode(), hashlib.sha256).digest())


class ObjectStorage(ABC):
    base_url: str = ""

    async def put(self, key: str, data: bytes, content_type: str, encrypt: bool = False) -> StoredObject:
        validate_key(key)
        payload = ENCRYPTED_MAGIC + security.encrypt_bytes(data) if encrypt else data
        await self._write(key, payload, content_type)
        return StoredObject(
            key=key,
            content_type=content_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            encrypted=encrypt,
        )

    async def get(self, key: str) -> bytes:
        """Plaintext bytes. ``NOT_FOUND`` if missing."""
        validate_key(key)
        payload = await self._read(key)
        if payload.startswith(ENCRYPTED_MAGIC):
            return security.decrypt_bytes(payload[len(ENCRYPTED_MAGIC) :])
        return payload

    async def delete(self, key: str) -> None:
        """Idempotent."""
        validate_key(key)
        await self._remove(key)

    async def content_type(self, key: str) -> str:
        return "application/octet-stream"

    async def signed_url(self, key: str, ttl_sec: int = 60) -> str:
        """Short-lived URL (default 60 s) for an authorized viewer. Authorize and audit first."""
        base = (self.base_url or settings.api_base_url).rstrip("/")
        return f"{base}{FILES_PATH}/{self.make_signed_token(key, ttl_sec)}"

    def make_signed_token(self, key: str, ttl_sec: int = 60, now: float | None = None) -> str:
        exp = int((now if now is not None else time.time()) + ttl_sec)
        payload = _b64e(json.dumps({"k": validate_key(key), "exp": exp}, separators=(",", ":")).encode())
        return f"{payload}.{_sign(payload)}"

    def verify_signed_token(self, token: str, now: float | None = None) -> str:
        """Return the object key, or raise ``NOT_FOUND`` (bad signature, malformed or expired)."""
        try:
            payload, sig = token.split(".")
            if not hmac.compare_digest(sig, _sign(payload)):
                raise ValueError("bad signature")
            data = json.loads(_b64d(payload))
            if (now if now is not None else time.time()) > data["exp"]:
                raise ValueError("expired")
            return validate_key(data["k"])
        except (ValueError, KeyError, TypeError, AppError) as exc:
            raise AppError("NOT_FOUND") from exc

    @abstractmethod
    async def _write(self, key: str, payload: bytes, content_type: str) -> None: ...

    @abstractmethod
    async def _read(self, key: str) -> bytes: ...

    @abstractmethod
    async def _remove(self, key: str) -> None: ...
