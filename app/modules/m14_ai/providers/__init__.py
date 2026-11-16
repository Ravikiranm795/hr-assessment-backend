"""Concrete LLM providers and the factory that picks one from ``org_ai_settings`` (spec 7.1).

Importing this package registers every provider in ``core.ai.registry``.
"""

from typing import Any

from app.core.ai import LLMProvider, ProviderConfig, build_provider, register_provider
from app.core.ai.registry import registered_providers
from app.core.config import settings
from app.core.errors import AppError
from app.core.security import decrypt_field
from app.modules.m14_ai import catalog
from app.modules.m14_ai.providers.anthropic import AnthropicProvider
from app.modules.m14_ai.providers.cli import CliProvider
from app.modules.m14_ai.providers.compatible import OpenAICompatibleProvider
from app.modules.m14_ai.providers.gemini import GeminiProvider
from app.modules.m14_ai.providers.openai import OpenAIProvider


def _need_key(cfg: ProviderConfig) -> str:
    if not cfg.api_key:
        raise AppError("AI_NOT_CONFIGURED", details={"reason": "api_key_missing"})
    return cfg.api_key


def _model(cfg: ProviderConfig) -> str:
    model = cfg.model or catalog.default_model(cfg.provider)
    if not model:
        raise AppError("AI_NOT_CONFIGURED", details={"reason": "model_missing"})
    return model


def _cli(cfg: ProviderConfig) -> LLMProvider:
    return CliProvider(command_key=cfg.cli_command_key, model=cfg.model)


def _anthropic(cfg: ProviderConfig) -> LLMProvider:
    if cfg.mode == "cli":
        return _cli(cfg)
    return AnthropicProvider(model=_model(cfg), api_key=_need_key(cfg), base_url=cfg.base_url)


def _gemini(cfg: ProviderConfig) -> LLMProvider:
    if cfg.mode == "cli":
        return _cli(cfg)
    return GeminiProvider(model=_model(cfg), api_key=_need_key(cfg), base_url=cfg.base_url)


def _openai(cfg: ProviderConfig) -> LLMProvider:
    if cfg.mode == "cli":
        return _cli(cfg)
    return OpenAIProvider(model=_model(cfg), api_key=_need_key(cfg), base_url=cfg.base_url)


def _compatible(cfg: ProviderConfig) -> LLMProvider:
    if cfg.mode == "cli":
        return _cli(cfg)
    return OpenAICompatibleProvider(model=_model(cfg), api_key=cfg.api_key, base_url=cfg.base_url)


_FACTORIES = {
    "anthropic": _anthropic,
    "claude": _anthropic,
    "gemini": _gemini,
    "google": _gemini,
    "openai": _openai,
    "openai_compatible": _compatible,
    "cli": _cli,
}
for _name, _factory in _FACTORIES.items():
    register_provider(_name, _factory)


def config_from_settings(row: Any) -> ProviderConfig:
    """``ProviderConfig`` from an ``org_ai_settings`` row, decrypting the key just before use."""
    if row is None:
        raise AppError("AI_NOT_CONFIGURED")
    key: str | None = None
    if getattr(row, "api_key_enc", None):
        try:
            key = decrypt_field(row.api_key_enc)
        except ValueError as exc:
            raise AppError("AI_NOT_CONFIGURED", details={"reason": "key_unreadable"}) from exc
    provider = row.provider
    if row.mode == "cli" and provider not in registered_providers():
        provider = "cli"
    return ProviderConfig(
        provider=provider,
        model=row.model or "",
        mode=row.mode,
        base_url=row.base_url,
        api_key=key,
        cli_command_key=row.cli_command_key,
        timeout_sec=settings.ai_request_timeout_sec,
    )


def get_provider(org_ai_settings: Any) -> LLMProvider:
    """Build the provider for an org's AI settings row (``None`` -> ``AI_NOT_CONFIGURED``).
    ``core.ai.set_provider_override`` (tests, demo) wins over the configured provider."""
    config = config_from_settings(org_ai_settings) if org_ai_settings is not None else None
    return build_provider(config)


__all__ = [
    "AnthropicProvider",
    "CliProvider",
    "GeminiProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "config_from_settings",
    "get_provider",
]
