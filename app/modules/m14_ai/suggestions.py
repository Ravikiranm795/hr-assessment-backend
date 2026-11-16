"""Turns raw model JSON into validated ``SuggestedQuestion`` items (spec 7.2 step 3).

Invalid items are dropped one by one. Text from the model is escaped and wrapped in ``<p>`` so
nothing it returns can inject markup into an assessment.
"""

import html
import re
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.modules.m14_ai.schemas import DIFFICULTIES, QUESTION_TYPES, SuggestedOption, SuggestedQuestion

_BLANK = re.compile(r"_{3,}")
_TOKEN = re.compile(r"[a-z0-9]+")
NEAR_DUPLICATE = 0.8

MAX_PROMPT_CHARS = 4000
MAX_OPTION_CHARS = 500
MAX_TEXT_CHARS = 2000


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("not_text")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit:
        raise ValueError("bad_length")
    return cleaned


class _RawOption(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str
    is_correct: bool = False

    @field_validator("text", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _text(v, MAX_OPTION_CHARS)


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    difficulty: str = "medium"
    prompt: str
    marks: float = 1.0
    options: list[_RawOption] | None = None
    blanks: list[list[str]] | None = None
    code_language: str | None = None
    model_answer: str | None = None
    key_points: list[str] | None = None
    explanation: str | None = Field(default=None, exclude=True)

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in QUESTION_TYPES:
            raise ValueError("bad_type")
        return v

    @field_validator("difficulty", mode="before")
    @classmethod
    def _difficulty(cls, v: Any) -> str:
        v = str(v or "medium").strip().lower()
        return v if v in DIFFICULTIES else "medium"

    @field_validator("prompt", mode="before")
    @classmethod
    def _prompt(cls, v: Any) -> str:
        return _text(v, MAX_PROMPT_CHARS)

    @field_validator("marks", mode="before")
    @classmethod
    def _marks(cls, v: Any) -> float:
        if isinstance(v, bool) or not isinstance(v, int | float):
            return 1.0
        return float(v) if 0 < v <= 100 else 1.0


def _para(text: str) -> str:
    return "<p>" + "<br>".join(html.escape(line) for line in text.splitlines()) + "</p>"


def _accepted(blanks: list[list[str]] | None, markers: int) -> list[dict[str, list[str]]]:
    if not blanks or len(blanks) != markers or markers < 1:
        raise ValueError("blank_mismatch")
    result = []
    for entry in blanks:
        accepted = [a.strip() for a in entry if isinstance(a, str) and a.strip() and len(a) <= 200]
        if not accepted:
            raise ValueError("blank_without_answer")
        result.append({"accepted": accepted})
    return result


def to_suggestion(item: Any) -> SuggestedQuestion | None:
    """Validate one raw item; ``None`` when it is unusable."""
    if not isinstance(item, dict):
        return None
    try:
        raw = _Raw.model_validate(item)
        options: list[SuggestedOption] | None = None
        answer_key: dict | None = None
        model_answer: str | None = None
        code_language: str | None = None
        scoring_mode = "auto"
        if raw.type in ("single_choice", "multi_choice"):
            opts = raw.options or []
            correct = sum(1 for o in opts if o.is_correct)
            if raw.type == "single_choice" and not (2 <= len(opts) <= 6 and correct == 1):
                return None
            if raw.type == "multi_choice" and not (2 <= len(opts) <= 8 and 1 <= correct):
                return None
            if len({o.text.casefold() for o in opts}) != len(opts):
                return None
            options = [SuggestedOption(text_html=_para(o.text), is_correct=o.is_correct) for o in opts]
            if raw.type == "multi_choice":
                answer_key = {"partial": False}
        elif raw.type == "fill_blank":
            answer_key = {
                "blanks": _accepted(raw.blanks, len(_BLANK.findall(raw.prompt))),
                "case_sensitive": False,
            }
        elif raw.type == "code_gap":
            code_language = _text(raw.code_language, 40).lower()
            answer_key = {"gaps": _accepted(raw.blanks, len(_BLANK.findall(raw.prompt)))}
        else:  # short_answer, scored by AI against the model answer and key points (S-10)
            model_answer = _text(raw.model_answer, MAX_TEXT_CHARS)
            points = [_text(p, 500) for p in (raw.key_points or []) if isinstance(p, str) and p.strip()]
            if len(points) < 1:
                return None
            answer_key = {"key_points": points[:12]}
            scoring_mode = "ai"
        return SuggestedQuestion(
            temp_id=f"tmp_{uuid.uuid4().hex[:12]}",
            type=raw.type,  # type: ignore[arg-type]
            prompt_html=_para(raw.prompt),
            marks=raw.marks,
            scoring_mode=scoring_mode,  # type: ignore[arg-type]
            difficulty=raw.difficulty,  # type: ignore[arg-type]
            options=options,
            answer_key=answer_key,
            model_answer=model_answer,
            code_language=code_language,
        )
    except (ValidationError, ValueError):
        return None


def _tokens(text: str) -> frozenset[str]:
    plain = re.sub(r"<[^>]+>", " ", html.unescape(text)).lower()
    return frozenset(_TOKEN.findall(plain))


def is_near_duplicate(a: SuggestedQuestion, b: SuggestedQuestion) -> bool:
    ta, tb = _tokens(a.prompt_html), _tokens(b.prompt_html)
    if not ta or not tb:
        return ta == tb
    return len(ta & tb) / len(ta | tb) >= NEAR_DUPLICATE


def merge_unique(
    existing: list[SuggestedQuestion], new: list[SuggestedQuestion]
) -> tuple[list[SuggestedQuestion], int]:
    """Append ``new`` items that are not near-duplicates of anything kept. Returns the merged list
    and the number of duplicates removed."""
    merged = list(existing)
    removed = 0
    for item in new:
        if any(is_near_duplicate(item, kept) for kept in merged):
            removed += 1
        else:
            merged.append(item)
    return merged, removed


def parse_items(payload: dict) -> tuple[list[SuggestedQuestion], int]:
    """Valid suggestions from a provider payload and the number of invalid items dropped."""
    raw_items = payload.get("questions")
    if not isinstance(raw_items, list):
        return [], 0
    valid, dropped = [], 0
    for item in raw_items:
        suggestion = to_suggestion(item)
        if suggestion is None:
            dropped += 1
        else:
            valid.append(suggestion)
    return valid, dropped
