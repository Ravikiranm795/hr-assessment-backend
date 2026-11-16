"""Demo data runner (spec section 12): two organizations, four assessments, roles, invites, attempts,
reviews and decisions, all created through the real services so audit entries, notifications and
scores appear naturally. Organizations are flagged ``is_demo``; ``reset_demo`` wipes and reseeds.
"""

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import typer
from sqlalchemy import select, text, update

from app.core import audit
from app.core.config import settings
from app.core.db import TenantContext, system_context, tenant_session
from app.core.flags import FLAG_KEYS
from app.core.roles import HR_USER, ORG_ADMIN, SUPER_ADMIN
from app.core.security import hash_password
from app.db import HelpRequest, Invite, User
from app.jobs import expire_invites
from app.modules.m01_auth.cli import create_default_superadmin
from app.modules.m02_platform.schemas import AdminIn, FlagsIn, OrgCreate
from app.modules.m02_platform.service import PlatformService
from app.modules.m03_team.schemas import UserCreate
from app.modules.m03_team.service import TeamService
from app.modules.m04_org_settings.schemas import BrandingIn, DocumentCreate
from app.modules.m04_org_settings.service import BrandingService, DocumentService
from app.modules.m05_assessments.schemas import (
    AssessmentCreate,
    HeaderConfig,
    OptionIn,
    ProctoringConfig,
    QuestionIn,
    QuestionOut,
    VersionOut,
    VersionPatch,
)
from app.modules.m05_assessments.service import AssessmentService
from app.modules.m06_roles.schemas import LinkIn, RoleCreate
from app.modules.m06_roles.service import RoleService
from app.modules.m07_invites.schemas import AccommodationsIn, ApproveIn, InviteCreate
from app.modules.m07_invites.service import HelpRequestService, InviteService
from app.modules.m09_review import scoring as _scoring  # noqa: F401  registers scoring on submit
from app.modules.m09_review.schemas import DecisionIn, DismissIn, ExtraTimeIn, OverrideIn, ScoreOverrideIn
from app.modules.m09_review.service import ReviewService
from app.modules.m12_dashboard.schemas import LayoutIn
from app.modules.m12_dashboard.service import DashboardService
from seed import data
from seed.candidate import Candidate
from seed.wipe import wipe_demo

log = logging.getLogger("seed.demo")
_DEMO_ORGS = "SELECT id FROM organizations WHERE is_demo"


def _in_demo_orgs(table: str) -> str:
    return f"SELECT count(*) FROM {table} WHERE org_id IN ({_DEMO_ORGS})"


_TABLES_FOR_COUNTS = {
    "organizations": "SELECT count(*) FROM organizations WHERE is_demo",
    "users": (
        f"SELECT count(*) FROM users WHERE org_id IN ({_DEMO_ORGS}) "
        "OR (org_id IS NULL AND email = :sa AND NOT is_default_super_admin)"
    ),
    **{
        table: _in_demo_orgs(table)
        for table in (
            "assessments",
            "assessment_versions",
            "open_roles",
            "invites",
            "attempts",
            "help_requests",
            "decisions",
        )
    },
}


# --------------------------------------------------------------------------- small helpers


def _staff_ctx(user_id: UUID, org_id: UUID | None, role: str) -> TenantContext:
    return TenantContext(
        org_id=org_id,
        user_id=user_id,
        role=role,
        is_platform=role == SUPER_ADMIN,
        request_id="seed-demo",
        ip=None,
        user_agent="seed-demo",
    )


@dataclass
class _Org:
    number: int
    id: UUID
    admin: TenantContext
    users: dict[str, TenantContext]  # account key -> context (admin included)


def _answer_for(question: QuestionOut, right: bool) -> dict[str, Any]:
    """A candidate answer in the shape ``ExamService`` expects, right or deliberately wrong."""
    key = question.answer_key if isinstance(question.answer_key, dict) else {}
    if question.type == "single_choice":
        wanted = [o for o in question.options if o.is_correct == right]
        return {"option_id": str(wanted[0].id)}
    if question.type == "multi_choice":
        picked = (
            [o for o in question.options if o.is_correct]
            if right
            else [o for o in question.options if not o.is_correct][:1]
        )
        return {"option_ids": [str(o.id) for o in picked]}
    if question.type in ("fill_blank", "code_gap"):
        name = "blanks" if question.type == "fill_blank" else "gaps"
        slots = key.get(name, [])
        values = [s["accepted"][0] for s in slots] if right else ["not sure"] * max(len(slots), 1)
        return {name: values}
    if right:
        return {"text": (question.model_answer or "A complete answer covering the key points.")}
    return {"text": "I would need to look this up."}


