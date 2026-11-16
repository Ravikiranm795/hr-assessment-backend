from typer.testing import CliRunner

from app import __version__
from app.cli import cli


def test_cli_group_lists_builtin_version_command() -> None:
    result = CliRunner().invoke(cli, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
