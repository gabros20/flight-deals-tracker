import pytest
from typer.testing import CliRunner
from flight_deals.cli import app

runner = CliRunner()

def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "usage" in result.output.lower() or "help" in result.output.lower()