def _question_in(q: data.Q) -> QuestionIn:
    return QuestionIn(
        type=q.type,
        prompt_html=q.prompt,
        marks=Decimal(str(q.marks)),
        options=[OptionIn(text_html=text_, is_correct=ok) for text_, ok in q.options],
        answer_key=q.key,
        model_answer=q.model_answer,
        code_language=q.language,
    )


# --------------------------------------------------------------------------- phases


async def _platform_admin(password_hash: str) -> TenantContext:
    """The demo (daily) super admin. The break-glass admin comes from DEFAULT_SA_* when configured."""
    try:
        state = await create_default_superadmin()
        log.info("default super admin: %s", state)
    except typer.Exit:
        log.info("default super admin: not configured (DEFAULT_SA_* unset)")
    ctx = system_context("seed-demo")
    async with tenant_session(ctx) as session:
        user = User(
            org_id=None,
            email=data.PLATFORM_ADMIN.email,
            name=data.PLATFORM_ADMIN.name,
            role=SUPER_ADMIN,
            password_hash=password_hash,
            must_change_password=False,
        )
        session.add(user)
        await session.flush()
        await audit.record(
            ctx,
            session,
            audit.USER_CREATED,
            "user",
            user.id,
            after={"email": user.email, "role": SUPER_ADMIN, "demo": True},
        )
    return _staff_ctx(user.id, None, SUPER_ADMIN)


async def _create_org(
    sa: TenantContext,
    number: int,
    name: str,
    slug: str,
    admin: data.Account,
    hr: tuple[data.Account, ...],
    flags_off: tuple[str, ...],
) -> _Org:
    platform = PlatformService()
    async with tenant_session(sa) as session:
        org, admin_user, _temp = await platform.create_org(
            sa,
            session,
            OrgCreate(
                name=name,
                slug=slug,
                is_demo=True,
                admin=AdminIn(name=admin.name, email=admin.email),
            ),
        )
        org_id, admin_id = org.id, admin_user.id
        wanted = {key: key not in flags_off for key in sorted(FLAG_KEYS)}
        await platform.set_org_flags(sa, session, org_id, FlagsIn(flags=wanted, reason="Demo configuration"))
    admin_ctx = _staff_ctx(admin_id, org_id, ORG_ADMIN)
    users = {admin.key: admin_ctx}
    team = TeamService()
    for account in hr:
        async with tenant_session(admin_ctx) as session:
            user, _temp = await team.create_user(
                admin_ctx, session, UserCreate(name=account.name, email=account.email)
            )
            users[account.key] = _staff_ctx(user.id, org_id, HR_USER)
    return _Org(number, org_id, admin_ctx, users)


async def _set_known_passwords(org_ids: list[UUID], password_hash: str) -> None:
    ctx = system_context("seed-demo")
    async with tenant_session(ctx) as session:
        await session.execute(
            update(User)
            .where(User.org_id.in_(org_ids), User.role.in_([ORG_ADMIN, HR_USER]))
            .values(password_hash=password_hash, must_change_password=False)
        )


async def _org1_settings(org: _Org) -> None:
    ctx = org.admin
    async with tenant_session(ctx) as session:
        await BrandingService(session).put(ctx, BrandingIn(**data.BRANDING))
    for doc_type, title, html in (
        ("consent", "Proctoring consent", data.CONSENT_HTML),
        ("privacy", "Privacy notice", data.PRIVACY_HTML),
    ):
        async with tenant_session(ctx) as session:
            service = DocumentService(session)
            doc = await service.create(ctx, DocumentCreate(doc_type=doc_type, title=title))
            version = await service.create_version(ctx, doc.id, html=html)
            await service.publish_version(ctx, doc.id, version.id)


