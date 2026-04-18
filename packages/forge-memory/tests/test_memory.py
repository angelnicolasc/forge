"""Tests for Forge Living Collaborative Memory."""

from __future__ import annotations

import pytest

from forge_core.types import MemoryEntry
from forge_memory.hybrid import _reciprocal_rank_fusion
from forge_memory.ingestion import IngestionPipeline, _extract_entities
from forge_memory.symbolic.rules import RuleAction, RuleEngine
from forge_memory.versioning import MemoryVersionStore

# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


class TestRRF:
    def _make_entries(self, ids: list[str]) -> list[tuple[MemoryEntry, float]]:
        return [
            (MemoryEntry(id=i, content=f"content {i}"), 1.0 / (j + 1)) for j, i in enumerate(ids)
        ]

    def test_single_list_passthrough(self) -> None:
        entries = self._make_entries(["a", "b", "c"])
        result = _reciprocal_rank_fusion([(entries, 1.0)])
        assert [e.id for e, _ in result] == ["a", "b", "c"]

    def test_two_lists_merged(self) -> None:
        list1 = self._make_entries(["a", "b", "c"])
        list2 = self._make_entries(["c", "a", "d"])
        result = _reciprocal_rank_fusion([(list1, 0.6), (list2, 0.4)])
        # "a" appears at top of both lists — should have high fused score
        ids = [e.id for e, _ in result]
        assert "a" in ids[:2]  # "a" should be in top 2

    def test_entry_only_in_one_list(self) -> None:
        list1 = self._make_entries(["x"])
        list2 = self._make_entries(["y"])
        result = _reciprocal_rank_fusion([(list1, 0.5), (list2, 0.5)])
        ids = {e.id for e, _ in result}
        assert "x" in ids
        assert "y" in ids

    def test_scores_are_positive(self) -> None:
        entries = self._make_entries(["a", "b"])
        result = _reciprocal_rank_fusion([(entries, 1.0)])
        for _, score in result:
            assert score > 0


# ---------------------------------------------------------------------------
# Symbolic Rule Engine
# ---------------------------------------------------------------------------


class TestRuleEngine:
    def test_veto_removes_entry(self) -> None:
        engine = RuleEngine()

        @engine.rule(priority=100)
        def veto_all(entry: MemoryEntry, context: dict) -> RuleAction | None:
            return RuleAction.veto("Always veto")

        entries = [(MemoryEntry(content="test"), 0.9)]
        result = engine.apply(entries)
        assert result == []

    def test_boost_increases_score(self) -> None:
        engine = RuleEngine()

        @engine.rule(priority=100)
        def boost_all(entry: MemoryEntry, context: dict) -> RuleAction | None:
            return RuleAction.boost(0.2)

        entries = [(MemoryEntry(content="test"), 0.5)]
        result = engine.apply(entries)
        assert len(result) == 1
        _, score = result[0]
        assert score == pytest.approx(0.7)

    def test_demote_decreases_score(self) -> None:
        engine = RuleEngine()

        @engine.rule(priority=100)
        def demote_all(entry: MemoryEntry, context: dict) -> RuleAction | None:
            return RuleAction.demote(0.3)

        entries = [(MemoryEntry(content="test"), 0.8)]
        result = engine.apply(entries)
        assert len(result) == 1
        _, score = result[0]
        assert score == pytest.approx(0.5)

    def test_tag_modifies_entry(self) -> None:
        engine = RuleEngine()

        @engine.rule(priority=100)
        def tag_all(entry: MemoryEntry, context: dict) -> RuleAction | None:
            return RuleAction.tag(processed="true")

        entry = MemoryEntry(content="test", tags={})
        result = engine.apply([(entry, 0.5)])
        modified_entry, _ = result[0]
        assert modified_entry.tags.get("processed") == "true"

    def test_priority_order_respected(self) -> None:
        engine = RuleEngine()
        fired: list[str] = []

        @engine.rule(priority=10)
        def low_priority(entry: MemoryEntry, context: dict) -> RuleAction | None:
            fired.append("low")
            return None

        @engine.rule(priority=100)
        def high_priority(entry: MemoryEntry, context: dict) -> RuleAction | None:
            fired.append("high")
            return None

        engine.apply([(MemoryEntry(content="test"), 0.5)])
        assert fired == ["high", "low"]

    def test_disable_enable_rule(self) -> None:
        engine = RuleEngine()

        @engine.rule(priority=100)
        def always_veto(entry: MemoryEntry, context: dict) -> RuleAction | None:
            return RuleAction.veto()

        entries = [(MemoryEntry(content="test"), 0.9)]
        assert engine.apply(entries) == []

        engine.disable_rule("always_veto")
        assert len(engine.apply(entries)) == 1

        engine.enable_rule("always_veto")
        assert engine.apply(entries) == []


