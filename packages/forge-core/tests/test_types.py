"""Tests for Forge core type system."""

from decimal import Decimal

import pytest

from forge_core.types import (
    AgentCard,
    CostSummary,
    MemoryEntry,
    MemoryQuery,
    Mutation,
    MutationDiff,
    MutationKind,
    RunConfig,
    RunEvent,
    RunEventKind,
    RunResult,
    RunStatus,
    TaskEnvelope,
    ToolRef,
)


class TestAgentCard:
    def test_defaults(self) -> None:
        card = AgentCard(name="researcher", role="Research agent")
        assert card.name == "researcher"
        assert card.role == "Research agent"
        assert len(card.id) == 12
        assert card.tools == []
        assert card.capabilities == []

    def test_full_card(self) -> None:
        card = AgentCard(
            name="coder",
            role="Code generation",
            model="claude-sonnet-4-20250514",
            tools=[ToolRef(name="bash", description="Run shell commands")],
            capabilities=["code_generation", "debugging"],
        )
        assert card.model == "claude-sonnet-4-20250514"
        assert len(card.tools) == 1
        assert card.tools[0].name == "bash"


class TestTaskEnvelope:
    def test_defaults(self) -> None:
        envelope = TaskEnvelope(input={"query": "test"})
        assert envelope.input == {"query": "test"}
        assert envelope.config.max_steps == 100
        assert envelope.config.cost_ceiling == Decimal("10.00")
        assert len(envelope.task_id) == 32

    def test_custom_config(self) -> None:
        envelope = TaskEnvelope(
            input={"query": "test"},
            config=RunConfig(max_steps=50, cost_ceiling=Decimal("5.00")),
        )
        assert envelope.config.max_steps == 50


class TestRunResult:
    def test_default_result(self) -> None:
        result = RunResult(task_id="abc123")
        assert result.status == RunStatus.COMPLETED
        assert result.cost.total_cost == Decimal("0")
        assert result.events == []

    def test_with_events(self) -> None:
        events = [
            RunEvent(
                kind=RunEventKind.LLM_CALL, model="gpt-4o", input_tokens=100, output_tokens=50
            ),
            RunEvent(kind=RunEventKind.TOOL_CALL, tool_name="search"),
        ]
        result = RunResult(task_id="abc", events=events)
        assert len(result.events) == 2
        assert result.events[0].model == "gpt-4o"


class TestMutation:
    def test_mutation_creation(self) -> None:
        m = Mutation(
            kind=MutationKind.PROMPT_REWRITE,
            description="Simplify researcher prompt",
            confidence=0.85,
            diff=[
                MutationDiff(
                    field_path="agents[id=researcher].system_prompt",
                    before="...",
                    after="...",
                    description="Simplify prompt",
                )
            ],
        )
        assert m.kind == MutationKind.PROMPT_REWRITE
        assert m.confidence == 0.85
        assert not m.applied
        assert len(m.diff) == 1
        assert m.diff[0].field_path == "agents[id=researcher].system_prompt"


class TestMemoryEntry:
    def test_defaults(self) -> None:
        entry = MemoryEntry(content="The API rate limit is 100 req/min")
        assert entry.version == 1
        assert entry.entities == []
        assert entry.supersedes is None

    def test_with_relations(self) -> None:
        entry = MemoryEntry(
            content="Agent-1 uses tool-search",
            entities=["agent-1", "tool-search"],
            relations=[("agent-1", "uses", "tool-search")],
            source_run_id="run-123",
        )
        assert len(entry.relations) == 1


class TestMemoryQuery:
    def test_text_query(self) -> None:
        q = MemoryQuery(text="rate limits")
        assert q.top_k == 10
        assert q.min_score == 0.0

    def test_filtered_query(self) -> None:
        q = MemoryQuery(
            text="cost optimization",
            entity_filter=["agent-1"],
            tag_filter={"domain": "finops"},
            top_k=5,
        )
        assert q.entity_filter == ["agent-1"]


class TestCostSummary:
    def test_defaults(self) -> None:
        s = CostSummary()
        assert s.total_cost == Decimal("0")
        assert s.llm_calls == 0
        assert s.cost_by_model == {}


# ---------------------------------------------------------------------------
# Fase 0 additions
# ---------------------------------------------------------------------------


class TestAgentCardSpiffe:
    def test_spiffe_id_none_by_default(self) -> None:
        card = AgentCard(name="agent")
        assert card.spiffe_id is None

    def test_valid_spiffe_uri(self) -> None:
        card = AgentCard(
            name="agent",
            spiffe_id="spiffe://trust.example.com/ns/default/sa/worker",
        )
        assert card.spiffe_id == "spiffe://trust.example.com/ns/default/sa/worker"

    def test_invalid_scheme_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentCard(name="agent", spiffe_id="https://trust.example.com/ns/sa")

    def test_missing_workload_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentCard(name="agent", spiffe_id="spiffe://trust.example.com/")

    def test_missing_trust_domain_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentCard(name="agent", spiffe_id="spiffe:///workload")

    def test_non_string_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentCard(name="agent", spiffe_id=42)  # type: ignore[arg-type]


class TestAgentCardA2A:
    def test_a2a_defaults(self) -> None:
        card = AgentCard(name="agent")
        assert card.url is None
        assert card.version == "0.1.0"
        assert card.provider == {}

    def test_a2a_fields_set(self) -> None:
        card = AgentCard(
            name="orchestrator",
            url="https://forge.example.com",
            version="1.2.3",
            provider={"name": "ACME Corp", "url": "https://acme.example.com"},
        )
        assert card.url == "https://forge.example.com"
        assert card.version == "1.2.3"
        assert card.provider["name"] == "ACME Corp"

    def test_model_dump_exclude_none(self) -> None:
        card = AgentCard(name="agent")
        data = card.model_dump(mode="json", exclude_none=True)
        assert "url" not in data
        assert "spiffe_id" not in data
        assert "model" not in data
        assert "system_prompt" not in data


class TestRunStatusInputRequired:
    def test_input_required_value(self) -> None:
        assert RunStatus.INPUT_REQUIRED == "input-required"

    def test_run_result_with_input_required(self) -> None:
        result = RunResult(task_id="t1", status=RunStatus.INPUT_REQUIRED)
        assert result.status == RunStatus.INPUT_REQUIRED


class TestRunEventContextInjected:
    def test_context_injected_kind_exists(self) -> None:
        assert RunEventKind.CONTEXT_INJECTED == "context_injected"

    def test_run_event_with_context_tokens(self) -> None:
        event = RunEvent(
            kind=RunEventKind.CONTEXT_INJECTED,
            context_tokens=512,
            data={"source": "rules", "rule_count": 3},
        )
        assert event.context_tokens == 512
        assert event.kind == RunEventKind.CONTEXT_INJECTED

    def test_context_tokens_default_zero(self) -> None:
        event = RunEvent(kind=RunEventKind.LLM_CALL)
        assert event.context_tokens == 0
