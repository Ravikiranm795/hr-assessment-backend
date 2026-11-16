"""``LLMProvider`` interface (spec 7.1). Concrete providers (Anthropic, Gemini, OpenAI,
OpenAI-compatible, CLI) are built in Task 10 and registered in ``core.ai.registry``.

Providers raise ``AppError("AI_PROVIDER_ERROR")`` for transport/provider failures and
``AppError("AI_OUTPUT_INVALID")`` when the output is not JSON matching ``json_schema``.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    async def generate_json(
        self, *, system: str, prompt: str, json_schema: dict, max_tokens: int, timeout_sec: int
    ) -> dict: ...

    async def test_connection(self) -> bool: ...
