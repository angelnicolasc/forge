"""Protocol definitions for the Forge ecosystem.

All extension points are defined as Python Protocols (structural typing).
Any class that implements the correct methods satisfies the protocol —
no inheritance required. This enables zero-coupling plugin architecture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from decimal import Decimal
    from pathlib import Path

    from forge_core.types import (
        AgentCard,
        MemoryEntry,
        MemoryQuery,
        Mutation,
        RunEvent,
        RunResult,
        TaskEnvelope,
        TopologyState,
    )


@runtime_checkable
class Adapter(Protocol):
    """Wraps a multi-agent framework into Forge's uniform interface.

    Each adapter translates a specific framework (LangGraph, CrewAI, AutoGen, etc.)
    into Forge's TaskEnvelope -> RunResult pipeline while preserving full
    observability through RunEvents.
    """

    @property
    def name(self) -> str:
        """Human-readable adapter name (e.g., 'langgraph', 'crewai')."""
        ...

    def detect(self, source: str | Path) -> bool:
        """Return True if this adapter can handle the given source file/module."""
        ...

    async def load(self, source: str | Path) -> None:
        """Load and prepare the agent flow from source."""
        ...

    async def run(self, envelope: TaskEnvelope) -> RunResult:
        """Execute the flow and return a complete result with traces."""
        ...

    def stream(self, envelope: TaskEnvelope) -> AsyncIterator[RunEvent]:
        """Execute the flow and yield events as they happen."""
        ...

    def topology(self) -> list[AgentCard]:
        """Return the current agent topology as a list of AgentCards."""
        ...


@runtime_checkable
class MemoryBackend(Protocol):
    """Pluggable memory store for the Living Collaborative Memory.

    Implementations can be vector stores (ChromaDB, Qdrant), graph stores
    (NetworkX, Neo4j), or symbolic stores (rule engines, fact stores).
    The HybridMemory orchestrator fans out queries across all backends.
    """

    @property
    def backend_type(self) -> str:
        """Type identifier: 'vector', 'graph', or 'symbolic'."""
        ...

    async def store(self, entry: MemoryEntry) -> str:
        """Store an entry and return its ID."""
        ...

    async def query(self, q: MemoryQuery) -> list[tuple[MemoryEntry, float]]:
        """Query entries, returning (entry, score) pairs sorted by relevance."""
        ...

    async def delete(self, entry_id: str) -> bool:
        """Delete an entry by ID. Return True if found and deleted."""
        ...

    async def health_check(self) -> bool:
        """Return True if the backend is healthy and reachable."""
        ...


@runtime_checkable
class Mutator(Protocol):
    """A single evolution mutation strategy.

    Mutators analyze the evolution journal, propose mutations (prompt rewrites,
    topology changes, agent add/cull, tool swaps, parameter tuning), apply
    them to a ``TopologyState`` (agents + config + prompts), and can roll
    them back from a captured snapshot if fitness degrades.

    The protocol operates on ``TopologyState`` rather than a bare
    ``list[AgentCard]`` so that mutations affecting ``RunConfig``
    (e.g. :class:`~forge_core.types.MutationKind.PARAMETER_TUNE`) have a
    place to land — see L7/L8 plan Gap #5.
    """

    mutation_kind: Any  # MutationKind — ``Any`` avoids circular import at Protocol definition time

    def propose(self, run_results: list[RunResult]) -> Mutation | None:
        """Analyze recent runs and propose a mutation, or ``None`` if no improvement found."""
        ...

    async def apply(self, mutation: Mutation, state: TopologyState) -> TopologyState:
        """Apply the mutation to the topology state and return the modified state.

        Implementations MUST be pure w.r.t. the input ``state`` — do not
        mutate it in place. Return a new (or ``replace``d) ``TopologyState``.
        This contract is what makes snapshot-based rollback (β.1) reliable.
        """
        ...

    async def rollback(self, mutation: Mutation, state: TopologyState) -> TopologyState:
        """Rollback a previously applied mutation.

        Called with the *post-mutation* state. Returns a state that reverses
        the mutation. For mutators that support snapshot-based rollback, the
        orchestrator restores from ``TopologySnapshot`` directly and this
        method is a no-op returning ``state``; it still exists for mutators
        that compute an inverse locally (e.g. :class:`ModelSwapMutator`).
        """
        ...


@runtime_checkable
class LLMCallInterceptor(Protocol):
    """Feeds per-call LLM telemetry (tokens, cost, spans) into the event bus.

    Every adapter (LangGraph, CrewAI, AutoGen, generic) routes its LLM
    observations through this protocol. Exactly one canonical implementation
    lives in :mod:`forge_observe.interceptor`; adapters import it, not their
    own copy. This is how Gap #1 (costs always $0) and Gap #3 (orphan spans)
    are closed simultaneously.

    Contract:

    * ``on_llm_start`` is called *before* the model call begins. It returns
      a ``span_id`` that the caller must pass back into ``on_llm_end`` /
      ``on_llm_error`` to close the same span.
    * Token counts passed to ``on_llm_end`` MUST be accurate; the
      interceptor does NOT estimate. If the underlying framework doesn't
      expose usage, the adapter passes ``0`` and logs a warning.
    * The interceptor reads :func:`forge_core.context.current_run` to bind
      events to the active run — it does not take ``run_id`` as a parameter.
    """

    async def on_llm_start(
        self,
        *,
        model: str,
        agent_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Open a span for an LLM call. Returns a ``span_id`` to pair with ``on_llm_end``."""
        ...

    async def on_llm_end(
        self,
        span_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        output: Any = None,
    ) -> None:
        """Close the span, publish a populated ``LLM_CALL`` event to the bus."""
        ...

    async def on_llm_error(self, span_id: str, error: BaseException) -> None:
        """Close the span with an error status and publish an ``ERROR`` event."""
        ...


@runtime_checkable
class CostModel(Protocol):
    """Pricing model for LLM API calls.

    Maps (model_name, input_tokens, output_tokens) to cost in USD.
    Used by forge-observe for real-time cost tracking and by the
    evolution loop for cost optimization.
    """

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        """Calculate the cost for a single LLM call."""
        ...

    def models(self) -> list[str]:
        """List all models with known pricing."""
        ...


@runtime_checkable
class Evaluator(Protocol):
    """Fitness evaluator for the evolution loop.

    Scores a RunResult on a 0-1 scale considering cost, latency,
    quality, and custom criteria.
    """

    async def evaluate(self, result: RunResult) -> float:
        """Score a run result. Returns 0.0 (worst) to 1.0 (best)."""
        ...

    @property
    def weights(self) -> dict[str, float]:
        """Current scoring weights (e.g., {'cost': 0.3, 'latency': 0.2, 'quality': 0.5})."""
        ...
