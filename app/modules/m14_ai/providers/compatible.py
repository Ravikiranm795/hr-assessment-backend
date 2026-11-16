"""OpenAI-compatible servers (local or self-hosted, custom base URL). Uses the widely supported
``json_object`` response format and puts the schema in the system prompt. The API key is optional."""

import httpx

from app.core.errors import AppError
from app.modules.m14_ai.providers.openai import OpenAIProvider


class OpenAICompatibleProvider(OpenAIProvider):
    name = "openai_compatible"
    use_json_schema = False
    token_param = "max_tokens"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not base_url or not base_url.lower().startswith(("http://", "https://")):
            raise AppError("AI_NOT_CONFIGURED", details={"reason": "base_url_required"})
        super().__init__(model=model, api_key=api_key, base_url=base_url, transport=transport)
