"""ζ.5c — HybridMemory.query degrades gracefully when both backends fail.

After ``failure_threshold`` consecutive full outages the breaker is OPEN
and ``query`` returns ``[]`` without touching the backends. Consistent
with the δ.1 promise that memory errors never abort a run.
"""

from __future__ import annotations

import pytest

from forge_core.circuit_breaker import CircuitBreaker, CircuitState
from forge_core.types import MemoryQuery
from forge_memory.hybrid import HybridMemory


class _FailingBackend:
    """Both ``store`` and ``query`` raise every time."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls = 0

    async def store(self, entry):  # pragma: no cover — unused in these tests
        raise RuntimeError(f"{self.kind} store exploded")

    async def query(self, q):
        self.calls += 1
        raise RuntimeError(f"{self.kind} query exploded")

    async def delete(self, entry_id):  # pragma: no cover — unused
        return False

    async def update(self, entry):  # pragma: no cover — unused
        return True


@pytest.mark.asyncio
async def test_memory_query_returns_empty_when_backends_keep_failing() -> None:
    breaker = CircuitBreaker("memory_query", failure_threshold=3, recovery_timeout_seconds=60.0)
    vector = _FailingBackend("vector")
    graph = _FailingBackend("graph")
    memory = HybridMemory(
        vector_backend=vector,
        graph_backend=graph,
        breaker=breaker,
    )

    # First three calls actually hit the backends, fail, and push the
    # breaker to OPEN. ``query`` swallows the exceptions and returns [].
    for _ in range(3):
        results = await memory.query(MemoryQuery(text="anything"))
        assert results == []

    assert breaker.state is CircuitState.OPEN
    assert vector.calls == 3
    assert graph.calls == 3

    # Subsequent queries short-circuit: the backends are not touched.
    for _ in range(5):
        results = await memory.query(MemoryQuery(text="anything"))
        assert results == []
    assert vector.calls == 3
    assert graph.calls == 3
