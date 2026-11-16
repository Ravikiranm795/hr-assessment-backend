"""Local-disk driver (demo and development). Content type is kept in a sidecar file."""

import asyncio
from pathlib import Path

from app.core.config import settings
from app.core.errors import AppError
from app.core.storage.base import ObjectStorage, validate_key

_CT_SUFFIX = ".content-type"


class LocalStorage(ObjectStorage):
    def __init__(self, root: str | None = None, base_url: str | None = None) -> None:
        self.root = Path(root or settings.storage_local_path).resolve()
        self.base_url = base_url or ""

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise AppError("UPLOAD_INVALID", 422, {"reason": "invalid_key"})
        return path

    async def _write(self, key: str, payload: bytes, content_type: str) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(payload)
            tmp.replace(path)
            path.with_name(path.name + _CT_SUFFIX).write_text(content_type)

        await asyncio.to_thread(write)

    async def _read(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise AppError("NOT_FOUND") from exc

    async def _remove(self, key: str) -> None:
        path = self._path(key)
        for target in (path, path.with_name(path.name + _CT_SUFFIX)):
            target.unlink(missing_ok=True)

    async def content_type(self, key: str) -> str:
        path = self._path(key)
        try:
            return path.with_name(path.name + _CT_SUFFIX).read_text().strip() or "application/octet-stream"
        except FileNotFoundError:
            return "application/octet-stream"
