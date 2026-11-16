"""Object storage: ``get_storage()`` returns the driver selected by ``STORAGE_DRIVER``."""

from app.core.config import settings
from app.core.storage.base import ObjectStorage, StoredObject, validate_key

__all__ = ["ObjectStorage", "StoredObject", "get_storage", "set_storage", "validate_key"]

_storage: ObjectStorage | None = None


def get_storage() -> ObjectStorage:
    global _storage
    if _storage is None:
        if settings.storage_driver == "s3":
            from app.core.storage.s3 import S3Storage

            _storage = S3Storage()
        else:
            from app.core.storage.local import LocalStorage

            _storage = LocalStorage()
    return _storage


def set_storage(storage: ObjectStorage | None) -> None:
    """Override the driver (tests); ``None`` re-reads settings on next use."""
    global _storage
    _storage = storage
