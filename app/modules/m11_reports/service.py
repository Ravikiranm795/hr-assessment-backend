"""M11 report chat service (spec 6/M11, 7.4).

interpret -> select -> preview-count -> generate -> download. The AI receives the field catalog and the
admin's words, returns FilterSpecs, and never sees a row. Every spec is validated against the catalog,
compiled to a parameterized query with ``org_id`` forced, and run on the read-only role; the counts shown
at confirmation come from the database. Audit rows store the typed text and the spec, never rows.
Super admin reports use the same pipeline over the platform sources only (no candidate content).
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import settings
from app.core.db import TenantContext, tenant_session
from app.core.errors import AppError
from app.core.pagination import Page, PageParams
from app.core.storage import get_storage
from app.db import ReportRequest
from app.modules.m11_reports import executor, exporters, prompt, repo
from app.modules.m11_reports.compiler import compile_spec
from app.modules.m11_reports.filterspec import FilterSpec, invalid, validate_spec
from app.modules.m11_reports.registry import catalog_json, sources_for
from app.modules.m11_reports.schemas import (
    CatalogOut,
    ChatOut,
    CountOut,
    GenerateIn,
    InterpretationOut,
    InterpretIn,
    ReportOut,
    SelectIn,
)
from app.modules.m14_ai.providers import get_provider

log = logging.getLogger("app.reports")

# report_requests.source only allows the five org sources (frozen schema); the real source is in filter_spec.
_DB_SOURCE = {"organizations": "team", "platform_audit": "audit"}
MAX_TEXT_STORED = 2000
# The frozen assets.kind list has no "report" kind. Files are encrypted and removed after 7 days.
ASSET_KIND = "doc_pdf"


def _db_source(name: str) -> str:
    return _DB_SOURCE.get(name, name)


def _month_start() -> datetime:
    return datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _out(row: ReportRequest) -> ReportOut:
    confirmed = row.status != "interpreted" and bool(row.filter_spec)
    return ReportOut(
        id=row.id,
        source=(row.filter_spec or {}).get("source") if confirmed else row.source,
        user_text=row.user_text,
        status=row.status,
        filter_spec=row.filter_spec if confirmed else None,
        columns=row.columns,
        format=row.format if row.status == "generated" else None,
        row_count=row.row_count,
        file_available=row.asset_id is not None,
        created_by=row.user_id,
        created_at=row.created_at,
    )


def _stored_interpretations(row: ReportRequest) -> list[dict]:
    return (row.filter_spec or {}).get("interpretations", []) if row.status == "interpreted" else []


class ReportService:
    @staticmethod
    def catalog(ctx: TenantContext) -> CatalogOut:
        return CatalogOut(sources=catalog_json(ctx.is_platform))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ counting

    @staticmethod
    async def _estimate(ctx: TenantContext, spec: FilterSpec) -> int | None:
        """Row count from the database (capped by the spec's limit), never from the AI."""
        compiled = compile_spec(spec, ctx.org_id, platform=ctx.is_platform)
        try:
            return min(await executor.count_rows(ctx, compiled), spec.limit)
        except AppError:
            return None  # too slow to count: the interpretation is still usable

    # ------------------------------------------------------------------ AI

    @staticmethod
    async def _ask_ai(
        ctx: TenantContext,
        session: AsyncSession,
        text: str,
        source: str | None,
        refine: str | None,
    ) -> dict:
        if ctx.org_id is None:  # no platform-provided AI: super admins send a structured filter instead
            raise AppError("AI_NOT_CONFIGURED", details={"reason": "platform_ai_unavailable"})
        row = await repo.get_ai_settings(session, ctx.org_id)
        if row is None:
            raise AppError("AI_NOT_CONFIGURED")
        provider = get_provider(row)
        cap = row.monthly_call_cap
        if cap is not None and await repo.count_ai_calls_since(session, ctx.org_id, _month_start()) >= cap:
            raise AppError("AI_LIMIT_REACHED", details={"monthly_call_cap": cap})
        system, user_prompt = prompt.build_prompt(catalog_json(False), text, source=source, refine=refine)
        usage = {"org_id": ctx.org_id, "user_id": ctx.user_id, "provider": row.provider, "model": row.model}
        try:
            payload = await provider.generate_json(
                system=system,
                prompt=user_prompt,
                json_schema=prompt.OUTPUT_SCHEMA,
                max_tokens=3000,
                timeout_sec=settings.ai_request_timeout_sec,
            )
        except AppError as exc:
            await ReportService._log_failed_call(ctx, usage, exc)
            raise
        await repo.add_ai_usage(session, status="success", **usage)
        if not isinstance(payload, dict):
            raise AppError("AI_OUTPUT_INVALID", details={"reason": "not_an_object"})
        return payload

    @staticmethod
    async def _log_failed_call(ctx: TenantContext, usage: dict, error: AppError) -> None:
        """A failed call rolls the request transaction back, so its usage row goes in its own one."""
        status = "output_invalid" if error.code == "AI_OUTPUT_INVALID" else "provider_error"
        try:
            async with tenant_session(ctx) as session:
                await repo.add_ai_usage(session, status=status, **usage)
        except Exception:  # never mask the original AI error
            log.exception("could not record failed report-chat call")

    @staticmethod
    def _parse_interpretations(
        payload: dict, ctx: TenantContext, source: str | None
    ) -> tuple[list[tuple[str, FilterSpec]], str | None]:
        """Catalog-validated interpretations from the AI output; invalid ones are dropped. Raises
        ``REPORT_FILTER_INVALID`` when the AI proposed only invalid specs, ``AI_OUTPUT_INVALID`` when it
        proposed nothing."""
        items = payload.get("interpretations")
        question = payload.get("clarifying_question")
        question = question.strip()[:300] if isinstance(question, str) and question.strip() else None
        valid: list[tuple[str, FilterSpec]] = []
        first_error: AppError | None = None
        seen: set[str] = set()
        for item in (items if isinstance(items, list) else [])[: prompt.MAX_INTERPRETATIONS * 2]:
            if not isinstance(item, dict) or not isinstance(item.get("spec"), dict):
                continue
            try:
                spec = validate_spec(item["spec"], platform=ctx.is_platform)
                if source and spec.source != source:
                    raise invalid("source", "unexpected_source", allowed=[source])
            except AppError as exc:
                first_error = first_error or exc
                continue
            key = spec.model_dump_json()
            label = item.get("label")
            if key in seen:
                continue
            seen.add(key)
            text = label.strip()[:200] if isinstance(label, str) and label.strip() else spec.source
            valid.append((text, spec))
        valid = valid[: prompt.MAX_INTERPRETATIONS]
        if valid:
            return valid, None
        if question:
            return [], question
        if first_error:
            raise first_error
        raise AppError("AI_OUTPUT_INVALID", details={"reason": "no_interpretation"})

    # ------------------------------------------------------------------ interpret / select

    @staticmethod
    async def _interpret(
        ctx: TenantContext,
        session: AsyncSession,
        row: ReportRequest | None,
        text: str,
        source: str | None,
        filter_spec: dict | None,
        refine: str | None = None,
    ) -> ChatOut:
        allowed = sources_for(ctx.is_platform)
        if source is not None and source not in allowed:
            raise invalid("source", "unknown_source", allowed=sorted(allowed))
        if filter_spec is not None:
            spec = validate_spec(filter_spec, platform=ctx.is_platform)
            if source and spec.source != source:
                raise invalid("source", "unexpected_source", allowed=[source])
            found, question, used_ai = [("Structured filter", spec)], None, False
        else:
            payload = await ReportService._ask_ai(ctx, session, text, source, refine)
            found, question = ReportService._parse_interpretations(payload, ctx, source)
            used_ai = True

        stored = [{"label": label, "filter_spec": spec.model_dump(mode="json")} for label, spec in found]
        shown_text = (text if not refine else f"{text} | {refine}")[:MAX_TEXT_STORED]
        first_source = found[0][1].source if found else (source or next(iter(allowed)))
        if row is None:
            row = await repo.add_request(
                session,
                org_id=ctx.org_id,
                user_id=ctx.user_id,
                source=_db_source(first_source),
                user_text=shown_text,
                filter_spec={"interpretations": stored},
                status="interpreted",
            )
        else:
            row.user_text, row.source = shown_text, _db_source(first_source)
            row.filter_spec = {"interpretations": stored}
        await audit.record(
            ctx,
            session,
            audit.REPORT_INTERPRETED,
            "report_request",
            row.id,
            after={
                "user_text": shown_text,
                "source": first_source,
                "ai": used_ai,
                "interpretations": [i["filter_spec"] for i in stored],
                "clarifying_question": bool(question),
            },
        )
        out = []
        for i, (label, spec) in enumerate(found):
            out.append(
                InterpretationOut(
                    index=i,
                    label=label,
                    filter_spec=spec.model_dump(mode="json"),
                    columns=spec.columns,
                    estimated_rows=await ReportService._estimate(ctx, spec),
                )
            )
        return ChatOut(report=_out(row), interpretations=out, clarifying_question=question)

    @staticmethod
    async def interpret(ctx: TenantContext, session: AsyncSession, body: InterpretIn) -> ChatOut:
        return await ReportService._interpret(
            ctx, session, None, (body.text or "").strip(), body.source, body.filter_spec
        )

    @staticmethod
    async def select(ctx: TenantContext, session: AsyncSession, body: SelectIn) -> ChatOut:
        row = await ReportService._get(ctx, session, body.id)
        if row.status != "interpreted":
            raise AppError("CONFLICT", details={"reason": "already_confirmed"})
        if body.text is not None:  # refine: same request, new interpretations
            return await ReportService._interpret(
                ctx, session, row, row.user_text or "", None, None, refine=body.text.strip()
            )
        stored = _stored_interpretations(row)
        if body.index is None or body.index >= len(stored):
            raise AppError(
                "VALIDATION_ERROR", details={"fields": [{"field": "index", "code": "no_such_interpretation"}]}
            )
        spec = validate_spec(stored[body.index]["filter_spec"], platform=ctx.is_platform)
        row.source, row.columns = _db_source(spec.source), spec.columns
        row.filter_spec, row.status = spec.model_dump(mode="json"), "confirmed"
        estimate = await ReportService._estimate(ctx, spec)
        return ChatOut(report=_out(row), interpretations=[], estimated_rows=estimate)

    # ------------------------------------------------------------------ count / generate / download

    @staticmethod
    async def _get(ctx: TenantContext, session: AsyncSession, report_id: uuid.UUID) -> ReportRequest:
        row = await repo.get_request(session, ctx, report_id)
        if row is None:
            raise AppError("NOT_FOUND")  # other organizations' ids look exactly like missing ones
        return row

    @staticmethod
    def _confirmed(ctx: TenantContext, row: ReportRequest) -> FilterSpec:
        if row.status not in ("confirmed", "generated") or not row.filter_spec:
            raise AppError("CONFLICT", details={"reason": "not_confirmed"})
        return validate_spec(row.filter_spec, platform=ctx.is_platform)

    @staticmethod
    async def preview_count(ctx: TenantContext, session: AsyncSession, report_id: uuid.UUID) -> CountOut:
        row = await ReportService._get(ctx, session, report_id)
        spec = ReportService._confirmed(ctx, row)
        compiled = compile_spec(spec, ctx.org_id, platform=ctx.is_platform)
        return CountOut(id=row.id, row_count=min(await executor.count_rows(ctx, compiled), spec.limit))

    @staticmethod
    async def generate(
        ctx: TenantContext, session: AsyncSession, report_id: uuid.UUID, body: GenerateIn
    ) -> ReportOut:
        row = await ReportService._get(ctx, session, report_id)
        spec = ReportService._confirmed(ctx, row)
        if body.columns is not None and not spec.aggregate:
            spec = validate_spec(spec.model_copy(update={"columns": body.columns}), platform=ctx.is_platform)
            if not spec.columns:
                raise invalid("columns", "required")
        cap = exporters.CAPS[body.format]
        compiled = compile_spec(spec, ctx.org_id, row_cap=cap, platform=ctx.is_platform)
        rows = await executor.run_readonly(ctx, compiled.sql, compiled.params)
        if len(rows) > cap:
            raise AppError(
                "REPORT_FILTER_INVALID",
                details={"fields": [{"field": "limit", "code": "row_cap_exceeded", "max": cap}]},
            )
        executor.decrypt_pii(rows, compiled.decrypt_columns)
        title = f"Report: {spec.source}"
        if body.format == "csv":
            content = exporters.build_csv(compiled.columns, rows)
        else:
            content = await asyncio.to_thread(exporters.build_pdf, title, compiled.columns, rows)

        old_asset = await repo.get_asset(session, row.asset_id) if row.asset_id else None
        key = f"{ctx.org_id or 'platform'}/report/{uuid.uuid4()}.{body.format}"
        stored = await get_storage().put(key, content, exporters.CONTENT_TYPES[body.format], encrypt=True)
        asset = await repo.add_asset(
            session,
            org_id=ctx.org_id,
            kind=ASSET_KIND,
            storage_key=stored.key,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            encrypted=True,
            created_by=ctx.user_id,
        )
        row.columns, row.format, row.row_count = compiled.columns, body.format, len(rows)
        row.status, row.asset_id = "generated", asset.id
        row.filter_spec = spec.model_dump(mode="json")
        await session.flush()
        if old_asset is not None:
            await repo.delete_asset(session, old_asset)
            await get_storage().delete(old_asset.storage_key)
        await audit.record(
            ctx,
            session,
            audit.REPORT_GENERATED,
            "report_request",
            row.id,
            after={
                "user_text": row.user_text,
                "filter_spec": row.filter_spec,
                "columns": compiled.columns,
                "format": body.format,
                "row_count": len(rows),
            },
        )
        return _out(row)

    @staticmethod
    async def download(
        ctx: TenantContext, session: AsyncSession, report_id: uuid.UUID
    ) -> tuple[bytes, str, str]:
        """``(content, media_type, filename)``. Downloads are audited (spec 4.5)."""
        row = await ReportService._get(ctx, session, report_id)
        asset = await repo.get_asset(session, row.asset_id) if row.asset_id else None
        if asset is None:  # never generated, or removed by the nightly cleanup
            raise AppError("NOT_FOUND", details={"reason": "file_not_available"})
        content = await get_storage().get(asset.storage_key)
        await audit.record(
            ctx,
            session,
            audit.REPORT_DOWNLOADED,
            "report_request",
            row.id,
            after={"format": row.format, "row_count": row.row_count},
        )
        source = (row.filter_spec or {}).get("source", row.source)
        return content, asset.content_type, f"report-{source}-{str(row.id)[:8]}.{row.format}"

    @staticmethod
    async def history(ctx: TenantContext, session: AsyncSession, params: PageParams) -> Page[ReportOut]:
        rows, total = await repo.list_requests(session, ctx, offset=params.offset, limit=params.limit)
        return Page[ReportOut].build([_out(r) for r in rows], total=total, params=params)
