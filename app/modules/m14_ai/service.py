"""AI service (spec 7.1-7.3): question generation and answer scoring.

Every call: checks the org's AI settings (``AI_NOT_CONFIGURED``), the optional monthly cap
(``AI_LIMIT_REACHED``), and for generation the per-user rate limit (``RATE_LIMITED``); then calls
the provider, writes ``ai_usage_log`` and an audit entry with metadata only (D-10): provider,
model, feature and counts, never the prompt, the job description or the output.
"""

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, flags
from app.core.ai import LLMProvider
from app.core.config import settings
from app.core.db import TenantContext, tenant_session
from app.core.errors import AppError
from app.core.ratelimit import check_rate_limit
from app.modules.m14_ai import catalog, repo
from app.modules.m14_ai.prompts import question_gen_v1, score_v1
from app.modules.m14_ai.providers import get_provider
from app.modules.m14_ai.providers.base import Usage
from app.modules.m14_ai.schemas import (
    DEFAULT_QUESTIONS,
    MAX_QUESTIONS,
    MIN_QUESTIONS,
    GenerationMeta,
    QuestionGenRequest,
    QuestionGenResponse,
    ScoreResult,
    SuggestedQuestion,
)
from app.modules.m14_ai.suggestions import merge_unique, parse_items

log = logging.getLogger("app.ai")

GENERATE_TIMEOUT_SEC = 90  # spec 7.2: synchronous call with a hard timeout
FEATURE_QUESTION_GEN = "question_gen"
FEATURE_SCORING = "scoring"

_HTML_TAG = re.compile(r"<[^>]+>")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{6,}\d(?![\w])")
MAX_ANSWER_CHARS = 8000
MAX_REASON_CHARS = 1000
MAX_POINTS = 12


def _month_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def strip_personal_data(text: str, redact_terms: tuple[str, ...] = ()) -> str:
    """Best-effort removal of emails, phone numbers and given names before text leaves the system."""
    cleaned = _EMAIL.sub("[email]", text)
    cleaned = _PHONE.sub("[phone]", cleaned)
    for term in redact_terms:
        if term and len(term.strip()) >= 2:
            cleaned = re.sub(re.escape(term.strip()), "[redacted]", cleaned, flags=re.IGNORECASE)
    return cleaned


def html_to_text(value: str) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG.sub(" ", value)).strip()


class _Prepared:
    def __init__(self, row: Any, provider: LLMProvider):
        self.row = row
        self.provider = provider
        self.provider_name: str = row.provider
        self.model: str = row.model or catalog.default_model(row.provider) or ""


