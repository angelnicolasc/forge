"""Model pricing registry and cost calculation.

Maintains a registry of per-token pricing for popular LLM models.
Used by the metrics collector for real-time cost tracking and by
the evolution loop for cost optimization decisions.

Pricing table has four dimensions per model:
  (input_per_m, output_per_m, thinking_per_m, cache_read_per_m)

Models that do not support extended thinking carry ``Decimal("0")`` in the
thinking slot. Cache-read price defaults to 10% of input price when not
explicitly specified.

When an unknown model is encountered, the model returns ``Decimal("0")``
but emits a structured warning exactly once per unique model name so
upstream systems (dashboard, tests) can detect silent mis-instrumentation.

Override the bundled table at runtime via ``FORGE_PRICING_TABLE_PATH``
(absolute path to a JSON file) or via the ``~/.forge/pricing.json`` local
cache updated by ``forge doctor --update-pricing``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Pricing as of May 2026 (USD per 1M tokens).
# Schema: (input, output, thinking, cache_read) — all per 1M tokens.
# Providers without extended thinking or prompt caching carry Decimal("0").
_MODEL_PRICING: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {
    # Anthropic — thinking_per_m based on published extended-thinking pricing
    "claude-opus-4-20250514": (
        Decimal("15.00"), Decimal("75.00"), Decimal("3.00"), Decimal("1.50"),
    ),
    "claude-sonnet-4-20250514": (
        Decimal("3.00"), Decimal("15.00"), Decimal("0.00"), Decimal("0.30"),
    ),
    "claude-haiku-4-20250514": (
        Decimal("0.80"), Decimal("4.00"), Decimal("0.00"), Decimal("0.08"),
    ),
    # OpenAI
    "gpt-4o": (Decimal("2.50"), Decimal("10.00"), Decimal("0.00"), Decimal("0.25")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60"), Decimal("0.00"), Decimal("0.015")),
    "gpt-4.1": (Decimal("2.00"), Decimal("8.00"), Decimal("0.00"), Decimal("0.20")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60"), Decimal("0.00"), Decimal("0.04")),
    "gpt-4.1-nano": (Decimal("0.10"), Decimal("0.40"), Decimal("0.00"), Decimal("0.01")),
    "o3": (Decimal("10.00"), Decimal("40.00"), Decimal("10.00"), Decimal("1.00")),
    "o3-mini": (Decimal("1.10"), Decimal("4.40"), Decimal("1.10"), Decimal("0.11")),
    "o4-mini": (Decimal("1.10"), Decimal("4.40"), Decimal("1.10"), Decimal("0.11")),
    # Google
    "gemini-2.5-pro": (Decimal("1.25"), Decimal("10.00"), Decimal("0.00"), Decimal("0.125")),
    "gemini-2.5-flash": (Decimal("0.15"), Decimal("0.60"), Decimal("0.00"), Decimal("0.015")),
    "gemini-2.0-flash": (Decimal("0.10"), Decimal("0.40"), Decimal("0.00"), Decimal("0.01")),
    # Meta (via API providers)
    "llama-4-maverick": (Decimal("0.20"), Decimal("0.60"), Decimal("0.00"), Decimal("0.00")),
    "llama-4-scout": (Decimal("0.10"), Decimal("0.30"), Decimal("0.00"), Decimal("0.00")),
    # DeepSeek
    "deepseek-r1": (Decimal("0.55"), Decimal("2.19"), Decimal("0.55"), Decimal("0.055")),
    "deepseek-v3": (Decimal("0.27"), Decimal("1.10"), Decimal("0.00"), Decimal("0.027")),
    # Test / demo stub — priced at $1/1M tokens so E2E fixtures using
    # MockLLMProvider or fake SDK modules surface a non-zero TOTAL in
    # the Rich output. Kept here (not in a test helper) so
    # `forge wrap` against mock-driven examples produces realistic
    # cost display even in CI.
    "mock-model": (Decimal("1.00"), Decimal("1.00"), Decimal("0.00"), Decimal("0.10")),
}

_PER_MILLION = Decimal("1000000")
_PRICING_CACHE_MAX_AGE_DAYS = 7


def _pricing_override_path() -> Path:
    raw = os.getenv("FORGE_PRICING_TABLE_PATH", "").strip()
    return Path(raw) if raw else Path()


def _local_cache_path() -> Path:
    return Path.home() / ".forge" / "pricing.json"


def _is_fresh(path: Path, max_age_days: int = _PRICING_CACHE_MAX_AGE_DAYS) -> bool:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return datetime.now(UTC) - mtime < timedelta(days=max_age_days)
    except OSError:
        return False


def _parse_pricing_json(
    raw: dict[str, list[str]],
) -> dict[str, tuple[Decimal, Decimal, Decimal, Decimal]]:
    out: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    for model, vals in raw.items():
        if len(vals) >= 4:
            out[model] = (
                Decimal(vals[0]), Decimal(vals[1]), Decimal(vals[2]), Decimal(vals[3]),
            )
        elif len(vals) == 2:
            out[model] = (Decimal(vals[0]), Decimal(vals[1]), Decimal("0"), Decimal("0"))
    return out


def _load_pricing_table() -> dict[str, tuple[Decimal, Decimal, Decimal, Decimal]]:
    """Load pricing with override priority: env-var path → local cache → bundled."""
    override = _pricing_override_path()
    if override.name and override.exists():
        try:
            return _parse_pricing_json(json.loads(override.read_text()))
        except Exception:
            logger.warning("cost_model.pricing_override_invalid", path=str(override))

    local = _local_cache_path()
    if local.exists() and _is_fresh(local):
        try:
            return _parse_pricing_json(json.loads(local.read_text()))
        except Exception:
            logger.warning("cost_model.local_cache_invalid", path=str(local))

    return dict(_MODEL_PRICING)


class DefaultCostModel:
    """Default cost model using known model pricing.

    Supports four token dimensions: input, output, thinking (extended
    reasoning), and cached_input (prompt-cache read hits). Adapters
    that do not report thinking or cached tokens pass ``0`` — the cost
    calculation degrades gracefully to the two-dimension formula.
    """

    def __init__(
        self,
        custom_pricing: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] | None = None,
    ) -> None:
        self._pricing = _load_pricing_table()
        if custom_pricing:
            self._pricing.update(custom_pricing)
        # Track which model names we've already warned about so we don't
        # spam logs on every LLM call. Reset if pricing is extended via
        # add_model().
        self._warned_unknown: set[str] = set()

    def cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        thinking_tokens: int = 0,
        cached_input_tokens: int = 0,
    ) -> Decimal:
        """Calculate cost for a single LLM call.

        ``thinking_tokens`` are billed at the model's thinking price (lower
        than output for Anthropic models). ``cached_input_tokens`` are billed
        at the cache-read price (~10% of regular input price).

        Returns ``Decimal("0")`` for unknown models and emits a structured
        ``cost_model.unknown_model`` warning exactly once per model name.
        """
        pricing = self._resolve_pricing(model)
        if pricing is None:
            if model not in self._warned_unknown:
                self._warned_unknown.add(model)
                logger.warning(
                    "cost_model.unknown_model",
                    model=model,
                    known_models_count=len(self._pricing),
                    action="returning_zero_cost",
                )
            return Decimal("0")

        input_price, output_price, thinking_price, cache_read_price = pricing
        return (
            Decimal(str(input_tokens)) * input_price / _PER_MILLION
            + Decimal(str(output_tokens)) * output_price / _PER_MILLION
            + Decimal(str(thinking_tokens)) * thinking_price / _PER_MILLION
            + Decimal(str(cached_input_tokens)) * cache_read_price / _PER_MILLION
        )

    def models(self) -> list[str]:
        """List all models with known pricing."""
        return sorted(self._pricing.keys())

    def add_model(
        self,
        model: str,
        input_per_m: Decimal,
        output_per_m: Decimal,
        thinking_per_m: Decimal = Decimal("0"),
        cache_read_per_m: Decimal = Decimal("0"),
    ) -> None:
        """Add or update pricing for a model.

        Clears the "already warned" flag for this model so the next call
        (which will now find pricing) doesn't emit a stale warning.
        """
        self._pricing[model] = (input_per_m, output_per_m, thinking_per_m, cache_read_per_m)
        self._warned_unknown.discard(model)

    def _resolve_pricing(
        self, model: str,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        """Resolve pricing, trying exact match first then prefix match."""
        if model in self._pricing:
            return self._pricing[model]

        # Prefix matching: "claude-sonnet-4" matches "claude-sonnet-4-20250514"
        for known_model, pricing in self._pricing.items():
            if known_model.startswith(model) or model.startswith(known_model):
                return pricing

        return None


def save_pricing_cache(pricing: dict[str, list[str]]) -> Path:
    """Write a pricing dict to the local cache file (~/.forge/pricing.json).

    Called by ``forge doctor --update-pricing`` after downloading fresh prices.
    Returns the path written.
    """
    cache_path = _local_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(pricing, indent=2))
    return cache_path


# Expose the bundled table as JSON-serialisable for ``forge doctor --update-pricing``
# to compare against a remote source without importing the full class.
BUNDLED_PRICING_TABLE: dict[str, list[str]] = {
    model: [str(v) for v in vals]
    for model, vals in _MODEL_PRICING.items()
}
