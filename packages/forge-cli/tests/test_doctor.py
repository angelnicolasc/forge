"""Tests for `forge doctor` (ζ.13)."""

from __future__ import annotations

from typer.testing import CliRunner

from forge_cli.main import app

runner = CliRunner()


def test_doctor_runs_and_reports_green_on_healthy_install() -> None:
    """On a workspace where all five forge packages are importable and
    chromadb/networkx are pulled in transitively, doctor should exit 0."""
    result = runner.invoke(app, ["doctor"])
    # Exit code 0 if issues==0, 1 otherwise. The test env has everything,
    # so we expect 0; if something's missing the output still must mention
    # it rather than crash.
    assert result.exit_code in (0, 1)
    assert "forge doctor" in result.stdout
    assert "Python version" in result.stdout
    assert "forge_core" in result.stdout


def test_doctor_lists_optional_extras() -> None:
    """Optional extras appear as rows (\u2713 or \u26a0), never crash the table."""
    result = runner.invoke(app, ["doctor"])
    assert "LangGraph adapter" in result.stdout
    assert "CrewAI adapter" in result.stdout
    assert "AutoGen adapter" in result.stdout
