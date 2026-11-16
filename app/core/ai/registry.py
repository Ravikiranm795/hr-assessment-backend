"""Provider registry: maps a provider name (``org_ai_settings.provider``) to a factory.

Task 10 registers the real providers at import time of its module, e.g.
``register_provider("anthropic", lambda cfg: AnthropicProvider(cfg))``. Tests can force every
lookup to a ``MockProvider`` with ``set_provider_override``.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.core import metrics
from app.core.ai.base import LLMProvider
from app.core.errors import AppError


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    mode: str = "api"  # api | cli
    base_url: str | None = None
    api_key: str | None = None  # decrypted just before use; never log
    cli_command_key: str | None = None
    timeout_sec: int = 60

    def __repr__(self) -> str:  # keep the key out of logs and tracebacks
        return f"ProviderConfig(provider={self.provider!r}, model={self.model!r}, mode={self.mode!r})"


ProviderFactory = Callable[[ProviderConfig], LLMProvider]

_factories: dict[str, ProviderFactory] = {}
_override: LLMProvider | None = None


def register_provider(name: str, factory: ProviderFactory) -> None:
    _factories[name] = factory


def registered_providers() -> list[str]:
    return sorted(_factories)


def set_provider_override(provider: LLMProvider | None) -> None:
    """Return ``provider`` from every ``build_provider`` call (tests, demo); ``None`` clears it."""
    global _override
    _override = provider


def _metered(provider: LLMProvider, label: str) -> LLMProvider:
    """Count ``generate_json`` calls by outcome on this instance (once; ``isinstance`` still holds)."""
    if getattr(provider, "_metered", False):
        return provider
    inner = provider.generate_json

    async def generate_json(**kwargs):  # noqa: ANN003, ANN202 - same keyword-only signature as the protocol
        try:
            result = await inner(**kwargs)
        except BaseException:
            metrics.inc("ai_calls_total", provider=label, outcome="error")
            raise
        metrics.inc("ai_calls_total", provider=label, outcome="ok")
        return result

    try:
        provider.generate_json = generate_json  # type: ignore[method-assign]
        provider._metered = True  # type: ignore[attr-defined]
    except AttributeError:  # slotted or frozen provider: leave it unmetered
        pass
    return provider


def build_provider(config: ProviderConfig | None) -> LLMProvider:
    """Instantiate the configured provider. ``AI_NOT_CONFIGURED`` if missing or unknown."""
    if _override is not None:
        return _metered(_override, "override")
    if config is None or config.provider not in _factories:
        raise AppError("AI_NOT_CONFIGURED")
    return _metered(_factories[config.provider](config), config.provider)
