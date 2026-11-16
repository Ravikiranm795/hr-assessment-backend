"""Request and response models for the AI endpoints and the service interfaces (spec 7.2, 7.3)."""

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.core.config import settings

QuestionType = Literal["single_choice", "multi_choice", "fill_blank", "code_gap", "short_answer"]
Difficulty = Literal["easy", "medium", "hard"]
QUESTION_TYPES: tuple[str, ...] = ("single_choice", "multi_choice", "fill_blank", "code_gap", "short_answer")
DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")

MIN_QUESTIONS = 10  # S-03
MAX_QUESTIONS = 50
DEFAULT_QUESTIONS = 10

Skill = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class QuestionGenRequest(BaseModel):
    """Inputs of S-03. Required: role title, job description, experience level, key skills."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "role_title": "Backend Engineer",
                "job_description": "Build and operate Python APIs on PostgreSQL. Own services end to end.",
                "experience_level": "mid",
                "key_skills": ["Python", "SQL", "REST design"],
                "difficulty_mix": {"easy": 30, "medium": 50, "hard": 20},
                "question_type_mix": {"single_choice": 5, "multi_choice": 2, "short_answer": 3},
                "num_questions": 10,
                "domain": "fintech",
                "topics_to_avoid": ["trivia about framework history"],
                "role_explanation": "They keep our payment APIs fast and correct.",
            }
        },
    )

    role_title: str = Field(min_length=1, max_length=200)
    job_description: str = Field(min_length=1)
    experience_level: str = Field(min_length=1, max_length=60)
    key_skills: list[Skill] = Field(min_length=1, max_length=30)
    difficulty_mix: dict[Difficulty, int] | None = Field(
        default=None, description="Relative weights, for example easy 30, medium 50, hard 20."
    )
    question_type_mix: dict[QuestionType, int] | None = Field(
        default=None, description="Relative weights per supported question type."
    )
    num_questions: int = Field(default=DEFAULT_QUESTIONS, ge=MIN_QUESTIONS, le=MAX_QUESTIONS)
    domain: str | None = Field(default=None, max_length=200)
    topics_to_avoid: list[Skill] | None = Field(default=None, max_length=20)
    role_explanation: str | None = Field(default=None, max_length=2000)

    @field_validator("role_title", "experience_level", "job_description", "domain", "role_explanation")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("job_description")
    @classmethod
    def _jd_length(cls, value: str) -> str:
        if not value:
            raise ValueError("empty")
        if len(value) > settings.max_jd_chars:
            raise ValueError("too_long")
        return value

    @field_validator("difficulty_mix", "question_type_mix")
    @classmethod
    def _weights(cls, value: dict | None) -> dict | None:
        if value is None:
            return None
        if any(w < 0 or w > 1000 for w in value.values()) or sum(value.values()) <= 0:
            raise ValueError("invalid_weights")
        return value

    @model_validator(mode="after")
    def _defaults(self) -> "QuestionGenRequest":
        if self.domain == "":
            self.domain = None
        return self


class SuggestedOption(BaseModel):
    text_html: str
    is_correct: bool


class SuggestedQuestion(BaseModel):
    """One AI suggestion, shaped like a question body so the frontend can post the ticked ones to
    ``/questions/bulk``. ``temp_id`` is a throwaway id; nothing is stored."""

    temp_id: str
    type: QuestionType
    prompt_html: str
    marks: float
    scoring_mode: Literal["auto", "ai"]
    difficulty: Difficulty
    options: list[SuggestedOption] | None = None
    answer_key: dict | None = None
    model_answer: str | None = None
    code_language: str | None = None


class GenerationMeta(BaseModel):
    provider: str
    model: str
    requested: int
    returned: int
    dropped: int
    repaired: bool


class QuestionGenResponse(BaseModel):
    suggestions: list[SuggestedQuestion]
    meta: GenerationMeta
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "suggestions": [
                    {
                        "temp_id": "tmp_9f2c1a7e",
                        "type": "single_choice",
                        "prompt_html": "<p>Which HTTP status code means a resource was created?</p>",
                        "marks": 1.0,
                        "scoring_mode": "auto",
                        "difficulty": "easy",
                        "options": [
                            {"text_html": "200", "is_correct": False},
                            {"text_html": "201", "is_correct": True},
                        ],
                        "answer_key": None,
                        "model_answer": None,
                        "code_language": None,
                    }
                ],
                "meta": {
                    "provider": "anthropic",
                    "model": "claude-haiku-5-5",
                    "requested": 10,
                    "returned": 10,
                    "dropped": 1,
                    "repaired": False,
                },
            }
        }
    )


class QuestionTypeOption(BaseModel):
    key: QuestionType
    label_key: str
    default_scoring_mode: Literal["auto", "ai"]
    default_marks: float


class AiOptionsResponse(BaseModel):
    configured: bool
    provider: str | None
    model: str | None
    question_types: list[QuestionTypeOption]
    difficulties: list[Difficulty]
    defaults: dict[str, int | float]
    limits: dict[str, int]
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "configured": True,
                "provider": "anthropic",
                "model": "claude-haiku-5-5",
                "question_types": [
                    {
                        "key": "single_choice",
                        "label_key": "assessments.question_type.single_choice",
                        "default_scoring_mode": "auto",
                        "default_marks": 1.0,
                    }
                ],
                "difficulties": ["easy", "medium", "hard"],
                "defaults": {"num_questions": 10, "min_questions": 10, "max_questions": 50},
                "limits": {"max_jd_chars": 8000, "max_key_skills": 30},
            }
        }
    )


class ScoreResult(BaseModel):
    """Result of scoring one answer (spec 7.3). ``fraction`` is clamped to 0..1."""

    fraction: float = Field(ge=0.0, le=1.0)
    reason: str
    matched: list[str] = Field(default_factory=list)
    missed: list[str] = Field(default_factory=list)

    def to_marks(self, marks: float) -> float:
        """Marks awarded out of ``marks`` (two decimals)."""
        value = max(0.0, min(1.0, self.fraction)) * float(marks)
        return round(value, 2) if math.isfinite(value) else 0.0
