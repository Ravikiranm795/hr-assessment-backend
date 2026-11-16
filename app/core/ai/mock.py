"""Scripted provider for tests and demo mode. Tests never call a real AI provider."""

from collections.abc import Callable, Iterable
from typing import Any

ResponseItem = dict | BaseException | Callable[[dict[str, Any]], dict]


class MockProvider:
    """Returns ``responses`` in order (the last one repeats once the list is exhausted).

    Each item is a dict (returned), an exception instance (raised), or a callable receiving the
    call kwargs and returning a dict. Every call is recorded in ``calls``.
    """

    def __init__(self, responses: Iterable[ResponseItem] | ResponseItem = (), *, connected: bool = True):
        if isinstance(responses, dict | BaseException) or callable(responses):
            responses = [responses]  # type: ignore[list-item]
        self.responses: list[ResponseItem] = list(responses)  # type: ignore[arg-type]
        self.calls: list[dict[str, Any]] = []
        self.connected = connected

    async def generate_json(
        self, *, system: str, prompt: str, json_schema: dict, max_tokens: int, timeout_sec: int
    ) -> dict:
        call = {
            "system": system,
            "prompt": prompt,
            "json_schema": json_schema,
            "max_tokens": max_tokens,
            "timeout_sec": timeout_sec,
        }
        self.calls.append(call)
        if not self.responses:
            return {}
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        item = self.responses[index]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(call)
        return item

    async def test_connection(self) -> bool:
        return self.connected