async def _layouts(orgs: list[_Org]) -> None:
    """Save each staff member's gadget layout (starting from the role's default one)."""
    for org in orgs:
        for ctx in org.users.values():
            async with tenant_session(ctx) as session:
                current = await DashboardService.layout(ctx, session)
                await DashboardService.save_layout(ctx, session, LayoutIn(gadgets=current.items))


async def _assessments(orgs: dict[int, _Org]) -> dict[tuple[str, int], tuple[UUID, VersionOut]]:
    """Create and publish every assessment version. Key: (assessment key, version number)."""
    registry: dict[tuple[str, int], tuple[UUID, VersionOut]] = {}
    for spec in data.ASSESSMENTS:
        org = orgs[spec.org]
        ctx = org.users[spec.owner]
        async with tenant_session(ctx) as session:
            created = await AssessmentService.create(
                ctx, session, AssessmentCreate(title=spec.title, description=spec.description)
            )
            assessment_id, version_id = created.id, created.versions[0].id
            await AssessmentService.update_version(
                ctx,
                session,
                assessment_id,
                version_id,
                VersionPatch(
                    duration_minutes=spec.duration_minutes,
                    pass_mark_percent=Decimal(spec.pass_mark_percent),
                    instructions_html=spec.instructions,
                    header_config=HeaderConfig(company_name=_org_name(spec.org)),
                    footer_html="<p>Good luck.</p>",
                    proctoring_config=ProctoringConfig(
                        snapshot_interval_seconds=spec.snapshot_interval_seconds
                    ),
                ),
            )
            for question in spec.questions:
                await AssessmentService.add_question(
                    ctx, session, assessment_id, version_id, _question_in(question)
                )
            await AssessmentService.publish_version(ctx, session, assessment_id, version_id)
            registry[(spec.key, 1)] = (
                assessment_id,
                await AssessmentService.get_version(ctx.org_id, session, assessment_id, version_id),
            )
        if spec.extra_v2:
            async with tenant_session(ctx) as session:
                draft = await AssessmentService.create_version(ctx, session, assessment_id)
                for question in spec.extra_v2:
                    await AssessmentService.add_question(
                        ctx, session, assessment_id, draft.id, _question_in(question)
                    )
                await AssessmentService.update_version(
                    ctx,
                    session,
                    assessment_id,
                    draft.id,
                    VersionPatch(duration_minutes=spec.duration_minutes + 5),
                )
                await AssessmentService.publish_version(ctx, session, assessment_id, draft.id)
                registry[(spec.key, 2)] = (
                    assessment_id,
                    await AssessmentService.get_version(ctx.org_id, session, assessment_id, draft.id),
                )
    return registry


def _org_name(number: int) -> str:
    return f"{settings.vendor_name} (demo)" if number == 1 else data.ORG2_NAME


async def _backdate(org_id: UUID, attempt_id: UUID | None, invite_id: UUID, days: int) -> None:
    """Spread demo activity over the last weeks so dashboards show a trend. Only timestamps change
    (shifted together, so every ordering stays valid); the audit log keeps its real times."""
    if days <= 0:
        return
    shift = {"d": days}
    ctx = system_context("seed-demo", org_id)
    async with tenant_session(ctx) as session:
        await session.execute(
            text(
                "UPDATE invites SET created_at = created_at - make_interval(days => :d), "
                "expires_at = expires_at - make_interval(days => :d) WHERE id = :i"
            ),
            {**shift, "i": invite_id},
        )
        if attempt_id is None:
            return
        params = {**shift, "a": attempt_id}
        for statement in (
            "UPDATE attempts SET created_at = created_at - make_interval(days => :d), "
            "consent_at = consent_at - make_interval(days => :d), "
            "started_at = started_at - make_interval(days => :d), "
            "deadline_at = deadline_at - make_interval(days => :d), "
            "submitted_at = submitted_at - make_interval(days => :d), "
            "decided_at = decided_at - make_interval(days => :d) WHERE id = :a",
            "UPDATE proctor_events SET occurred_at = occurred_at - make_interval(days => :d), "
            "received_at = received_at - make_interval(days => :d) WHERE attempt_id = :a",
            "UPDATE decisions SET created_at = created_at - make_interval(days => :d) WHERE attempt_id = :a",
        ):
            await session.execute(text(statement), params)


