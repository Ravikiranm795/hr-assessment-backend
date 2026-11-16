"""AI provider catalog for ``GET /org/settings/ai/providers`` (spec 7.1, D-08).

Defaults come from ``app/modules/m14_ai/ai_models.json`` when that file exists and is readable;
M14 owns it. It is read defensively (several shapes accepted, any problem falls back), so this
module never depends on M14 being present or well formed.
"""

import json
import logging
from pathlib import Path
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

MODELS_FILE = Path(__file__).resolve().parents[1] / "m14_ai" / "ai_models.json"

CLAUDE, GEMINI, OPENAI, OPENAI_COMPATIBLE, CLI = "claude", "gemini", "openai", "openai-compatible", "cli"
PROVIDER_IDS: tuple[str, ...] = (CLAUDE, GEMINI, OPENAI, OPENAI_COMPATIBLE, CLI)
API_PROVIDERS: tuple[str, ...] = (CLAUDE, GEMINI, OPENAI, OPENAI_COMPATIBLE)

_ALIASES = {
    "anthropic": CLAUDE,
    "google": GEMINI,
    "openai_compatible": OPENAI_COMPATIBLE,
    "compatible": OPENAI_COMPATIBLE,
}

# Light, cheap defaults (spec 7.1). Used when ai_models.json is missing or does not name a model.
_FALLBACK: dict[str, dict[str, Any]] = {
    CLAUDE: {
        "label": "Claude (Anthropic)",
        "default_model": "claude-haiku-5-5",
        "models": ["claude-haiku-5-5"],
    },
    GEMINI: {"label": "Gemini (Google)", "default_model": "gemini-2.5-flash", "models": ["gemini-2.5-flash"]},
    OPENAI: {"label": "OpenAI", "default_model": "gpt-5-mini", "models": ["gpt-5-mini"]},
    OPENAI_COMPATIBLE: {"label": "OpenAI-compatible endpoint", "default_model": None, "models": []},
    CLI: {"label": "Command line (runs on the backend host)", "default_model": None, "models": []},
}


def _model_ids(value: Any) -> list[str]:
    ids = []
    for item in value if isinstance(value, list) else []:
        model = item.get("id") if isinstance(item, dict) else item
        if isinstance(model, str) and model.strip():
            ids.append(model.strip())
    return ids


def _load_overrides() -> dict[str, dict[str, Any]]:
    """``{provider_id: {"default_model": str, "models": [str]}}`` from the M14 file; ``{}`` on any problem."""
    try:
        raw = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("providers"), dict):
        raw = raw["providers"]
    if not isinstance(raw, dict):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        provider = _ALIASES.get(str(key).lower(), str(key).lower())
        if provider not in _FALLBACK:
            continue
        if isinstance(entry, str):
            entry = {"default_model": entry}
        if not isinstance(entry, dict):
            continue
        default = next(
            (
                entry[k].strip()
                for k in ("default_model", "default", "model")
                if isinstance(entry.get(k), str) and entry[k].strip()
            ),
            None,
        )
        models = _model_ids(entry.get("models"))
        if default and default not in models:
            models.insert(0, default)
        if default or models:
            found[provider] = {"default_model": default or models[0], "models": models}
    return found


def provider_catalog() -> list[dict[str, Any]]:
    overrides = _load_overrides()
    catalog = []
    for provider in PROVIDER_IDS:
        base = _FALLBACK[provider]
        merged = {**base, **overrides.get(provider, {})}
        entry: dict[str, Any] = {
            "id": provider,
            "label": base["label"],
            "modes": ["cli"] if provider == CLI else ["api"],
            "requires_api_key": provider in (CLAUDE, GEMINI, OPENAI),
            "requires_base_url": provider == OPENAI_COMPATIBLE,
            "requires_model": provider != CLI,
            "default_model": merged["default_model"],
            "models": list(merged["models"]),
        }
        if provider == CLI:
            entry["cli_commands"] = list(settings.ai_cli_allowlist)
        catalog.append(entry)
    return catalog


def default_model(provider: str) -> str | None:
    return next((p["default_model"] for p in provider_catalog() if p["id"] == provider), None)
