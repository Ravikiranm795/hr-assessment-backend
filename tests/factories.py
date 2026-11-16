"""Test data factories and auth helpers shared by every module's tests.

All factories take an ``AsyncSession`` first (the ``db_session`` fixture), add rows,
flush, and return the ORM object. They never commit; the caller decides. Keyword
overrides are passed straight to the model, so any column can be set.

Tokens: ``make_token`` is the single place tests mint tokens; it delegates to
``app.core.security.create_access_token`` (keys installed by ``tests/conftest.py`` via
``core.testing.configure_test_secrets``).
"""

import uuid
from datetime import UTC, datetime, timedelta
from itertools import count
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.db import (
    Assessment,
    AssessmentVersion,
    Attempt,
    Invite,
    OpenRole,
    Organization,
    Question,
    QuestionOption,
    RoleAssessmentLink,
    User,
)

_seq = count(1)

FAKE_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$test$not-a-real-hash"


def _n() -> int:
    return next(_seq)


# --------------------------------------------------------------------------- hashing and tokens


def email_hash(email: str) -> str:
    """Blind index of a candidate email, exactly as the application computes it."""
    return security.blind_index(email)


def hash_password(password: str) -> str:
    return security.hash_password(password)


def make_token(
    role: str,
    org_id: uuid.UUID | None,
    user_id: uuid.UUID,
    token_version: int = 0,
    sid: uuid.UUID | None = None,
) -> str:
    """Signed API access token for the given identity, minted by ``core.security``."""
    user = SimpleNamespace(id=user_id, org_id=org_id, role=role, token_version=token_version)
    return security.create_access_token(user, sid or uuid.uuid4())


# --------------------------------------------------------------------------- factories


async def _add(session: AsyncSession, obj: Any) -> Any:
    session.add(obj)
    await session.flush()
    return obj


async def make_org(session: AsyncSession, **overrides: Any) -> Organization:
    n = _n()
    values = {"name": f"Org {n}", "slug": f"org-{n}-{uuid.uuid4().hex[:6]}"}
    values.update(overrides)
    return await _add(session, Organization(**values))


async def make_user(
    session: AsyncSession,
    role: str = "hr_user",
    org: Organization | None = None,
    *,
    password: str | None = None,
    **overrides: Any,
) -> User:
    """``role`` is ``super_admin``, ``org_admin`` or ``hr_user``.

    Non-SA users belong to ``org`` (a new org is created when omitted).
    """
    n = _n()
    if role != "super_admin" and org is None and "org_id" not in overrides:
        org = await make_org(session)
    values = {
        "org_id": None if role == "super_admin" else (org.id if org else None),
        "email": f"user{n}-{uuid.uuid4().hex[:6]}@example.com",
        "name": f"User {n}",
        "role": role,
        "password_hash": hash_password(password) if password else FAKE_PASSWORD_HASH,
        "must_change_password": False,
    }
    values.update(overrides)
    return await _add(session, User(**values))


async def make_assessment(
    session: AsyncSession,
    org: Organization,
    owner: User | None = None,
    **overrides: Any,
) -> Assessment:
    owner = owner or await make_user(session, "hr_user", org)
    values = {"org_id": org.id, "title": f"Assessment {_n()}", "owner_id": owner.id, "created_by": owner.id}
    values.update(overrides)
    return await _add(session, Assessment(**values))


