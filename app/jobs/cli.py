"""``python -m app.cli run-job <name>`` and ``list-jobs``."""

import asyncio
import json

import typer

from app.core import db as core_db
from app.jobs import JOBS


async def _run(name: str) -> dict:
    try:
        return await JOBS[name]()
    finally:
        if core_db._engine.cache_info().currsize:
            await core_db.get_engine().dispose()
        core_db._engine.cache_clear()


def register(app: typer.Typer) -> None:
    @app.command("list-jobs")
    def list_jobs() -> None:
        """Print the available job names."""
        for name in sorted(JOBS):
            typer.echo(name)

    @app.command("run-job")
    def run_job(name: str = typer.Argument(..., help="Job name, see list-jobs")) -> None:
        """Run one background job now and print its result as JSON."""
        if name not in JOBS:
            typer.echo(f"unknown job {name!r}; available: {', '.join(sorted(JOBS))}", err=True)
            raise typer.Exit(2)
        typer.echo(json.dumps(asyncio.run(_run(name)), default=str))
