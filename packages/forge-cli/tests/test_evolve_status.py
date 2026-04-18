"""Tests for ``forge evolve status`` / ``forge evolve resume`` (ζ.15)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

from forge_cli.main import app

runner = CliRunner()

# Rich truncates narrow tables in the default 80-col test terminal.
_WIDE_ENV = {"COLUMNS": "200", "TERM": "xterm-256color"}


def _seed_journal(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_evolve_status_empty_journal(tmp_path: Path) -> None:
    journal = tmp_path / "evolution_journal.jsonl"
    result = runner.invoke(app, ["evolve", "status", "--journal", str(journal)])
    assert result.exit_code == 0
    assert "No evolution history" in result.stdout


def test_evolve_status_renders_entries(tmp_path: Path) -> None:
    journal = tmp_path / "evolution_journal.jsonl"
    _seed_journal(
        journal,
        [
            {
                "mutation_id": "m1",
                "mutation_kind": "prompt_rewrite",
                "description": "Tighten tool selection",
                "kind": "applied",
                "fitness_before": 0.60,
                "fitness_after": 0.72,
                "improvement": 0.12,
                "reason": "",
                "timestamp": "2026-04-18T12:00:00+00:00",
            },
            {
                "mutation_id": "m2",
                "mutation_kind": "parameter_tune",
                "description": "Reduce max_steps",
                "kind": "rolled_back",
                "fitness_before": 0.72,
                "fitness_after": 0.65,
                "improvement": -0.07,
                "reason": "quality_regression",
                "timestamp": "2026-04-18T12:05:00+00:00",
            },
        ],
    )

    result = runner.invoke(app, ["evolve", "status", "--journal", str(journal)], env=_WIDE_ENV)
    assert result.exit_code == 0
    # Column headers + row content
    assert "Evolution Journal" in result.stdout
    assert "prompt_rewrite" in result.stdout
    assert "parameter_tune" in result.stdout
    assert "applied" in result.stdout
    assert "rolled_back" in result.stdout
    assert "+0.120" in result.stdout
    assert "-0.070" in result.stdout


def test_evolve_status_shows_breaker_banner_when_open(tmp_path: Path) -> None:
    journal = tmp_path / "evolution_journal.jsonl"
    _seed_journal(
        journal,
        [
            {
                "mutation_id": "m1",
                "mutation_kind": "prompt_rewrite",
                "description": "",
                "kind": "breaker_opened",
                "fitness_before": None,
                "fitness_after": None,
                "improvement": None,
                "reason": "consecutive_rollbacks",
                "timestamp": "2026-04-18T13:00:00+00:00",
            },
        ],
    )

    result = runner.invoke(app, ["evolve", "status", "--journal", str(journal)], env=_WIDE_ENV)
    assert result.exit_code == 0
    assert "breaker" in result.stdout.lower()
    assert "forge evolve resume" in result.stdout
