"""Tests for Forge core type system."""

from decimal import Decimal

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
