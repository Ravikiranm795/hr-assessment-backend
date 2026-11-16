"""Anthropic (Claude) Messages API. JSON is obtained with a forced tool call whose input schema
is the requested schema, which makes the model return structured arguments."""

import httpx

from app.core.errors import AppError
from app.modules.m14_ai import catalog
from app.modules.m14_ai.providers.base import HttpProvider, usage_of, validate_schema

ANTHROPIC_VERSION = "2023-06-01"
TOOL_NAME = "emit_json"


class AnthropicProvider(HttpProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(
            model=model,
            base_url=base_url or catalog.default_base_url("anthropic") or "https://api.anthropic.com",
            api_key=api_key,
            transport=transport,
        )

    async def generate_json(
        self, *, system: str, prompt: str, json_schema: dict, max_tokens: int, timeout_sec: int
    ) -> dict:
        schema = {**json_schema, "type": "object"}
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": "Return the final answer as structured JSON.",
                    "input_schema": schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }
        headers = {
            "x-api-key": self._api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        payload = await self._post(
            f"{self.base_url}/v1/messages", headers=headers, body=body, timeout_sec=timeout_sec
        )
        usage = payload.get("usage") or {}
        self.last_usage = usage_of(usage.get("input_tokens"), usage.get("output_tokens"))
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
                result = block.get("input")
                if not isinstance(result, dict):
                    break
                validate_schema(result, json_schema)
                return result
        raise AppError("AI_OUTPUT_INVALID", details={"reason": "no_tool_output"})
