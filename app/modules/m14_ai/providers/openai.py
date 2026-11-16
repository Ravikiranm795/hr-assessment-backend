"""OpenAI Chat Completions with Structured Outputs (``response_format: json_schema``)."""

import json

import httpx

from app.core.errors import AppError
from app.modules.m14_ai import catalog
from app.modules.m14_ai.providers.base import HttpProvider, parse_json_output, usage_of, validate_schema


class OpenAIProvider(HttpProvider):
    name = "openai"
    # Subclass switches for OpenAI-compatible servers (see compatible.py).
    use_json_schema = True
    token_param = "max_completion_tokens"
    require_key = True

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(
            model=model,
            base_url=base_url or catalog.default_base_url("openai") or "https://api.openai.com/v1",
            api_key=api_key,
            transport=transport,
        )

    def _body(self, system: str, prompt: str, json_schema: dict, max_tokens: int) -> dict:
        body: dict = {
            "model": self.model,
            self.token_param: max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        if self.use_json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": json_schema, "strict": False},
            }
        else:
            body["response_format"] = {"type": "json_object"}
            body["messages"][0]["content"] = (
                f"{system}\n\nReturn one JSON object that matches this JSON Schema:\n"
                f"{json.dumps(json_schema)}"
            )
        return body

    async def generate_json(
        self, *, system: str, prompt: str, json_schema: dict, max_tokens: int, timeout_sec: int
    ) -> dict:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        payload = await self._post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            body=self._body(system, prompt, json_schema, max_tokens),
            timeout_sec=timeout_sec,
        )
        usage = payload.get("usage") or {}
        self.last_usage = usage_of(usage.get("prompt_tokens"), usage.get("completion_tokens"))
        choices = payload.get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise AppError("AI_OUTPUT_INVALID", details={"reason": "no_content"})
        result = parse_json_output(content)
        validate_schema(result, json_schema)
        return result
