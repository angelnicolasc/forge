"""Real-time metrics collection for Forge runs.

Aggregates per-run, per-agent, and per-model metrics including
token counts, costs, latencies, and success rates.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from forge_core.types import RunEvent, RunEventKind, RunResult
from forge_observe.cost_model import DefaultCostModel
from forge_observe.labels import LabelSanitizer


@dataclass
class AgentMetrics:
    """Aggregated metrics for a single agent."""

    name: str
    runs: int = 0
    total_cost: Decimal = Decimal("0")
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    errors: int = 0


@dataclass
class SessionMetrics:
    """Aggregated metrics across all runs in a session."""

    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    total_cost: Decimal = Decimal("0")
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_duration_ms: float = 0.0
    cost_by_model: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    agent_metrics: dict[str, AgentMetrics] = field(default_factory=dict)


class MetricsCollector:
    """Collects and aggregates metrics from Forge runs."""

    def __init__(
        self,
        cost_model: DefaultCostModel | None = None,
        *,
        label_sanitizer: LabelSanitizer | None = None,
    ) -> None:
        self._cost_model = cost_model or DefaultCostModel()
        self._session = SessionMetrics()
        self._runs: list[RunResult] = []
        # A sanitizer bounds the agent_id dimension — without it a runaway
        # caller can turn agent_metrics into a per-run dict and blow the
        # metrics store at scale. Default to the canonical allowlist.
        self._label_sanitizer = label_sanitizer or LabelSanitizer()

    @property
    def label_sanitizer(self) -> LabelSanitizer:
        return self._label_sanitizer

    def export_labels(self, labels: dict[str, str | None]) -> dict[str, str]:
        """Public sanitization hook for OTel/Prometheus exporters."""
        return self._label_sanitizer.sanitize(labels)

    @property
    def session(self) -> SessionMetrics:
        return self._session

    @property
    def runs(self) -> list[RunResult]:
        return list(self._runs)

    def record_run(self, result: RunResult) -> None:
        """Record a complete run and update session metrics."""
        self._runs.append(result)
        s = self._session
        s.total_runs += 1
        s.total_duration_ms += result.duration_ms

        if result.status == "completed":
            s.successful_runs += 1
        else:
            s.failed_runs += 1

        # Process events
        for event in result.events:
            self._process_event(event)

        # Update cost summary
        s.total_cost += result.cost.total_cost
        s.total_input_tokens += result.cost.total_input_tokens
        s.total_output_tokens += result.cost.total_output_tokens

        for model, cost in result.cost.cost_by_model.items():
            s.cost_by_model[model] += cost

    def _process_event(self, event: RunEvent) -> None:
        """Process a single event into agent-level metrics."""
        if not event.agent_id:
            return

        # Sanitize agent_id so that a caller emitting a unique id per run
        # can't grow agent_metrics without bound.
        agent_id = self._label_sanitizer.sanitize({"agent_id": event.agent_id}).get(
            "agent_id", event.agent_id
        )
        if agent_id not in self._session.agent_metrics:
            self._session.agent_metrics[agent_id] = AgentMetrics(name=agent_id)

        am = self._session.agent_metrics[agent_id]

        if event.kind in (RunEventKind.LLM_CALL, RunEventKind.LLM_RESPONSE):
            am.llm_calls += 1
            am.total_input_tokens += event.input_tokens
            am.total_output_tokens += event.output_tokens
            am.total_cost += event.cost
            am.total_latency_ms += event.latency_ms

        elif event.kind in (RunEventKind.TOOL_CALL, RunEventKind.TOOL_RESULT):
            am.tool_calls += 1

        elif event.kind == RunEventKind.ERROR:
            am.errors += 1

    def cost_savings_estimate(self) -> dict[str, Decimal]:
        """Estimate potential cost savings based on model substitution.

        Compares current costs against using cheaper models
        where quality metrics suggest it's viable.
        """
        savings: dict[str, Decimal] = {}
        cheap_alternatives = {
            "gpt-4o": "gpt-4o-mini",
            "claude-opus-4-20250514": "claude-sonnet-4-20250514",
            "claude-sonnet-4-20250514": "claude-haiku-4-20250514",
        }

        for model, cost in self._session.cost_by_model.items():
            alt = cheap_alternatives.get(model)
            if alt:
                alt_cost = self._cost_model.cost(
                    alt,
                    self._session.total_input_tokens,
                    self._session.total_output_tokens,
                )
                potential = cost - alt_cost
                if potential > 0:
                    savings[f"{model} -> {alt}"] = potential

        return savings

    def summary_dict(self) -> dict[str, Any]:
        """Return session metrics as a plain dict for serialization."""
        s = self._session
        return {
            "total_runs": s.total_runs,
            "successful_runs": s.successful_runs,
            "failed_runs": s.failed_runs,
            "total_cost_usd": str(s.total_cost),
            "total_input_tokens": s.total_input_tokens,
            "total_output_tokens": s.total_output_tokens,
            "total_duration_ms": round(s.total_duration_ms, 1),
            "cost_by_model": {k: str(v) for k, v in s.cost_by_model.items()},
            "agents": {
                k: {
                    "llm_calls": v.llm_calls,
                    "tool_calls": v.tool_calls,
                    "cost_usd": str(v.total_cost),
                    "errors": v.errors,
                }
                for k, v in s.agent_metrics.items()
            },
        }
