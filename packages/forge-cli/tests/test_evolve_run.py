"""Command-line validation for change proposals."""

from unittest.mock import AsyncMock, patch

from rich.text import Text
from typer.testing import CliRunner

from forge_cli.main import app


def test_invalid_mode_is_rejected_before_loading_the_flow() -> None:
    with patch("forge_core.harness.MetaOrchestrator.load", new_callable=AsyncMock) as load:
        result = CliRunner().invoke(app, ["evolve", "run", "example.py", "--mode", "atuo"])

    message = Text.from_ansi(result.output).plain
    assert result.exit_code == 2
    assert "--mode" in message
    assert "suggest" in message and "auto" in message
    load.assert_not_awaited()
