"""M14 AI endpoints under ``/assessments/ai`` (spec 6/M5, 7.2). Roles: HR and OA. Flag: ``ai_question_builder``."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.config import settings
from app.core.db import TenantContext
from app.core.deps import DbSession, require_feature, require_role
from app.core.flags import AI_QUESTION_BUILDER
from app.core.roles import HR_USER, ORG_ADMIN
from app.modules.m14_ai import catalog  # noqa: F401  (keeps the catalog import path warm)
from app.modules.m14_ai.schemas import (
    DEFAULT_QUESTIONS,
    DIFFICULTIES,
    MAX_QUESTIONS,
    MIN_QUESTIONS,
    AiOptionsResponse,
    QuestionGenRequest,
    QuestionGenResponse,
    QuestionTypeOption,
)
from app.modules.m14_ai.service import AiService

router = APIRouter(
    prefix="/assessments/ai",
    tags=["ai"],
    dependencies=[Depends(require_feature(AI_QUESTION_BUILDER))],
)

Staff = Annotated[TenantContext, Depends(require_role(ORG_ADMIN, HR_USER))]


def _error(code: str, message_key: str, description: str, details: dict | None = None) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "example": {
                    "error": {
                        "code": code,
                        "message_key": message_key,
                        "details": details,
                        "request_id": "7c1f2f0a-0d3c-4a55-9b0e-2f2a5c2f1a11",
                    }
                }
            }
        },
    }


_COMMON = {
    401: _error(
        "UNAUTHENTICATED", "errors.auth.unauthenticated", "Missing, invalid or expired access token."
    ),
    403: _error(
        "FORBIDDEN",
        "errors.common.forbidden",
        "Role not allowed, or the feature flag is off (FEATURE_DISABLED).",
    ),
}


@router.get(
    "/options",
    response_model=AiOptionsResponse,
    summary="AI form options",
    description=(
        "Supported question types, difficulty levels, defaults and limits for the AI question form, plus whether "
        "AI is configured for the organization. Roles: HR, OA. Flag: `ai_question_builder`."
    ),
    responses=_COMMON,
)
async def ai_options(ctx: Staff, session: DbSession) -> AiOptionsResponse:
    configured, provider, model = await AiService(session).describe(ctx)
    types = [
        QuestionTypeOption(
            key=key,
            label_key=f"assessments.question_type.{key}",
            default_scoring_mode="ai" if key == "short_answer" else "auto",
            default_marks=5.0
            if key == "short_answer"
            else (2.0 if key in ("fill_blank", "code_gap") else 1.0),
        )
        for key in ("single_choice", "multi_choice", "fill_blank", "code_gap", "short_answer")
    ]
    return AiOptionsResponse(
        configured=configured,
        provider=provider,
        model=model,
        question_types=types,
        difficulties=list(DIFFICULTIES),  # type: ignore[arg-type]
        defaults={
            "num_questions": DEFAULT_QUESTIONS,
            "min_questions": MIN_QUESTIONS,
            "max_questions": MAX_QUESTIONS,
        },
        limits={"max_jd_chars": settings.max_jd_chars, "max_key_skills": 30, "max_topics_to_avoid": 20},
    )


@router.post(
    "/generate",
    response_model=QuestionGenResponse,
    summary="Generate question suggestions with AI",
    description=(
        "Stateless: returns a tick-list of at least 10 suggested questions built from the role, job description, "
        "level and key skills. Nothing is stored; post the ticked items to `/questions/bulk`. Uses the organization's "
        "own AI provider (bring your own key). Limits: 5 requests per minute per user, optional monthly call cap, "
        "synchronous with a 90 second hard timeout. Audited as `AI_QUESTIONS_GENERATED` with counts, provider and "
        "model only. Roles: HR, OA. Flag: `ai_question_builder`."
    ),
    responses={
        **_COMMON,
        409: _error(
            "AI_NOT_CONFIGURED",
            "errors.ai.not_configured",
            "No usable AI provider is configured for the organization.",
        ),
        422: _error(
            "VALIDATION_ERROR",
            "errors.common.validation",
            "Invalid input (for example fewer than 10 questions or a job description over MAX_JD_CHARS).",
            {"fields": [{"field": "body.num_questions", "code": "greater_than_equal"}]},
        ),
        429: _error(
            "AI_LIMIT_REACHED",
            "errors.ai.limit_reached",
            "Monthly AI call cap reached, or RATE_LIMITED (5 per minute per user).",
            {"monthly_call_cap": 500},
        ),
        502: _error(
            "AI_PROVIDER_ERROR",
            "errors.ai.provider_error",
            "The provider failed, timed out, or returned output that is not valid JSON (AI_OUTPUT_INVALID).",
        ),
    },
)
async def generate(body: QuestionGenRequest, ctx: Staff, session: DbSession) -> QuestionGenResponse:
    return await AiService(session).generate(ctx, body)
