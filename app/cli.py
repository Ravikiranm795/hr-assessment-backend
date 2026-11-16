"""Command line entry (``python -m app.cli``).

Commands live next to the code they belong to. Every ``app/modules/*/cli.py`` and
``app/jobs/cli.py`` exposing ``register(app: typer.Typer)`` is loaded automatically.
"""

import importlib
import importlib.util

import typer

import app.modules as modules_package
from app import __version__
from app.core.autoload import load_submodules

cli = typer.Typer(help="Operations commands", no_args_is_help=True, add_completion=False)


@cli.callback()
def main() -> None:
    """Keep this a command group even while only one command is registered."""


@cli.command("version")
def show_version() -> None:
    """Print the application version."""
    typer.echo(__version__)


def _register_all() -> None:
    cli_modules = load_submodules(modules_package, "cli")
    if importlib.util.find_spec("app.jobs.cli") is not None:
        cli_modules.append(importlib.import_module("app.jobs.cli"))
    if importlib.util.find_spec("seed.cli") is not None:
        cli_modules.append(importlib.import_module("seed.cli"))
    for module in cli_modules:
        module.register(cli)


_register_all()

if __name__ == "__main__":
    cli()
