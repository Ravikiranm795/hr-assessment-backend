"""Question validation: what must hold to save a question, and what must hold to publish a version."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.core.errors import AppError
from app.modules.m05_assessments.sanitize import plain_text, sanitize_html
from app.modules.m05_assessments.schemas import QuestionIn

ALLOWED_SCORING = {
    "single_choice": {"auto"},
    "multi_choice": {"auto"},
    "fill_blank": {"auto"},
    "code_gap": {"auto", "ai", "manual"},
    "short_answer": {"ai", "manual"},
}
DEFAULT_SCORING = {
    "single_choice": "auto",
    "multi_choice": "auto",
    "fill_blank": "auto",
    "code_gap": "auto",
    "short_answer": "manual",
}
CHOICE = {"single_choice", "multi_choice"}

Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class _Slot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: list[Text] = Field(default_factory=list, max_length=20)
    case_sensitive: bool | None = None


class _MultiKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partial: bool = False


class _FillKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blanks: list[_Slot] = Field(default_factory=list, max_length=50)
    case_sensitive: bool = False


class _GapKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gaps: list[_Slot] = Field(default_factory=list, max_length=50)
    case_sensitive: bool = True
    key_points: list[Text] = Field(default_factory=list, max_length=20)


class _ShortKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_points: list[Text] = Field(default_factory=list, max_length=20)


_KEY_MODELS: dict[str, type[BaseModel]] = {
    "multi_choice": _MultiKey,
    "fill_blank": _FillKey,
    "code_gap": _GapKey,
    "short_answer": _ShortKey,
}


@dataclass
class NormalizedQuestion:
    type: str
    prompt_html: str
    marks: Decimal
    scoring_mode: str
    answer_key: dict | None
    model_answer: str | None
    code_language: str | None
    options: list[tuple[str, bool]] = field(default_factory=list)


def _invalid(field_name: str, code: str) -> AppError:
    return AppError("QUESTION_INVALID", details={"fields": [{"field": field_name, "code": code}]})


def _key_points(key: dict | None) -> list[str]:
    return [p for p in (key or {}).get("key_points", []) if str(p).strip()]


def normalize_question(body: QuestionIn) -> NormalizedQuestion:
    """Sanitize and normalize a question for saving. Structural problems are ``QUESTION_INVALID``;
    an AI-scored question with neither a model answer nor key points is ``MODEL_ANSWER_REQUIRED``
    (S-10). Completeness (enough options, a correct one, blanks with answers) is a publish rule."""
    qtype = body.type
    scoring = body.scoring_mode or DEFAULT_SCORING[qtype]
    if scoring not in ALLOWED_SCORING[qtype]:
        raise _invalid("scoring_mode", "not_allowed_for_type")
    prompt = sanitize_html(body.prompt_html) or ""
    if not plain_text(prompt):
        raise _invalid("prompt_html", "empty")
    if qtype in CHOICE:
        options = []
        for index, option in enumerate(body.options):
            text = sanitize_html(option.text_html) or ""
            if not plain_text(text):
                raise _invalid(f"options.{index}.text_html", "empty")
            options.append((text, option.is_correct))
    elif body.options:
        raise _invalid("options", "not_allowed_for_type")
    else:
        options = []

    key: dict | None = None
    if qtype in _KEY_MODELS:
        try:
            key = _KEY_MODELS[qtype].model_validate(body.answer_key or {}).model_dump(exclude_none=True)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise _invalid("answer_key." + ".".join(str(p) for p in first["loc"]), first["type"]) from exc
        for slots in ("blanks", "gaps"):
            for slot in key.get(slots, []):
                slot["accepted"] = [a for a in slot["accepted"] if a]
    elif body.answer_key:
        raise _invalid("answer_key", "not_allowed_for_type")

    model_answer = (body.model_answer or "").strip() or None
    if scoring == "ai" and not model_answer and not _key_points(key):
        raise AppError(
            "MODEL_ANSWER_REQUIRED", details={"fields": [{"field": "model_answer", "code": "required"}]}
        )
    language = (body.code_language or "").strip() or None
    return NormalizedQuestion(
        type=qtype,
        prompt_html=prompt,
        marks=body.marks,
        scoring_mode=scoring,
        answer_key=key,
        model_answer=model_answer,
        code_language=language,
        options=options,
    )


# --------------------------------------------------------------------------- publish rules


@dataclass(frozen=True)
class PublishView:
    """Just the fields the publish rules read (so they work on ORM rows and on test doubles)."""

    id: Any
    position: int
    type: str
    marks: Decimal
    scoring_mode: str
    answer_key: dict | list | None
    model_answer: str | None
    option_flags: list[bool]


def question_issues(q: PublishView) -> list[str]:
    """Issue codes that stop a question from being published (spec M5, "Publish validations")."""
    issues: list[str] = []
    if q.marks is None or Decimal(str(q.marks)) <= 0:
        issues.append("marks_not_positive")
    key = q.answer_key if isinstance(q.answer_key, dict) else {}
    if q.type in CHOICE:
        if len(q.option_flags) < 2:
            issues.append("too_few_options")
        correct = sum(q.option_flags)
        if correct == 0:
            issues.append("no_correct_option")
        elif q.type == "single_choice" and correct > 1:
            issues.append("multiple_correct_options")
    elif q.type == "fill_blank" or (q.type == "code_gap" and q.scoring_mode == "auto"):
        slots = key.get("blanks" if q.type == "fill_blank" else "gaps", [])
        if not slots:
            issues.append("no_blanks")
        elif any(not [a for a in s.get("accepted", []) if str(a).strip()] for s in slots):
            issues.append("blank_without_answers")
    if q.scoring_mode == "ai" and not (q.model_answer or "").strip() and not _key_points(key):
        issues.append("model_answer_required")
    return issues


def publish_issues(questions: list[PublishView]) -> list[dict]:
    if not questions:
        return [{"question_id": None, "position": None, "code": "no_questions"}]
    return [
        {"question_id": str(q.id), "position": q.position, "code": code}
        for q in questions
        for code in question_issues(q)
    ]