class AiService:
    """Use inside a request or job transaction: ``AiService(session)``. ``ctx.org_id`` is required
    (super admins have no org-level AI settings)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ------------------------------------------------------------------ shared checks

    async def _prepare(self, ctx: TenantContext) -> _Prepared:
        if ctx.org_id is None:
            raise AppError("AI_NOT_CONFIGURED")
        row = await repo.get_ai_settings(self.session, ctx.org_id)
        if row is None:
            raise AppError("AI_NOT_CONFIGURED")
        provider = get_provider(row)  # raises AI_NOT_CONFIGURED for a missing key, bad CLI key, ...
        cap = row.monthly_call_cap
        if cap is not None and await repo.count_calls_since(self.session, ctx.org_id, _month_start()) >= cap:
            raise AppError("AI_LIMIT_REACHED", details={"monthly_call_cap": cap})
        return _Prepared(row, provider)

    async def describe(self, ctx: TenantContext) -> tuple[bool, str | None, str | None]:
        """``(configured, provider, model)`` for the options endpoint; never exposes secrets."""
        if ctx.org_id is None:
            return False, None, None
        row = await repo.get_ai_settings(self.session, ctx.org_id)
        if row is None:
            return False, None, None
        usable = bool(row.api_key_enc) or row.mode == "cli" or row.provider == "openai_compatible"
        return usable, row.provider, row.model or catalog.default_model(row.provider)

    async def _log_failure(
        self, ctx: TenantContext, prepared: _Prepared, feature: str, action: str, error: AppError, extra: dict
    ) -> None:
        """Failures roll back the request transaction, so they are recorded in their own one."""
        status = "output_invalid" if error.code == "AI_OUTPUT_INVALID" else "provider_error"
        try:
            async with tenant_session(ctx) as session:
                await repo.add_usage(
                    session,
                    org_id=ctx.org_id,  # type: ignore[arg-type]
                    user_id=ctx.user_id,
                    feature=feature,
                    provider=prepared.provider_name,
                    model=prepared.model,
                    status=status,
                    tokens_in=None,
                    tokens_out=None,
                )
                await audit.record(
                    ctx,
                    session,
                    action,
                    "ai_call",
                    ctx.org_id,
                    after={
                        "feature": feature,
                        "provider": prepared.provider_name,
                        "model": prepared.model,
                        **extra,
                    },
                    reason=error.code,
                    result="failure",
                )
        except Exception:  # never mask the original AI error
            log.exception("could not record failed ai call")

    async def _record_success(
        self,
        ctx: TenantContext,
        prepared: _Prepared,
        feature: str,
        action: str,
        entity_id: Any,
        usage: Usage | None,
        counts: dict,
    ) -> None:
        await repo.add_usage(
            self.session,
            org_id=ctx.org_id,  # type: ignore[arg-type]
            user_id=ctx.user_id,
            feature=feature,
            provider=prepared.provider_name,
            model=prepared.model,
            status="success",
            tokens_in=usage.tokens_in if usage else None,
            tokens_out=usage.tokens_out if usage else None,
        )
        await audit.record(
            ctx,
            self.session,
            action,
            "ai_call",
            entity_id if entity_id is not None else ctx.org_id,
            after={"feature": feature, "provider": prepared.provider_name, "model": prepared.model, **counts},
        )

    # ------------------------------------------------------------------ question generation

    async def generate(self, ctx: TenantContext, req: QuestionGenRequest) -> QuestionGenResponse:
        """Stateless suggestions (spec 7.2). Nothing is stored except usage and audit metadata."""
        prepared = await self._prepare(ctx)
        if ctx.user_id is not None:
            check_rate_limit(settings.rate_limit_ai_generate, "ai_generate", "user", str(ctx.user_id))
        count = max(MIN_QUESTIONS, min(req.num_questions or DEFAULT_QUESTIONS, MAX_QUESTIONS))
        system, prompt = question_gen_v1.build_prompt(req, count=count)
        max_tokens = min(16000, 700 * count + 1000)
        started = time.monotonic()
        usage = Usage()
        calls = 0

        async def ask(text: str, timeout: int) -> dict | None:  # noqa: ASYNC109 (provider timeout, seconds)
            nonlocal calls
            calls += 1
            try:
                return await prepared.provider.generate_json(
                    system=system,
                    prompt=text,
                    json_schema=question_gen_v1.OUTPUT_SCHEMA,
                    max_tokens=max_tokens,
                    timeout_sec=timeout,
                )
            except AppError as exc:
                if exc.code != "AI_OUTPUT_INVALID":
                    raise
                return None
            finally:
                last = getattr(prepared.provider, "last_usage", None)
                if last is not None:
                    usage.tokens_in = (usage.tokens_in or 0) + (last.tokens_in or 0)
                    usage.tokens_out = (usage.tokens_out or 0) + (last.tokens_out or 0)

        suggestions: list[SuggestedQuestion] = []
        dropped = 0
        repaired = False
        try:
            payload = await ask(prompt, GENERATE_TIMEOUT_SEC)
            if payload is not None:
                suggestions, dropped = parse_items(payload)
                suggestions, dupes = merge_unique([], suggestions)
                dropped += dupes
            if len(suggestions) < count:
                # One repair retry: the first reply was invalid or too short.
                repaired = True
                remaining = max(15, GENERATE_TIMEOUT_SEC - int(time.monotonic() - started))
                retry = await ask(
                    prompt + question_gen_v1.repair_addendum(count=count, usable=len(suggestions)), remaining
                )
                if retry is not None:
                    more, more_dropped = parse_items(retry)
                    suggestions, dupes = merge_unique(suggestions, more)
                    dropped += more_dropped + dupes
            if not suggestions:
                raise AppError("AI_OUTPUT_INVALID", details={"reason": "no_valid_questions"})
        except AppError as exc:
            await self._log_failure(
                ctx,
                prepared,
                FEATURE_QUESTION_GEN,
                audit.AI_QUESTIONS_GENERATED,
                exc,
                {"requested": count, "calls": calls},
            )
            raise
        meta = GenerationMeta(
            provider=prepared.provider_name,
            model=prepared.model,
            requested=count,
            returned=len(suggestions),
            dropped=dropped,
            repaired=repaired,
        )
        await self._record_success(
            ctx,
            prepared,
            FEATURE_QUESTION_GEN,
            audit.AI_QUESTIONS_GENERATED,
            None,
            usage,
            {
                "requested": count,
                "returned": len(suggestions),
                "dropped": dropped,
                "repaired": repaired,
                "calls": calls,
            },
        )
        return QuestionGenResponse(suggestions=suggestions, meta=meta)

    async def generate_questions(
        self, ctx: TenantContext, req: QuestionGenRequest
    ) -> list[SuggestedQuestion]:
        return (await self.generate(ctx, req)).suggestions

    # ------------------------------------------------------------------ scoring

    async def score_answer(
        self,
        ctx: TenantContext,
        question: str,
        model_answer: str,
        marks: float,
        answer_text: str,
        *,
        entity_id: UUID | str | None = None,
        redact_terms: tuple[str, ...] = (),
    ) -> ScoreResult:
        """Score one answer (spec 7.3). Sends only the question text, the model answer or key
        points, the marks and the answer text; emails, phone numbers and ``redact_terms`` (for
        example the candidate's name) are removed from the text first.

        Raises ``AppError`` (``FEATURE_DISABLED``, ``AI_NOT_CONFIGURED``, ``AI_LIMIT_REACHED``,
        ``AI_PROVIDER_ERROR``, ``AI_OUTPUT_INVALID``); the caller sets ``needs_manual``.
        ``entity_id`` (for example the attempt answer id) is stored in the audit row only.
        """
        if ctx.org_id is not None and not await flags.is_enabled(self.session, ctx.org_id, flags.AI_SCORING):
            raise AppError("FEATURE_DISABLED", 403, {"flag": flags.AI_SCORING})
        prepared = await self._prepare(ctx)
        system, prompt = score_v1.build_prompt(
            question=strip_personal_data(html_to_text(question), redact_terms),
            model_answer=strip_personal_data(html_to_text(model_answer), redact_terms),
            marks=float(marks),
            answer_text=strip_personal_data(answer_text[:MAX_ANSWER_CHARS], redact_terms),
        )
        try:
            payload = await prepared.provider.generate_json(
                system=system,
                prompt=prompt,
                json_schema=score_v1.OUTPUT_SCHEMA,
                max_tokens=1500,
                timeout_sec=settings.ai_request_timeout_sec,
            )
            result = self._to_result(payload)
        except AppError as exc:
            await self._log_failure(ctx, prepared, FEATURE_SCORING, audit.AI_SCORING_RUN, exc, {"answers": 1})
            raise
        await self._record_success(
            ctx,
            prepared,
            FEATURE_SCORING,
            audit.AI_SCORING_RUN,
            entity_id,
            getattr(prepared.provider, "last_usage", None),
            {"answers": 1},
        )
        return result

    @staticmethod
    def _to_result(payload: dict) -> ScoreResult:
        fraction = payload.get("fraction")
        if isinstance(fraction, bool) or not isinstance(fraction, int | float) or fraction != fraction:
            raise AppError("AI_OUTPUT_INVALID", details={"reason": "fraction"})
        reason = payload.get("reason")
        reason = reason.strip()[:MAX_REASON_CHARS] if isinstance(reason, str) else ""

        def points(value: Any) -> list[str]:
            if not isinstance(value, list):
                return []
            return [p.strip()[:300] for p in value if isinstance(p, str) and p.strip()][:MAX_POINTS]

        return ScoreResult(
            fraction=max(0.0, min(1.0, float(fraction))),
            reason=reason,
            matched=points(payload.get("matched")),
            missed=points(payload.get("missed")),
        )
