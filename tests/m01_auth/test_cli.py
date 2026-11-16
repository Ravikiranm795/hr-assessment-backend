"""``create-default-superadmin`` command."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from app.cli import cli
from app.core.config import settings
from app.modules.m01_auth.cli import create_default_superadmin
from tests import factories


def test_command_is_registered_for_the_entrypoint():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0 and "create-default-superadmin" in result.stdout


def test_missing_settings_skip_without_failing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "default_sa_email", "")
    result = CliRunner().invoke(cli, ["create-default-superadmin"])
    assert result.exit_code == 0  # the container must still start


async def test_weak_password_is_refused(monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession):
    import typer

    monkeypatch.setattr(settings, "default_sa_email", "weak@example.com")
    monkeypatch.setattr(settings, "default_sa_name", "Weak")
    monkeypatch.setattr(settings, "default_sa_password", "short")
    with pytest.raises(typer.Exit) as exc:
        await create_default_superadmin()
    assert exc.value.exit_code == 1


async def test_email_owned_by_another_account_is_refused(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
):
    import typer

    other = await factories.make_user(db_session, "super_admin")
    await db_session.commit()
    monkeypatch.setattr(settings, "default_sa_email", other.email)
    monkeypatch.setattr(settings, "default_sa_name", "Clash")
    monkeypatch.setattr(settings, "default_sa_password", "a-strong-password-123")
    with pytest.raises(typer.Exit) as exc:
        await create_default_superadmin()
    assert exc.value.exit_code == 1
