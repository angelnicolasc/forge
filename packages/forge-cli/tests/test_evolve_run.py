"""Command-line validation for change proposals."""

from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from forge_cli.main import app


def test_invalid_mode_is_rejected_before_loading_the_flow() -> None:
    with patch("forge_core.harness.MetaOrchestrator.load", new_callable=AsyncMock) as load:
        result = CliRunner().invoke(app, ["evolve", "run", "example.py", "--mode", "atuo"])

    assert result.exit_code == 2
    assert "--mode" in result.output
    assert "suggest" in result.output and "auto" in result.output
    load.assert_not_awaited()