async def _run_invite(
    spec: data.InviteSpec,
    *,
    role_id: UUID,
    owner: TenantContext,
    admin: TenantContext,
    version: VersionOut,
    index: int,
) -> None:
    accommodations = None
    if spec.camera_exempt or spec.time_multiplier or spec.extra_minutes:
        accommodations = AccommodationsIn(
            camera_exempt=True if spec.camera_exempt else None,
            time_multiplier=Decimal(str(spec.time_multiplier)) if spec.time_multiplier else None,
            extra_minutes=spec.extra_minutes,
            reason="Accommodation agreed with the candidate before the exam",
        )
    hours = 1 if spec.outcome == "expired" else spec.expires_in_hours
    async with tenant_session(owner) as session:
        invite, url = await InviteService.create(
            owner,
            session,
            role_id,
            InviteCreate(
                candidate_name=spec.name,
                email=spec.email,
                expires_in_hours=hours,
                accommodations=accommodations,
            ),
        )
        invite_id = invite.id
    token = url.rsplit("/", 1)[1]

    attempt_id: UUID | None = None
    if spec.outcome == "revoked":
        async with tenant_session(owner) as session:
            await InviteService.revoke(owner, session, invite_id)
    elif spec.outcome == "expired":
        async with tenant_session(owner) as session:  # the expiry job below flips the status
            await session.execute(
                update(Invite)
                .where(Invite.id == invite_id)
                .values(expires_at=text("now() - interval '1 hour'"))
            )
    elif spec.outcome != "invited":
        attempt_id = await _play_attempt(spec, token, invite_id, owner, admin, version, index)
    await _backdate(owner.org_id, attempt_id, invite_id, spec.days_ago)  # type: ignore[arg-type]


async def _play_attempt(
    spec: data.InviteSpec,
    token: str,
    invite_id: UUID,
    owner: TenantContext,
    admin: TenantContext,
    version: VersionOut,
    index: int,
) -> UUID:
    candidate = Candidate(token, f"{index:02d}", f"203.0.113.{10 + index}")
    await candidate.open()
    if spec.help_first:
        await candidate.ask_help(spec.help_reason, "I cannot use a camera on this machine.")
        async with tenant_session(owner) as session:
            help_id = (
                await session.execute(select(HelpRequest.id).where(HelpRequest.invite_id == invite_id))
            ).scalar_one()
            await HelpRequestService.approve(
                owner,
                session,
                help_id,
                ApproveIn(
                    camera_exempt=True,
                    extra_minutes=10,
                    note="Approved: camera exemption and 10 extra minutes",
                ),
            )
    await candidate.prepare(photo_seed=index)
    if spec.outcome == "help_open":
        await candidate.ask_help(spec.help_reason, "The camera light turns on but the picture is black.")
        return candidate.attempt_id

    await candidate.start()
    questions = sorted(version.questions, key=lambda q: q.position)
    plan = spec.correct
    for position, question in enumerate(questions, start=1):
        flag = plan[position - 1] if position - 1 < len(plan) else None
        if question.type == "short_answer" and flag is None and position in spec.short_marks:
            flag = "1"
        if flag in (None, "-"):
            continue
        await candidate.answer(question.id, _answer_for(question, flag == "1"))

    rows = []
    if spec.events:
        rows = await candidate.send_events([(e.type, e.duration_ms) for e in spec.events])
        for seq, event in enumerate(spec.events):
            if event.snapshot:
                stored = next(r for r in rows if r.meta.get("seq") == seq)
                await candidate.snapshot(stored.id, index + seq)

    attempt_id = candidate.attempt_id
    if spec.outcome == "in_progress":
        if spec.extra_time_grant:
            async with tenant_session(owner) as session:
                await ReviewService.extra_time(
                    owner,
                    session,
                    attempt_id,
                    ExtraTimeIn(minutes=spec.extra_time_grant, reason="Network outage reported"),
                )
        return attempt_id

    await candidate.submit()
    for position, marks in spec.short_marks.items():
        question = next(q for q in questions if q.position == position)
        async with tenant_session(owner) as session:
            await ReviewService.override_score(
                owner,
                session,
                attempt_id,
                question.id,
                ScoreOverrideIn(marks=Decimal(str(marks)), reason="Scored against the model answer"),
            )
    for event_type in spec.dismiss:
        stored = next(r for r in rows if r.type == event_type)
        async with tenant_session(owner) as session:
            await ReviewService.dismiss_flag(
                owner, session, attempt_id, stored.id, DismissIn(note="Reviewed: not a violation")
            )
    if spec.decision:
        async with tenant_session(owner) as session:
            await ReviewService.decide(
                owner, session, attempt_id, DecisionIn(decision=spec.decision, note="Reviewed the report")
            )
    if spec.override:
        decision, reason = spec.override
        async with tenant_session(admin) as session:
            await ReviewService.override(
                admin, session, attempt_id, OverrideIn(decision=decision, reason=reason)
            )
    return attempt_id


