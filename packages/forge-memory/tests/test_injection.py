"""Tests for :class:`MemoryContextInjector` (fase δ.1).

These tests lock down the contract that closes the memory loop:
**memory that is stored eventually surfaces inside the next run's
envelope.** Anything less and the "Living Collaborative Memory" is a
museum, not a working memory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from forge_core.types import MemoryConfig, MemoryEntry, MemoryQuery, TaskEnvelope
from forge_memory.injection import MemoryContextInjector


class _StubMemory:
    """Deterministic stand-in for :class:`HybridMemory`.

    Returns a pre-seeded result list verbatim and records the last
    :class:`MemoryQuery` it received, so tests can assert on both the
    query the injector formed *and* the envelope it produced.
    """

    def __init__(self, results: list[tuple[MemoryEntry, float]]) -> None:
        self._results = results
        self.last_query: MemoryQuery | None = None

    async def query(self, q: MemoryQuery) -> list[tuple[MemoryEntry, float]]:
        self.last_query = q
        return self._results


def _entry(content: str, **extra: Any) -> MemoryEntry:
    return MemoryEntry(
        id=extra.pop("id", content[:8]),
        content=content,
        valid_from=datetime.now(UTC),
        **extra,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injection_enriches_envelope_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_MEMORY_INJECTION", "1")
    results = [
        (_entry("Previous finding: harnesses reduce cost by 30%"), 0.88),
        (_entry("Secondary finding: latency drops with caching"), 0.71),
    ]
    injector = MemoryContextInjector(
        memory=_StubMemory(results),
        config=MemoryConfig(enable_context_injection=True, top_k=5, min_relevance=0.3),
    )

    env = TaskEnvelope(input={"query": "tell me about agent harnesses"})
    out = await injector.inject(env)

    # memory_context is populated with formatted lines
    assert len(out.memory_context) == 2
    assert "Previous finding" in out.memory_context[0]
    # structured companion in context.forge_memory
    assert "forge_memory" in out.context
    assert out.context["forge_memory"]["count"] == 2
    # metadata counter for observability
    assert out.metadata["memory_hits"] == 2


# ---------------------------------------------------------------------------
# Disabled paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injection_noop_when_config_disabled(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_MEMORY_INJECTION", "1")
    injector = MemoryContextInjector(
        memory=_StubMemory([(_entry("unused"), 0.99)]),
        config=MemoryConfig(enable_context_injection=False),
    )
    env = TaskEnvelope(input={"query": "x"})
    out = await injector.inject(env)
    assert out is env  # unchanged pass-through
    assert out.memory_context == []


@pytest.mark.asyncio
async def test_injection_noop_when_env_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("FORGE_ENABLE_MEMORY_INJECTION", raising=False)
    injector = MemoryContextInjector(
        memory=_StubMemory([(_entry("unused"), 0.99)]),
        config=MemoryConfig(enable_context_injection=True),
    )
    env = TaskEnvelope(input={"query": "x"})
    out = await injector.inject(env)
    # env flag is the kill-switch — config flag alone is insufficient.
    assert out.memory_context == []
    assert "forge_memory" not in out.context


# ---------------------------------------------------------------------------
# Relevance floor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injection_filters_below_min_relevance(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_MEMORY_INJECTION", "1")
    results = [
        (_entry("strong hit"), 0.9),
        (_entry("weak hit"), 0.1),
    ]
    injector = MemoryContextInjector(
        memory=_StubMemory(results),
        config=MemoryConfig(enable_context_injection=True, min_relevance=0.5),
    )
    env = TaskEnvelope(input={"query": "q"})
    out = await injector.inject(env)
    assert out.metadata["memory_hits"] == 1
    assert "strong hit" in out.memory_context[0]


@pytest.mark.asyncio
async def test_injection_noop_when_no_relevant_hits(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_MEMORY_INJECTION", "1")
    injector = MemoryContextInjector(
        memory=_StubMemory([(_entry("weak"), 0.05)]),
        config=MemoryConfig(enable_context_injection=True, min_relevance=0.5),
    )
    env = TaskEnvelope(input={"query": "q"})
    out = await injector.inject(env)
    assert out.memory_context == []
    assert "forge_memory" not in out.context


# ---------------------------------------------------------------------------
# Defensive behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injection_survives_backend_exception(monkeypatch) -> None:
    """Backend hiccup must not fail the user's run — pass through instead."""
    monkeypatch.setenv("FORGE_ENABLE_MEMORY_INJECTION", "1")

    class _ExplodingMemory:
        async def query(self, q: MemoryQuery) -> list[tuple[MemoryEntry, float]]:
            raise RuntimeError("backend down")

    injector = MemoryContextInjector(
        memory=_ExplodingMemory(),
        config=MemoryConfig(enable_context_injection=True),
    )
    env = TaskEnvelope(input={"query": "q"})
    out = await injector.inject(env)
    assert out.memory_context == []


# ---------------------------------------------------------------------------
# Query-text extraction heuristic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_text_extraction_prefers_common_fields(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_MEMORY_INJECTION", "1")
    mem = _StubMemory([(_entry("irrelevant"), 0.9)])
    injector = MemoryContextInjector(
        memory=mem,
        config=MemoryConfig(enable_context_injection=True),
    )
    await injector.inject(TaskEnvelope(input={"question": "what is X?"}))
    assert mem.last_query is not None
    assert mem.last_query.text == "what is X?"


@pytest.mark.asyncio
async def test_query_text_extraction_handles_string_input(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_MEMORY_INJECTION", "1")
    mem = _StubMemory([(_entry("irrelevant"), 0.9)])
    injector = MemoryContextInjector(
        memory=mem,
        config=MemoryConfig(enable_context_injection=True),
    )
    # Passing a bare string via dict-only input via context; TaskEnvelope
    # requires a dict. The heuristic's string branch is tested via direct call.
    result = injector._extract_query_text("plain string query")
    assert result == "plain string query"
    result = injector._extract_query_text({"irrelevant_key": "v1", "other": 42})
    # Fallback concatenation
    assert "v1" in result and "42" in result


# ---------------------------------------------------------------------------
# Tag propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injection_passes_tags_into_query(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_MEMORY_INJECTION", "1")
    mem = _StubMemory([(_entry("hit"), 0.9)])
    injector = MemoryContextInjector(
        memory=mem,
        config=MemoryConfig(enable_context_injection=True),
    )
    await injector.inject(
        TaskEnvelope(
            input={"query": "q"},
            tags={"domain": "agents", "tier": "prod"},
        )
    )
    assert mem.last_query is not None
    assert mem.last_query.tags == {"domain": "agents", "tier": "prod"}
