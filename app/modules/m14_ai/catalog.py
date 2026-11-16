"""Reader for ``ai_models.json`` (editable provider and model catalog, spec 7.1).

The file is re-read when its modification time changes, so editing it needs no restart.
"""

import json
import threading
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("ai_models.json")

_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


def load_catalog() -> dict[str, Any]:
    global _cache
    mtime = CATALOG_PATH.stat().st_mtime
    with _lock:
        if _cache is None or _cache[0] != mtime:
            _cache = (mtime, json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
        return _cache[1]


def provider_entry(provider: str) -> dict[str, Any] | None:
    return load_catalog().get("providers", {}).get(provider)


def default_model(provider: str) -> str | None:
    entry = provider_entry(provider)
    return entry.get("default_model") if entry else None


def default_base_url(provider: str) -> str | None:
    entry = provider_entry(provider)
    return entry.get("default_base_url") if entry else None
