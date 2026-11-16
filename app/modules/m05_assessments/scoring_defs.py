"""Automatic scoring definitions (spec M5, "Scoring definitions") and the question view they run on.

``grade_auto(question, answer) -> Decimal`` is a pure function used by M9. ``question`` is any
object with ``type``, ``marks``, ``scoring_mode``, ``answer_key`` and ``options`` (each option with
``id`` and ``is_correct``); ``GradableQuestion`` (returned by ``get_published_version``) fits.

Candidate answer shapes (JSON stored in ``attempt_answers.answer``):

- ``single_choice``: ``{"option_id": "<uuid>"}``
- ``multi_choice``:  ``{"option_ids": ["<uuid>", ...]}``
- ``fill_blank``:    ``{"blanks": ["text", ...]}`` (one entry per blank, in order)
- ``code_gap``:      ``{"gaps": ["code", ...]}`` (one entry per gap, in order)
- ``short_answer``:  ``{"text": "..."}`` (never auto-graded)

Bare values (a string id, a list of ids, a list of strings) are accepted as well.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

CENT = Decimal("0.01")
_WHITESPACE = re.compile(r"\s+")

CHOICE_TYPES = ("single_choice", "multi_choice")


@dataclass(frozen=True)
class GradableOption:
    id: UUID
    text_html: str
    is_correct: bool
    position: int = 0


@dataclass(frozen=True)
class GradableQuestion:
    id: UUID
    position: int
    type: str
    prompt_html: str
    marks: Decimal
    scoring_mode: str
    answer_key: dict | list | None
    model_answer: str | None
    code_language: str | None
    options: list[GradableOption] = field(default_factory=list)


def _marks(value: Any) -> Decimal:
    return Decimal(str(value))


def money(value: Decimal) -> Decimal:
    """Two decimal places, half up (``questions.marks`` is ``numeric(6,2)``)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _key(question: Any) -> dict:
    key = getattr(question, "answer_key", None)
    return key if isinstance(key, dict) else {}


def _id_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str | UUID):
        return {str(value)}
    if isinstance(value, list | tuple | set):
        return {str(item) for item in value if item is not None}
    return set()


def _selected(answer: Any, *names: str) -> set[str]:
    if isinstance(answer, dict):
        for name in names:
            if name in answer:
                return _id_set(answer[name])
        return set()
    return _id_set(answer)


def _texts(answer: Any, name: str) -> list[str]:
    value = answer.get(name) if isinstance(answer, dict) else answer
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return ["" if item is None else str(item) for item in value]
    return []


def _normalize(value: str, *, case_sensitive: bool, collapse_whitespace: bool) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    if collapse_whitespace:
        value = _WHITESPACE.sub(" ", value)
    return value if case_sensitive else value.casefold()


def _grade_choice(question: Any, answer: Any, marks: Decimal) -> Decimal:
    options = list(getattr(question, "options", None) or [])
    known = {str(o.id) for o in options}
    correct = {str(o.id) for o in options if o.is_correct}
    if not correct:
        return Decimal("0.00")
    if question.type == "single_choice":
        picked = _selected(answer, "option_id", "option_ids")
        return money(marks) if len(correct) == 1 and picked == correct else Decimal("0.00")
    picked = _selected(answer, "option_ids", "option_id") & known
    if picked == correct:
        return money(marks)
    if not _key(question).get("partial"):
        return Decimal("0.00")
    # Partial credit: each correct pick earns 1/n, each wrong pick costs 1/n, never below zero.
    net = len(picked & correct) - len(picked - correct)
    if net <= 0:
        return Decimal("0.00")
    return money(marks * Decimal(net) / Decimal(len(correct)))


def _grade_slots(
    slots: list[dict], given: list[str], marks: Decimal, *, case_sensitive: bool, collapse_whitespace: bool
) -> Decimal:
    """Marks split evenly across the blanks/gaps; a slot is right if any accepted answer matches."""
    if not slots:
        return Decimal("0.00")
    right = 0
    for index, slot in enumerate(slots):
        if index >= len(given):
            continue
        sensitive = bool(slot.get("case_sensitive", case_sensitive))
        wanted = {
            _normalize(str(a), case_sensitive=sensitive, collapse_whitespace=collapse_whitespace)
            for a in slot.get("accepted", [])
            if str(a).strip()
        }
        got = _normalize(given[index], case_sensitive=sensitive, collapse_whitespace=collapse_whitespace)
        if got and got in wanted:
            right += 1
    return money(marks * Decimal(right) / Decimal(len(slots)))


def grade_auto(question: Any, answer: Any) -> Decimal:
    """Auto marks for one answer, 0.00 to ``question.marks``. Questions whose scoring mode is not
    ``auto`` (AI or manual) and short answers have no auto marks."""
    marks = _marks(question.marks)
    if getattr(question, "scoring_mode", "auto") != "auto" or answer is None:
        return Decimal("0.00")
    qtype = question.type
    key = _key(question)
    if qtype in CHOICE_TYPES:
        return _grade_choice(question, answer, marks)
    if qtype == "fill_blank":
        return _grade_slots(
            key.get("blanks", []),
            _texts(answer, "blanks"),
            marks,
            case_sensitive=bool(key.get("case_sensitive", False)),
            collapse_whitespace=False,
        )
    if qtype == "code_gap":
        return _grade_slots(
            key.get("gaps", []),
            _texts(answer, "gaps"),
            marks,
            case_sensitive=bool(key.get("case_sensitive", True)),
            collapse_whitespace=True,
        )
    return Decimal("0.00")
