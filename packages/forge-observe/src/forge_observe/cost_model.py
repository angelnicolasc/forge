"""Model pricing registry and cost calculation.

Maintains a registry of per-token pricing for popular LLM models.
Used by the metrics collector for real-time cost tracking and by
the evolution loop for cost optimization decisions.

When an unknown model is encountered, the model returns ``Decimal("0")``
but emits a structured warning exactly once per unique model name so
upstream systems (dashboard, tests) can detect silent mis-instrumentation
— see L7/L8 plan α.5 ("Fallback a $0.00 con warning estructurado").
"""

from __future__ import annotations

from decimal import Decimal

import structlog

logger = structlog.get_logger()

# Pricing as of April 2026 (USD per 1M tokens)
_MODEL_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    # Anthropic
    "claude-opus-4-20250514": (Decimal("15.00"), Decimal("75.00")),
    "claude-sonnet-4-20250514": (Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-20250514": (Decimal("0.80"), Decimal("4.00")),
    # OpenAI
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4.1": (Decimal("2.00"), Decimal("8.00")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
    "gpt-4.1-nano": (Decimal("0.10"), Decimal("0.40")),
    "o3": (Decimal("10.00"), Decimal("40.00")),
    "o3-mini": (Decimal("1.10"), Decimal("4.40")),
    "o4-mini": (Decimal("1.10"), Decimal("4.40")),
    # Google
    "gemini-2.5-pro": (Decimal("1.25"), Decimal("10.00")),
    "gemini-2.5-flash": (Decimal("0.15"), Decimal("0.60")),
    "gemini-2.0-flash": (Decimal("0.10"), Decimal("0.40")),
    # Meta (via API providers)
    "llama-4-maverick": (Decimal("0.20"), Decimal("0.60")),
    "llama-4-scout": (Decimal("0.10"), Decimal("0.30")),
    # DeepSeek
    "deepseek-r1": (Decimal("0.55"), Decimal("2.19")),
    "deepseek-v3": (Decimal("0.27"), Decimal("1.10")),
    # Test / demo stub — priced at $1/1M tokens so E2E fixtures using
    # MockLLMProvider or fake SDK modules surface a non-zero TOTAL in
    # the Rich output. Kept here (not in a test helper) so
    # `forge wrap` against mock-driven examples produces realistic
    # cost display even in CI.
    "mock-model": (Decimal("1.00"), Decimal("1.00")),
}

_PER_MILLION = Decimal("1000000")


class DefaultCostModel:
    """Default cost model using known model pricing."""

    def __init__(self, custom_pricing: dict[str, tuple[Decimal, Decimal]] | None = None) -> None:
        self._pricing = dict(_MODEL_PRICING)
        if custom_pricing:
            self._pricing.update(custom_pricing)
        # Track which model names we've already warned about so we don't
        # spam logs on every LLM call. Reset if pricing is extended via
        # add_model().
        self._warned_unknown: set[str] = set()

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        """Calculate cost for a single LLM call.

        Returns ``Decimal("0")`` for unknown models and emits a structured
        ``cost_model.unknown_model`` warning exactly once per model name.
        This is deliberate: we prefer a loud, loggable zero to a silent
        guess that would corrupt fitness scores downstream.
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

        input_price, output_price = pricing
        return (
            Decimal(str(input_tokens)) * input_price / _PER_MILLION
            + Decimal(str(output_tokens)) * output_price / _PER_MILLION
        )

    def models(self) -> list[str]:
        """List all models with known pricing."""
        return sorted(self._pricing.keys())

    def add_model(self, model: str, input_per_m: Decimal, output_per_m: Decimal) -> None:
        """Add or update pricing for a model.

        Clears the "already warned" flag for this model so the next call
        (which will now find pricing) doesn't emit a stale warning.
        """
        self._pricing[model] = (input_per_m, output_per_m)
        self._warned_unknown.discard(model)

    def _resolve_pricing(self, model: str) -> tuple[Decimal, Decimal] | None:
        """Resolve pricing, trying exact match first then prefix match."""
        if model in self._pricing:
            return self._pricing[model]

        # Try prefix matching (e.g., "claude-sonnet-4" matches "claude-sonnet-4-20250514")
        for known_model, pricing in self._pricing.items():
            if known_model.startswith(model) or model.startswith(known_model):
                return pricing

        return None
