"""Version rules (spec S-04, S-05, 5.4): one draft per assessment, at most three versions, the oldest
unprotected version is purged to make room, and a protected version is never deleted."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import TenantContext
from app.core.errors import AppError
from app.db import AssessmentVersion
from app.modules.m05_assessments import repo

MAX_VERSIONS = 3


class VersionRules:
    MAX_VERSIONS = MAX_VERSIONS

    @staticmethod
    async def create_draft(ctx: TenantContext, session: AsyncSession, assessment_id) -> AssessmentVersion:
        """New draft cloned from the latest published version.

        - A draft already exists: ``DRAFT_ALREADY_EXISTS``.
        - Three versions exist: the oldest *unprotected* one is deleted first (audited as
          ``VERSION_PURGED``); if all three are protected, ``VERSION_LIMIT_REACHED_ALL_IN_USE``.
        The assessment row is locked so two concurrent requests cannot both create a draft.
        """
        org_id = ctx.org_id
        assessment = await repo.get_assessment(session, org_id, assessment_id, lock=True)
        if assessment is None:
            raise AppError("NOT_FOUND")
        versions = await repo.list_versions(session, org_id, [assessment.id])
        if any(v.status == "draft" for v in versions):
            raise AppError("DRAFT_ALREADY_EXISTS")
        published = [v for v in versions if v.status == "published"]
        if not published:  # cannot happen (v1 starts as a draft), but never clone from nothing
            raise AppError("ASSESSMENT_NOT_PUBLISHED")
        source = max(published, key=lambda v: v.version_no)

        if len(versions) >= MAX_VERSIONS:
            protected = await repo.protected_version_ids(session, org_id, assessment.id)
            victim = next((v for v in sorted(versions, key=lambda v: v.version_no) if v.id not in protected), None)
            if victim is None:
                raise AppError("VERSION_LIMIT_REACHED_ALL_IN_USE")
            await audit.record(
                ctx,
                session,
                audit.VERSION_PURGED,
                "assessment_version",
                victim.id,
                before={"assessment_id": assessment.id, "version_no": victim.version_no, "status": victim.status},
                reason="version_limit",
            )
            await repo.delete_version(session, victim)

        draft = await repo.insert_version(
            session,
            assessment_id=assessment.id,
            org_id=org_id,
            version_no=await repo.next_version_no(session, assessment.id),
            status="draft",
            duration_minutes=source.duration_minutes,
            pass_mark_percent=source.pass_mark_percent,
            instructions_html=source.instructions_html,
            header_config=source.header_config,
            footer_html=source.footer_html,
            proctoring_config=source.proctoring_config,
            shuffle_questions=source.shuffle_questions,
            shuffle_options=source.shuffle_options,
            created_by=ctx.user_id,
        )
        await repo.clone_content(session, org_id, source.id, draft)
        await audit.record(
            ctx,
            session,
            audit.VERSION_CREATED,
            "assessment_version",
            draft.id,
            after={"assessment_id": assessment.id, "version_no": draft.version_no, "cloned_from": source.id},
        )
        return draft
