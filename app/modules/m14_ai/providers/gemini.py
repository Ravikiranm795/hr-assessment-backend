"""Google Gemini ``generateContent`` (v1beta) with ``responseMimeType=application/json``.

Model ids come from ``ai_models.json``; the key is sent in the ``x-goog-api-key`` header
(never in the URL, so it cannot leak into access logs).
"""

import re

import httpx

from app.core.errors import AppError
from app.modules.m14_ai import catalog
from app.modules.m14_ai.providers.base import HttpProvider, parse_json_output, usage_of, validate_schema

_MODEL_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class GeminiProvider(HttpProvider):
    name = "gemini"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not _MODEL_ID.match(model):
            raise AppError("AI_NOT_CONFIGURED", details={"reason": "bad_model_id"})
        super().__init__(
            model=model,
            base_url=base_url
            or catalog.default_base_url("gemini")
            or "https://generativelanguage.googleapis.com",
            api_key=api_key,
            transport=transport,
        )

    async def generate_json(
        self, *, system: str, prompt: str, json_schema: dict, max_tokens: int, timeout_sec: int
    ) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": json_schema,
                "maxOutputTokens": max_tokens,
            },
        }
        headers = {"x-goog-api-key": self._api_key or "", "content-type": "application/json"}
        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        payload = await self._post(url, headers=headers, body=body, timeout_sec=timeout_sec)
        usage = payload.get("usageMetadata") or {}
        self.last_usage = usage_of(usage.get("promptTokenCount"), usage.get("candidatesTokenCount"))
        text = ""
        candidates = payload.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        result = parse_json_output(text)
        validate_schema(result, json_schema)
        return result
