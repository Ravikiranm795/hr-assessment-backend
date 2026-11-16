"""Shared helpers for the concrete providers: JSON extraction, a small schema validator and
the HTTP plumbing. Providers raise ``AI_PROVIDER_ERROR`` for transport or vendor failures and
``AI_OUTPUT_INVALID`` when the output is not JSON that matches the schema (core.ai.base)."""

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.errors import AppError

log = logging.getLogger("app.ai")

_FENCE = re.compile(r"^```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


@dataclass
class Usage:
    tokens_in: int | None = None
    tokens_out: int | None = None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usage_of(tokens_in: Any, tokens_out: Any) -> Usage:
    return Usage(_as_int(tokens_in), _as_int(tokens_out))


def _balanced_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth, in_str, escaped = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def parse_json_output(text: str) -> dict:
    """Parse model text into a JSON object: plain JSON, a fenced block, or the first balanced
    ``{...}`` in surrounding prose. Anything else is ``AI_OUTPUT_INVALID``."""
    candidate = (text or "").strip()
    if not candidate:
        raise AppError("AI_OUTPUT_INVALID", details={"reason": "empty"})
    fenced = _FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    for attempt in (candidate, _balanced_object(candidate)):
        if not attempt:
            continue
        try:
            value = json.loads(attempt)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise AppError("AI_OUTPUT_INVALID", details={"reason": "not_json_object"})


_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, float) and not math.isfinite(value):
        return False
    return isinstance(value, _TYPES.get(expected, (object,)))


def validate_schema(
    value: Any, schema: dict, *, max_depth: int = 2, _depth: int = 0, path: str = "$"
) -> None:
    """Validate ``value`` against the subset of JSON Schema we send (type, required, properties,
    items, enum). Validation stops at ``max_depth`` levels, so one malformed list item does not
    reject a whole response; the caller validates and drops items individually."""
    expected = schema.get("type")
    if expected and not _type_ok(value, expected):
        raise AppError("AI_OUTPUT_INVALID", details={"reason": "schema", "path": path})
    if "enum" in schema and value not in schema["enum"]:
        raise AppError("AI_OUTPUT_INVALID", details={"reason": "schema", "path": path})
    if _depth >= max_depth:
        return
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise AppError("AI_OUTPUT_INVALID", details={"reason": "schema", "path": f"{path}.{name}"})
        for name, sub in schema.get("properties", {}).items():
            if name in value and value[name] is not None:
                validate_schema(
                    value[name], sub, max_depth=max_depth, _depth=_depth + 1, path=f"{path}.{name}"
                )
    elif isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_schema(
                item, schema["items"], max_depth=max_depth, _depth=_depth + 1, path=f"{path}[{index}]"
            )


class HttpProvider:
    """Base for API-mode providers. ``transport`` lets tests inject ``httpx.MockTransport``;
    production never sets it, and tests never reach a real network."""

    name = "http"

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._transport = transport
        self.last_usage: Usage | None = None

    def __repr__(self) -> str:  # never show the key
        return f"{type(self).__name__}(model={self.model!r})"

    async def _post(self, url: str, *, headers: dict[str, str], body: dict, timeout_sec: int) -> dict:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_sec), transport=self._transport, follow_redirects=False
            ) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            log.warning("ai provider timeout provider=%s", self.name)
            raise AppError("AI_PROVIDER_ERROR", details={"reason": "timeout"}) from exc
        except httpx.HTTPError as exc:
            log.warning("ai provider transport error provider=%s type=%s", self.name, type(exc).__name__)
            raise AppError("AI_PROVIDER_ERROR", details={"reason": "transport"}) from exc
        if response.status_code >= 400:
            log.warning("ai provider http error provider=%s status=%s", self.name, response.status_code)
            raise AppError("AI_PROVIDER_ERROR", details={"reason": "http", "status": response.status_code})
        try:
            payload = response.json()
        except ValueError as exc:
            raise AppError("AI_PROVIDER_ERROR", details={"reason": "bad_response"}) from exc
        if not isinstance(payload, dict):
            raise AppError("AI_PROVIDER_ERROR", details={"reason": "bad_response"})
        return payload

    async def test_connection(self) -> bool:
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        try:
            await self.generate_json(
                system="Reply with JSON only.",
                prompt='Return {"ok": true}.',
                json_schema=schema,
                max_tokens=256,
                timeout_sec=20,
            )
        except AppError as exc:
            # The vendor answered but the output was not the expected JSON: the connection works.
            return exc.code == "AI_OUTPUT_INVALID"
        return True

    async def generate_json(
        self, *, system: str, prompt: str, json_schema: dict, max_tokens: int, timeout_sec: int
    ) -> dict:
        raise NotImplementedError