async def make_version(
    session: AsyncSession,
    assessment: Assessment,
    *,
    status: str = "published",
    version_no: int = 1,
    questions: int = 1,
    **overrides: Any,
) -> AssessmentVersion:
    """A version with ``questions`` single-choice questions (two options each).

    Questions are inserted while the version is a draft, then it is published, because
    the database refuses changes under a published version.
    """
    values = {
        "assessment_id": assessment.id,
        "org_id": assessment.org_id,
        "version_no": version_no,
        "status": "draft",
    }
    values.update(overrides)
    version = await _add(session, AssessmentVersion(**values))
    for position in range(1, questions + 1):
        question = await _add(
            session,
            Question(
                version_id=version.id,
                org_id=assessment.org_id,
                position=position,
                type="single_choice",
                prompt_html=f"<p>Question {position}</p>",
                marks=1,
            ),
        )
        session.add_all(
            [
                QuestionOption(question_id=question.id, position=1, text_html="Right", is_correct=True),
                QuestionOption(question_id=question.id, position=2, text_html="Wrong", is_correct=False),
            ]
        )
    version.total_marks = questions
    await session.flush()
    if status == "published":
        version.status = "published"
        version.published_at = datetime.now(UTC)
        version.published_by = assessment.owner_id
        await session.flush()
    return version


async def make_role(
    session: AsyncSession,
    org: Organization,
    owner: User | None = None,
    *,
    version: AssessmentVersion | None = None,
    **overrides: Any,
) -> OpenRole:
    """An open role; linked to ``version`` when given."""
    owner = owner or await make_user(session, "hr_user", org)
    values = {
        "org_id": org.id,
        "title": f"Role {_n()}",
        "owner_id": owner.id,
        "status": "open",
        "created_by": owner.id,
    }
    values.update(overrides)
    role = await _add(session, OpenRole(**values))
    if version is not None:
        await _add(
            session,
            RoleAssessmentLink(
                org_id=org.id, role_id=role.id, assessment_version_id=version.id, linked_by=owner.id
            ),
        )
    return role


async def make_invite(
    session: AsyncSession,
    org: Organization,
    role: OpenRole | None = None,
    version: AssessmentVersion | None = None,
    *,
    email: str | None = None,
    token: str | None = None,
    **overrides: Any,
) -> Invite:
    """An invite inside ``role`` (an open role linked to a fresh published version if omitted).

    The raw token is stored on the returned object as ``invite.raw_token`` (never in the DB).
    """
    if version is None:
        assessment = await make_assessment(session, org)
        version = await make_version(session, assessment)
    if role is None:
        role = await make_role(session, org, version=version)
    email = email or f"candidate{_n()}@example.com"
    token = token or uuid.uuid4().hex
    values = {
        "org_id": org.id,
        "role_id": role.id,
        "assessment_version_id": version.id,
        "candidate_name": f"Candidate {_n()}",
        "candidate_email_enc": None,
        "candidate_email_hash": email_hash(email),
        "token_hash": security.hmac_hash(token),
        "expires_at": datetime.now(UTC) + timedelta(hours=72),
        "created_by": role.owner_id,
    }
    values.update(overrides)
    invite = await _add(session, Invite(**values))
    invite.raw_token = token
    return invite


async def make_attempt(
    session: AsyncSession,
    invite: Invite,
    *,
    status: str = "in_progress",
    duration_minutes: int = 60,
    **overrides: Any,
) -> Attempt:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "org_id": invite.org_id,
        "invite_id": invite.id,
        "assessment_version_id": invite.assessment_version_id,
        "status": status,
    }
    if status in ("in_progress", "submitted"):
        values.update(started_at=now, deadline_at=now + timedelta(minutes=duration_minutes))
    if status == "submitted":
        values.update(submitted_at=now, submit_reason="candidate")
    values.update(overrides)
    attempt = await _add(session, Attempt(**values))
    invite.status = {"in_progress": "started", "submitted": "submitted"}.get(status, invite.status)
    await session.flush()
    return attempt


async def auth_headers(
    session: AsyncSession,
    role: str,
    org: Organization | None = None,
    *,
    user: User | None = None,
) -> dict[str, str]:
    """``Authorization`` header for a (new, unless given) user with ``role`` in ``org``."""
    if user is None:
        user = await make_user(session, role, org)
    token = make_token(user.role, user.org_id, user.id, token_version=user.token_version or 0)
    return {"Authorization": f"Bearer {token}"}