# ---------------------------------------------------------------------------
# Ingestion Pipeline
# ---------------------------------------------------------------------------


class TestIngestionPipeline:
    @pytest.mark.asyncio
    async def test_short_content_single_chunk(self) -> None:
        pipeline = IngestionPipeline(chunk_size=1000)
        entries = await pipeline.ingest("This is a short text.")
        assert len(entries) == 1
        assert entries[0].content == "This is a short text."

    @pytest.mark.asyncio
    async def test_long_content_multiple_chunks(self) -> None:
        pipeline = IngestionPipeline(chunk_size=100, chunk_overlap=20)
        content = "A" * 350
        entries = await pipeline.ingest(content)
        assert len(entries) > 1

    @pytest.mark.asyncio
    async def test_metadata_propagation(self) -> None:
        pipeline = IngestionPipeline()
        entries = await pipeline.ingest(
            "Test content",
            source_run_id="run-123",
            source_agent_id="researcher",
            tags={"domain": "AI"},
        )
        assert entries[0].source_run_id == "run-123"
        assert entries[0].source_agent_id == "researcher"
        assert entries[0].tags == {"domain": "AI"}

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty(self) -> None:
        pipeline = IngestionPipeline()
        entries = await pipeline.ingest("")
        assert entries == []

    def test_entity_extraction_finds_acronyms(self) -> None:
        entities = _extract_entities("The LLM used RAG and GPT-4 in the pipeline.")
        assert "LLM" in entities
        assert "RAG" in entities

    def test_entity_extraction_finds_model_names(self) -> None:
        entities = _extract_entities("We tested gpt-4o-mini and claude-haiku-4-20250514.")
        model_entities = [e for e in entities if "gpt" in e.lower() or "claude" in e.lower()]
        assert len(model_entities) > 0


# ---------------------------------------------------------------------------
# Version Store
# ---------------------------------------------------------------------------


class TestMemoryVersionStore:
    def test_next_version_increments(self, tmp_path: object) -> None:
        store = MemoryVersionStore(persist_path=f"{tmp_path}/versions.jsonl")
        entry = MemoryEntry(id="entry-1", content="original")
        versioned = store.next_version(entry)
        assert versioned.version == 1

        store.record(versioned, operation="store")
        next_v = store.next_version(versioned)
        assert next_v.version == 2

    def test_lineage_follows_chain(self, tmp_path: object) -> None:
        store = MemoryVersionStore(persist_path=f"{tmp_path}/versions.jsonl")
        v1 = MemoryEntry(id="entry-1", content="v1", version=1)
        store.record(v1, operation="store")

        v2 = v1.model_copy(update={"version": 2, "supersedes": v1.id, "content": "v2"})
        store.record(v2, operation="store")

        lineage = store.lineage("entry-1")
        assert len(lineage) >= 2

    def test_stats_count_writes(self, tmp_path: object) -> None:
        store = MemoryVersionStore(persist_path=f"{tmp_path}/versions.jsonl")
        for i in range(3):
            entry = MemoryEntry(id=f"entry-{i}", content=f"content {i}")
            store.record(entry)
        stats = store.stats()
        assert stats["total_writes"] == 3
        assert stats["unique_entries"] == 3
