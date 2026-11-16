"""``create-default-superadmin``: seed the break-glass super admin from the environment (A-05).

Idempotent: the container entrypoint runs it on every start. The row is protected by a database
trigger afterwards (no edits, no deletes), so a changed ``DEFAULT_SA_PASSWORD`` is not applied to
an existing row; that needs a superuser-level maintenance step documented in the handover.
"""

import asyncio

import typer
from sqlalchemy.exc import IntegrityError

from app.core import audit
from app.core.config import settings
from app.core.db import system_context, tenant_session
from app.core.roles import SUPER_ADMIN
from app.core.security import hash_password, password_policy_violations
from app.db import User


async def create_default_superadmin() -> str:
    """Returns ``created`` or ``exists``. Raises ``typer.Exit`` for unusable configuration."""
    email = settings.default_sa_email.strip()
    name = settings.default_sa_name.strip()
    password = settings.default_sa_password
    if not (email and name and password):
        typer.echo(
            "create-default-superadmin: DEFAULT_SA_EMAIL, DEFAULT_SA_NAME and DEFAULT_SA_PASSWORD are not "
            "all set; skipping.",
            err=True,
        )
        raise typer.Exit(0)

    from sqlalchemy import select

    ctx = system_context("create-default-superadmin")
    async with tenant_session(ctx) as session:
        existing = (
            await session.execute(select(User).where(User.is_default_super_admin.is_(True)))
        ).scalar_one_or_none()
        if existing is not None:
            return "exists"
        clash = (await session.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
        if clash is not None:
            typer.echo("create-default-superadmin: DEFAULT_SA_EMAIL belongs to another account.", err=True)
            raise typer.Exit(1)
        problems = password_policy_violations(password)
        if problems:
            typer.echo(
                f"create-default-superadmin: DEFAULT_SA_PASSWORD is weak ({', '.join(problems)}).", err=True
            )
            raise typer.Exit(1)
        try:
            async with session.begin_nested():
                user = User(
                    org_id=None,
                    email=email,
                    name=name,
                    role=SUPER_ADMIN,
                    password_hash=hash_password(password),
                    must_change_password=False,  # the account cannot be edited, so no forced change
                    is_default_super_admin=True,
                )
                session.add(user)
                await session.flush()
        except IntegrityError:
            return "exists"  # another instance won the race
        await audit.record(
            ctx,
            session,
            audit.USER_CREATED,
            "user",
            user.id,
            after={"email": email, "role": SUPER_ADMIN, "is_default_super_admin": True},
        )
    return "created"


def register(app: typer.Typer) -> None:
    @app.command("create-default-superadmin")
    def _create_default_superadmin() -> None:
        """Create the default (break-glass) super admin from DEFAULT_SA_* settings, if missing."""
        typer.echo(f"create-default-superadmin: {asyncio.run(create_default_superadmin())}")
