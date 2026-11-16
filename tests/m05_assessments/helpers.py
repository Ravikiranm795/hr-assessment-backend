"""Payload builders and API shortcuts for the M5 tests."""

from types import SimpleNamespace
from typing import Any

BASE = "/api/v1/assessments"


def single(prompt: str = "<p>2 + 2?</p>", marks: float = 1, **extra: Any) -> dict:
    return {
        "type": "single_choice",
        "prompt_html": prompt,
        "marks": marks,
        "options": [
            {"text_html": "4", "is_correct": True},
            {"text_html": "5", "is_correct": False},
        ],
        **extra,
    }


def multi(partial: bool = False, marks: float = 2) -> dict:
    return {
        "type": "multi_choice",
        "prompt_html": "<p>Pick the primes</p>",
        "marks": marks,
        "options": [
            {"text_html": "2", "is_correct": True},
            {"text_html": "3", "is_correct": True},
            {"text_html": "4", "is_correct": False},
        ],
        "answer_key": {"partial": partial},
    }


def fill(blanks: list[list[str]] | None = None, marks: float = 2) -> dict:
    blanks = [["Paris"], ["France"]] if blanks is None else blanks
    return {
        "type": "fill_blank",
        "prompt_html": "<p>___ is the capital of ___</p>",
        "marks": marks,
        "answer_key": {"blanks": [{"accepted": accepted} for accepted in blanks]},
    }


def gap(scoring_mode: str = "auto", **extra: Any) -> dict:
    body = {
        "type": "code_gap",
        "prompt_html": "<pre>for i in ___(3):</pre>",
        "marks": 1,
        "scoring_mode": scoring_mode,
        "code_language": "python",
        "answer_key": {"gaps": [{"accepted": ["range"]}]},
    }
    body.update(extra)
    return body


def short(scoring_mode: str = "manual", **extra: Any) -> dict:
    body = {"type": "short_answer", "prompt_html": "<p>Explain TCP</p>", "marks": 5, "scoring_mode": scoring_mode}
    body.update(extra)
    return body


async def new_assessment(client, headers: dict, title: str = "Backend test") -> SimpleNamespace:
    resp = await client.post(BASE, json={"title": title, "description": "d"}, headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    draft = next(v for v in body["versions"] if v["status"] == "draft")
    return SimpleNamespace(id=body["id"], draft_id=draft["id"], body=body)


async def add_question(client, headers: dict, aid: str, vid: str, payload: dict | None = None) -> dict:
    resp = await client.post(f"{BASE}/{aid}/versions/{vid}/questions", json=payload or single(), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def publish(client, headers: dict, aid: str, vid: str):
    return await client.post(f"{BASE}/{aid}/versions/{vid}/publish", headers=headers)


async def published_assessment(client, headers: dict, title: str = "Published") -> SimpleNamespace:
    made = await new_assessment(client, headers, title)
    await add_question(client, headers, made.id, made.draft_id)
    resp = await publish(client, headers, made.id, made.draft_id)
    assert resp.status_code == 200, resp.text
    made.v1 = made.draft_id
    return made
