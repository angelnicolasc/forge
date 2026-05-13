"""Tests for DefaultCostModel — including thinking tokens, cached tokens,
pricing override, and the save/load cache helpers."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from forge_observe.cost_model import (
    BUNDLED_PRICING_TABLE,
    DefaultCostModel,
    _is_fresh,
    _load_pricing_table,
    _parse_pricing_json,
    save_pricing_cache,
)


# ---------------------------------------------------------------------------
# Basic cost calculation
# ---------------------------------------------------------------------------


def test_known_model_basic_cost() -> None:
    cm = DefaultCostModel()
    cost = cm.cost("claude-sonnet-4-20250514", input_tokens=1_000_000, output_tokens=0)
    assert cost == Decimal("3.00")


def test_known_model_output_cost() -> None:
    cm = DefaultCostModel()
    cost = cm.cost("claude-sonnet-4-20250514", input_tokens=0, output_tokens=1_000_000)
    assert cost == Decimal("15.00")


def test_thinking_tokens_billed_separately() -> None:
    cm = DefaultCostModel()
    # claude-opus-4 has thinking_price = 3.00/MTok
    cost_with_thinking = cm.cost(
        "claude-opus-4-20250514",
        input_tokens=0,
        output_tokens=0,
        thinking_tokens=1_000_000,
    )
    assert cost_with_thinking == Decimal("3.00")


def test_cached_input_tokens_billed_at_cache_rate() -> None:
    cm = DefaultCostModel()
    # claude-opus-4 cache_read = 1.50/MTok
    cost = cm.cost(
        "claude-opus-4-20250514",
        input_tokens=0,
        output_tokens=0,
        cached_input_tokens=1_000_000,
    )
    assert cost == Decimal("1.50")


def test_combined_all_token_types() -> None:
    cm = DefaultCostModel()
    cost = cm.cost(
        "claude-opus-4-20250514",
        input_tokens=1_000_000,   # 15.00
        output_tokens=1_000_000,  # 75.00
        thinking_tokens=1_000_000,  # 3.00
        cached_input_tokens=1_000_000,  # 1.50
    )
    assert cost == Decimal("94.50")


def test_unknown_model_returns_zero_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    cm = DefaultCostModel()
    with caplog.at_level(logging.WARNING):
        cost = cm.cost("unknown-model-xyz", input_tokens=1000, output_tokens=500)
    assert cost == Decimal("0")
    # Warning emitted exactly once
    cost2 = cm.cost("unknown-model-xyz", input_tokens=1000, output_tokens=500)
    assert cost2 == Decimal("0")


def test_prefix_matching_resolves_versioned_model() -> None:
    cm = DefaultCostModel()
    # "claude-opus-4" prefix matches "claude-opus-4-20250514"
    cost = cm.cost("claude-opus-4", input_tokens=1_000_000, output_tokens=0)
    assert cost > Decimal("0")


def test_add_model_extends_pricing() -> None:
    cm = DefaultCostModel()
    cm.add_model(
        "custom-model",
        Decimal("1.00"),
        Decimal("2.00"),
        Decimal("0.50"),
        Decimal("0.10"),
    )
    cost = cm.cost("custom-model", input_tokens=1_000_000, output_tokens=0)
    assert cost == Decimal("1.00")


def test_add_model_clears_warned_flag() -> None:
    cm = DefaultCostModel()
    cm.cost("new-model", 100, 100)  # triggers warning, adds to warned set
    assert "new-model" in cm._warned_unknown
    cm.add_model("new-model", Decimal("1.00"), Decimal("2.00"))
    assert "new-model" not in cm._warned_unknown


def test_models_list_sorted() -> None:
    cm = DefaultCostModel()
    models = cm.models()
    assert models == sorted(models)


def test_mock_model_nonzero_cost() -> None:
    cm = DefaultCostModel()
    cost = cm.cost("mock-model", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == Decimal("2.00")


# ---------------------------------------------------------------------------
# Pricing override: env var path
# ---------------------------------------------------------------------------


def test_env_var_override_loads_custom_pricing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(json.dumps({"test-model-override": ["5.00", "10.00", "0.00", "0.50"]}))
    monkeypatch.setenv("FORGE_PRICING_TABLE_PATH", str(pricing_file))

    table = _load_pricing_table()
    assert "test-model-override" in table
    assert table["test-model-override"][0] == Decimal("5.00")


def test_env_var_override_invalid_json_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not valid json{{")
    monkeypatch.setenv("FORGE_PRICING_TABLE_PATH", str(bad_file))

    # Should fall back to bundled table without raising
    table = _load_pricing_table()
    assert "claude-opus-4-20250514" in table


def test_missing_env_var_path_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_PRICING_TABLE_PATH", "/nonexistent/path/pricing.json")
    table = _load_pricing_table()
    assert "claude-opus-4-20250514" in table


# ---------------------------------------------------------------------------
# Local cache
# ---------------------------------------------------------------------------


def test_save_and_load_pricing_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "forge_observe.cost_model._local_cache_path",
        lambda: tmp_path / "pricing.json",
    )
    monkeypatch.setenv("FORGE_PRICING_TABLE_PATH", "")

    sample = {"cached-model": ["2.00", "4.00", "0.00", "0.20"]}
    path = save_pricing_cache(sample)
    assert path.exists()

    table = _load_pricing_table()
    assert "cached-model" in table


def test_stale_cache_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime

    cache_path = tmp_path / "pricing.json"
    sample = {"stale-model": ["1.00", "2.00", "0.00", "0.10"]}
    cache_path.write_text(json.dumps(sample))

    # Set mtime to 30 days ago
    old_mtime = (datetime.datetime.now() - datetime.timedelta(days=30)).timestamp()
    os.utime(cache_path, (old_mtime, old_mtime))

    monkeypatch.setattr(
        "forge_observe.cost_model._local_cache_path",
        lambda: cache_path,
    )
    monkeypatch.setenv("FORGE_PRICING_TABLE_PATH", "")

    table = _load_pricing_table()
    # Stale cache should be skipped → bundled table used
    assert "stale-model" not in table
    assert "claude-opus-4-20250514" in table


# ---------------------------------------------------------------------------
# _parse_pricing_json
# ---------------------------------------------------------------------------


def test_parse_pricing_json_four_values() -> None:
    raw = {"m1": ["1.00", "2.00", "0.50", "0.10"]}
    result = _parse_pricing_json(raw)
    assert result["m1"] == (Decimal("1.00"), Decimal("2.00"), Decimal("0.50"), Decimal("0.10"))


def test_parse_pricing_json_two_values_defaults_zeros() -> None:
    raw = {"m2": ["3.00", "6.00"]}
    result = _parse_pricing_json(raw)
    assert result["m2"] == (Decimal("3.00"), Decimal("6.00"), Decimal("0"), Decimal("0"))


def test_parse_pricing_json_skips_short_entries() -> None:
    raw = {"bad": ["1.00"]}
    result = _parse_pricing_json(raw)
    assert "bad" not in result


# ---------------------------------------------------------------------------
# _is_fresh
# ---------------------------------------------------------------------------


def test_is_fresh_new_file(tmp_path: Path) -> None:
    f = tmp_path / "fresh.json"
    f.write_text("{}")
    assert _is_fresh(f, max_age_days=7) is True


def test_is_fresh_nonexistent_file(tmp_path: Path) -> None:
    assert _is_fresh(tmp_path / "missing.json", max_age_days=7) is False


# ---------------------------------------------------------------------------
# BUNDLED_PRICING_TABLE
# ---------------------------------------------------------------------------


def test_bundled_table_is_json_serialisable() -> None:
    # BUNDLED_PRICING_TABLE must be a plain dict[str, list[str]] for forge doctor
    assert isinstance(BUNDLED_PRICING_TABLE, dict)
    for model, vals in BUNDLED_PRICING_TABLE.items():
        assert isinstance(model, str)
        assert isinstance(vals, list)
        for v in vals:
            assert isinstance(v, str)
            Decimal(v)  # must be valid Decimal strings


def test_bundled_table_includes_all_bundled_models() -> None:
    from forge_observe.cost_model import _MODEL_PRICING

    for model in _MODEL_PRICING:
        assert model in BUNDLED_PRICING_TABLE