async def _roles(orgs: dict[int, _Org], registry: dict[tuple[str, int], tuple[UUID, VersionOut]]) -> None:
    counter = 0
    for spec in data.ROLES:
        org = orgs[spec.org]
        owner = org.users[spec.owner]
        first_version = spec.invites[0].version_no if spec.invites else 1
        async with tenant_session(owner) as session:
            role = await RoleService.create(
                owner,
                session,
                RoleCreate(
                    title=spec.title,
                    department=spec.department,
                    experience_level=spec.level,
                    vacancies=spec.vacancies,
                    description=spec.description,
                ),
            )
            role_id = role.id
            await RoleService.link_assessment(
                owner,
                session,
                role_id,
                LinkIn(assessment_version_id=registry[(spec.assessment, first_version)][1].id),
            )
            if spec.status != "draft":
                await RoleService.open(owner, session, role_id)
        linked = first_version
        for invite in spec.invites:
            if invite.version_no != linked:  # the role moves to the newer published version
                async with tenant_session(owner) as session:
                    await RoleService.link_assessment(
                        owner,
                        session,
                        role_id,
                        LinkIn(assessment_version_id=registry[(spec.assessment, invite.version_no)][1].id),
                    )
                linked = invite.version_no
            counter += 1
            await _run_invite(
                invite,
                role_id=role_id,
                owner=owner,
                admin=org.admin,
                version=registry[(spec.assessment, invite.version_no)][1],
                index=counter,
            )
        if spec.status == "closed":
            async with tenant_session(owner) as session:
                await RoleService.close(owner, session, role_id)


# --------------------------------------------------------------------------- public entry points


async def demo_counts() -> dict[str, int]:
    ctx = system_context("seed-demo")
    out: dict[str, int] = {}
    async with tenant_session(ctx) as session:
        for name, sql in _TABLES_FOR_COUNTS.items():
            params = {"sa": data.PLATFORM_ADMIN.email} if ":sa" in sql else {}
            out[name] = int((await session.execute(text(sql), params)).scalar_one())
    return out


async def seed_demo() -> dict[str, Any]:
    """Create the demo set. Does nothing (``status: exists``) when demo data is already present."""
    counts = await demo_counts()
    if counts["organizations"]:
        return {"status": "exists", "counts": counts}
    password_hash = await asyncio.to_thread(hash_password, data.DEMO_PASSWORD)
    sa = await _platform_admin(password_hash)
    org1 = await _create_org(sa, 1, _org_name(1), data.ORG1_SLUG, data.ORG1_ADMIN, data.ORG1_HR, flags_off=())
    org2 = await _create_org(
        sa, 2, _org_name(2), data.ORG2_SLUG, data.ORG2_ADMIN, data.ORG2_HR, flags_off=data.ORG2_FLAGS_OFF
    )
    await _set_known_passwords([org1.id, org2.id], password_hash)
    orgs = {1: org1, 2: org2}
    await _org1_settings(org1)
    registry = await _assessments(orgs)
    await _roles(orgs, registry)
    await _layouts([org1, org2])
    await expire_invites.run()  # unstarted invites past their expiry become "expired"; owners get warnings
    return {"status": "seeded", "counts": await demo_counts()}


async def reset_demo() -> dict[str, Any]:
    """Wipe demo data (only ``is_demo`` organizations and the demo platform admin), then reseed."""
    wiped = await wipe_demo()
    result = await seed_demo()
    return {**result, "wiped": wiped}
