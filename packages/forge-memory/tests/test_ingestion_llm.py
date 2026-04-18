"""Tests for LLM-based entity extraction (fase δ.4).

These tests pin down the three-way decision tree in
:func:`_extract_entities_async`:

1. No client OR flag off → regex fallback (byte-identical to legacy).
2. Flag on + working client → LLM output used verbatim (deduped).
3. Flag on + broken client → regex fallback, no exception leaks.
"""

from __future__ import annotations

import pytest

from forge_memory.ingestion import (
    EntityExtractionTool,
    IngestionPipeline,
    _extract_entities,
    _extract_entities_async,
)


class _StubLLM:
    def __init__(self, entities: list[str]) -> None:
        self._entities = entities
        self.calls = 0

    async def structured_call(self, *, schema, **_kwargs):
        self.calls += 1
        return schema(entities=self._entities)


class _ExplodingLLM:
    async def structured_call(self, **_kwargs):
        raise RuntimeError("API down")


_SAMPLE = "OpenAI released GPT-4 alongside ResearchAgent. Claude helps too."


# ---------------------------------------------------------------------------
# Regex fallback paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_client_uses_regex(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_LLM_ENTITY_EXTRACTION", "1")
    result = await _extract_entities_async(_SAMPLE, llm_client=None)
    # Regex finds the acronyms / agent-suffix tokens it always has.
    assert result == _extract_entities(_SAMPLE)


@pytest.mark.asyncio
async def test_flag_off_uses_regex_even_with_client(monkeypatch) -> None:
    monkeypatch.delenv("FORGE_ENABLE_LLM_ENTITY_EXTRACTION", raising=False)
    llm = _StubLLM(["should_not_appear"])
    result = await _extract_entities_async(_SAMPLE, llm_client=llm)
    assert llm.calls == 0
    assert "should_not_appear" not in result


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_extraction_used_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_LLM_ENTITY_EXTRACTION", "1")
    llm = _StubLLM(["OpenAI", "GPT-4", "ResearchAgent", "Claude"])
    result = await _extract_entities_async(_SAMPLE, llm_client=llm)
    assert llm.calls == 1
    assert result == ["OpenAI", "GPT-4", "ResearchAgent", "Claude"]


@pytest.mark.asyncio
async def test_llm_extraction_dedupes_case_variants(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_LLM_ENTITY_EXTRACTION", "1")
    llm = _StubLLM(["OpenAI", "openai", "GPT-4", "gpt-4", "Claude"])
    result = await _extract_entities_async(_SAMPLE, llm_client=llm)
    # First-seen wins — canonical casing preserved in order.
    assert result == ["OpenAI", "GPT-4", "Claude"]


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_regex(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_LLM_ENTITY_EXTRACTION", "1")
    result = await _extract_entities_async(_SAMPLE, llm_client=_ExplodingLLM())
    # Regex path: identical to the no-client case. Nothing leaks.
    assert result == _extract_entities(_SAMPLE)


# ---------------------------------------------------------------------------
# Pipeline wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_routes_through_llm_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_LLM_ENTITY_EXTRACTION", "1")
    llm = _StubLLM(["FirstEntity", "SecondEntity"])
    pipeline = IngestionPipeline(llm_client=llm)
    entries = await pipeline.ingest(content=_SAMPLE, source_run_id="r1")
    assert len(entries) == 1
    assert entries[0].entities == ["FirstEntity", "SecondEntity"]


def test_schema_caps_entity_count() -> None:
    # >40 should reject — prevents runaway LLM responses inflating storage.
    with pytest.raises(Exception):
        EntityExtractionTool(entities=[f"e{i}" for i in range(100)])
