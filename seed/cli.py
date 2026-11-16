"""``seed-demo`` and ``reset-demo`` (spec section 12). Loaded by ``app/cli.py``."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from app.core.config import settings


def _require_secrets() -> None:
    missing = [
        name
        for name, value in (
            ("JWT_PRIVATE_KEY", settings.jwt_private_key),
            ("JWT_PUBLIC_KEY", settings.jwt_public_key),
            ("FIELD_ENCRYPTION_KEYS", settings.field_encryption_keys),
            ("TOKEN_PEPPER", settings.token_pepper),
        )
        if not value
    ]
    if missing:
        typer.echo(f"Set {', '.join(missing)} first: demo data is encrypted and signed with them.", err=True)
        raise typer.Exit(2)


def _refuse_in_prod() -> None:
    if settings.app_env == "prod":
        typer.echo(
            "Demo data is never created in a production environment (APP_ENV=prod).", err=True
        )  # friendly; seed_demo() enforces it
        raise typer.Exit(2)


def register(app: typer.Typer) -> None:
    @app.command("export-openapi")
    def export_openapi_command(
        output: Annotated[Path, typer.Option("--output", "-o", help="Target file.")] = Path(
            "docs/openapi.json"
        ),
    ) -> None:
        """Write the OpenAPI document (docs/openapi.json by default). Needs no database or secrets."""
        from app.main import create_app

        output.parent.mkdir(parents=True, exist_ok=True)
        document = create_app().openapi()
        output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        typer.echo(f"wrote {output} ({len(document['paths'])} paths)")

    @app.command("seed-demo")
    def seed_demo_command() -> None:
        """Create the demo organizations, assessments, roles, invites and reviews (no-op if present)."""
        _refuse_in_prod()
        _require_secrets()
        from seed.demo import seed_demo

        typer.echo(json.dumps(asyncio.run(seed_demo()), indent=2, default=str))

    @app.command("reset-demo")
    def reset_demo_command() -> None:
        """Wipe all demo data (organizations flagged is_demo) and reseed it."""
        _refuse_in_prod()
        _require_secrets()
        from seed.demo import reset_demo

        typer.echo(json.dumps(asyncio.run(reset_demo()), indent=2, default=str))
