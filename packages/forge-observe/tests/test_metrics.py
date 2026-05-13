"""Unit tests for MetricsCollector — covers properties, failed runs,
cost_by_model, event kinds, cost_savings_estimate, and summary_dict."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from forge_core.types import (
    CostSummary,
    RunEvent,
    RunEventKind,
    RunResult,
    RunStatus,
)
from forge_observe.metrics import MetricsCollector


def _run(
    *,
    status: RunStatus = RunStatus.COMPLETED,
    events: list[RunEvent] | None = None,
    cost: CostSummary | None = None,
) -> RunResult:
    return RunResult(
        task_id=uuid4().hex,
        run_id=uuid4().hex,
        status=status,
        duration_ms=10.0,
        events=events or [],
        cost=cost or CostSummary(),
    )


def test_label_sanitizer_property() -> None:
    collector = MetricsCollector()
    assert collector.label_sanitizer is not None


def test_export_labels() -> None:
    collector = MetricsCollector()
    result = collector.export_labels({"agent_id": "planner"})
    assert isinstance(result, dict)


def test_runs_property_empty() -> None:
    collector = MetricsCollector()
    assert collector.runs == []


def test_runs_property_after_record() -> None:
    collector = MetricsCollector()
    collector.record_run(_run())
    assert len(collector.runs) == 1


def test_record_failed_run() -> None:
    collector = MetricsCollector()
    collector.record_run(_run(status=RunStatus.FAILED))
    assert collector.session.failed_runs == 1
    assert collector.session.successful_runs == 0


def test_cost_by_model_updated() -> None:
    collector = MetricsCollector()
    cost = CostSummary(
        total_cost=Decimal("0.10"),
        cost_by_model={"gpt-4o": Decimal("0.10")},
    )
    collector.record_run(_run(cost=cost))
    assert "gpt-4o" in collector.session.cost_by_model
    assert collector.session.cost_by_model["gpt-4o"] == Decimal("0.10")


def test_process_event_no_agent_id_skipped() -> None:
    collector = MetricsCollector()
    event = RunEvent(kind=RunEventKind.LLM_CALL, agent_id=None, input_tokens=10)
    collector.record_run(_run(events=[event]))
    assert collector.session.agent_metrics == {}


def test_process_event_tool_call() -> None:
    collector = MetricsCollector()
    event = RunEvent(kind=RunEventKind.TOOL_CALL, agent_id="planner")
    collector.record_run(_run(events=[event]))
    assert collector.session.agent_metrics["planner"].tool_calls == 1


def test_process_event_tool_result() -> None:
    collector = MetricsCollector()
    event = RunEvent(kind=RunEventKind.TOOL_RESULT, agent_id="planner")
    collector.record_run(_run(events=[event]))
    assert collector.session.agent_metrics["planner"].tool_calls == 1


def test_process_event_error() -> None:
    collector = MetricsCollector()
    event = RunEvent(kind=RunEventKind.ERROR, agent_id="planner")
    collector.record_run(_run(events=[event]))
    assert collector.session.agent_metrics["planner"].errors == 1


def test_cost_savings_estimate_finds_cheaper_alternative() -> None:
    collector = MetricsCollector()
    cost = CostSummary(
        total_cost=Decimal("5.00"),
        total_input_tokens=500_000,
        total_output_tokens=100_000,
        cost_by_model={"claude-opus-4-20250514": Decimal("5.00")},
    )
    collector.record_run(_run(cost=cost))
    savings = collector.cost_savings_estimate()
    assert any("claude-opus-4-20250514" in k for k in savings)
    assert all(v > 0 for v in savings.values())


def test_cost_savings_estimate_no_known_alternative() -> None:
    collector = MetricsCollector()
    cost = CostSummary(
        total_cost=Decimal("0.01"),
        cost_by_model={"mock-model": Decimal("0.01")},
    )
    collector.record_run(_run(cost=cost))
    assert collector.cost_savings_estimate() == {}


def test_cost_savings_estimate_gpt4o_to_mini() -> None:
    collector = MetricsCollector()
    cost = CostSummary(
        total_cost=Decimal("2.50"),
        total_input_tokens=1_000_000,
        total_output_tokens=0,
        cost_by_model={"gpt-4o": Decimal("2.50")},
    )
    collector.record_run(_run(cost=cost))
    savings = collector.cost_savings_estimate()
    assert "gpt-4o -> gpt-4o-mini" in savings


def test_summary_dict_structure() -> None:
    collector = MetricsCollector()
    collector.record_run(_run(status=RunStatus.COMPLETED))
    d = collector.summary_dict()
    assert d["total_runs"] == 1
    assert d["successful_runs"] == 1
    assert d["failed_runs"] == 0
    assert "total_cost_usd" in d
    assert "agents" in d
    assert isinstance(d["cost_by_model"], dict)


def test_summary_dict_with_agents() -> None:
    collector = MetricsCollector()
    event = RunEvent(kind=RunEventKind.LLM_CALL, agent_id="writer", input_tokens=10)
    collector.record_run(_run(events=[event]))
    d = collector.summary_dict()
    assert "writer" in d["agents"]
    assert "llm_calls" in d["agents"]["writer"]
