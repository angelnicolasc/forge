"""Core type definitions for the Forge ecosystem.

All shared Pydantic models live here. Every other package depends on these
types for zero-coupling extensibility via structural typing (Protocols).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

# Pydantic requires typing_extensions.TypedDict on Python < 3.12
from typing_extensions import TypedDict

# SPIFFE URI format: spiffe://<trust-domain>/<workload-identifier>
_SPIFFE_RE = re.compile(r"^spiffe://(?P<td>[^/]+)/(?P<wl>.+)$")

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunEventKind(StrEnum):
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    LLM_CALL = "llm_call"
    LLM_RESPONSE = "llm_response"
    HANDOFF = "handoff"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    ERROR = "error"
    EVOLUTION = "evolution"
    CONTEXT_INJECTED = "context_injected"  # fired before LLM call; payload: context_tokens, source
    SKILL_CALL = "skill_call"  # fired after skill handler completes; payload: skill, duration_ms
    SPEC_VERIFIED = "spec_verified"  # after spec verification; payload: spec, rate, passed
    REVIEW_COMPLETE = "review_complete"  # after review gate; payload: hook, allowed, finding_count
    LLM_TOKEN = "llm_token"  # one streaming chunk; payload: token, is_thinking
    ADAPTER_DEGRADED = "adapter_degraded"  # SDK patch failed; payload: adapter, sdk, reason, impact


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INPUT_REQUIRED = "input-required"  # A2A: task suspended awaiting human input


class MutationKind(StrEnum):
    PROMPT_REWRITE = "prompt_rewrite"
    TOPOLOGY_CHANGE = "topology_change"
    AGENT_ADD = "agent_add"
    AGENT_CULL = "agent_cull"
    TOOL_SWAP = "tool_swap"
    MODEL_SWAP = "model_swap"
    PARAMETER_TUNE = "parameter_tune"


class TopologyKind(StrEnum):
    ORCHESTRATOR_WORKER = "orchestrator_worker"
    HIERARCHICAL = "hierarchical"
    SWARM = "swarm"
    BLACKBOARD = "blackboard"
    DEBATE = "debate"
    PIPELINE = "pipeline"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Agent / Topology
# ---------------------------------------------------------------------------


class ToolRef(BaseModel):
    """Reference to a tool available to an agent."""

    name: str
    description: str = ""
    schema_ref: dict[str, Any] | None = None


class AgentCard(BaseModel):
    """Describes an agent in the topology — A2A Agent Card compliant."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    name: str = ""
    role: str = ""
    description: str = ""
    model: str | None = None
    system_prompt: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    tools: list[ToolRef] = Field(default_factory=list)
    upstream: list[str] = Field(default_factory=list)
    downstream: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # A2A Agent Card fields — all optional, backward-compatible
    url: str | None = None
    version: str = "0.1.0"
    provider: dict[str, Any] = Field(default_factory=dict)
    # SPIFFE Verifiable Identity Document URI — format-only. Operator-supplied.
    spiffe_id: str | None = None

    @field_validator("spiffe_id", mode="before")
    @classmethod
    def _validate_spiffe_uri(cls, v: object) -> object:
        if v is None:
            return v
        if not isinstance(v, str):
            raise ValueError("spiffe_id must be a string or None")
        if not _SPIFFE_RE.fullmatch(v):
            raise ValueError(
                f"Invalid SPIFFE URI {v!r}. Expected: spiffe://<trust-domain>/<workload-identifier>"
            )
        return v


class EvolutionConfig(BaseModel):
    """Configuration for the self-evolution loop."""

    auto_trigger_enabled: bool = False
    min_history_for_evolution: int = 5
    min_interval_seconds: float = 300.0
    trigger_every_n_runs: int = 3
    max_mutations_per_session: int = 5
    cost_ceiling_per_eval: Decimal = Decimal("1.00")
    rollback_threshold: float = 0.05
    mode: str = "suggest"  # "suggest" | "auto" | "off"


class MemoryConfig(BaseModel):
    """Configuration for the living collaborative memory."""

    enable_context_injection: bool = False
    top_k: int = 5
    min_relevance: float = 0.3
    persist_dir: str | None = None


class RunConfig(BaseModel):
    """Configuration for a single run."""

    max_steps: int = 100
    timeout_seconds: float = 300.0
    cost_ceiling: Decimal = Decimal("10.00")
    enable_memory: bool = True
    enable_evolution: bool = False
    evolution_mode: str = "suggest"  # "suggest" | "auto" | "off"
    model_override: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


class ForgeMemoryContext(TypedDict):
    """Typed shape of the ``forge_memory`` key injected into ``TaskEnvelope.context``."""

    entries: list[MemoryEntry]
    query: str
    hits: int


class TaskEnvelopeContext(TypedDict, total=False):
    """Typed contract for ``TaskEnvelope.context``.

    ``total=False`` means every key is optional, preserving backward
    compatibility with callers that pass arbitrary ``dict[str, Any]`` values.
    New well-known keys should be added here as they are formalized.
    """

    forge_memory: ForgeMemoryContext


class TaskEnvelope(BaseModel):
    """Universal task input — the single entry point for any Forge run."""

    task_id: str = Field(default_factory=lambda: uuid4().hex)
    input: dict[str, Any]
    config: RunConfig = Field(default_factory=RunConfig)
    memory_context: list[str] = Field(default_factory=list)
    context: TaskEnvelopeContext = Field(default_factory=TaskEnvelopeContext)
    tags: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Events and results
# ---------------------------------------------------------------------------


class RunEvent(BaseModel):
    """A single event in a run's execution trace."""

    id: str = Field(default_factory=lambda: uuid4().hex[:16])
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: RunEventKind
    run_id: str | None = None
    agent_id: str | None = None
    parent_span_id: str | None = None
    span_id: str | None = None
    tool_name: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0  # extended thinking tokens (Anthropic claude-opus-4+)
    cached_input_tokens: int = 0  # prompt-cache read tokens (billed at ~10% of input price)
    context_tokens: int = 0  # tokens injected into context window (CONTEXT_INJECTED events)
    cost: Decimal = Decimal("0")
    latency_ms: float = 0.0
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class CostSummary(BaseModel):
    """Aggregated cost metrics for a run."""

    total_cost: Decimal = Decimal("0")
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cost_by_model: dict[str, Decimal] = Field(default_factory=dict)
    cost_by_agent: dict[str, Decimal] = Field(default_factory=dict)
    llm_calls: int = 0
    tool_calls: int = 0


class RunResult(BaseModel):
    """Complete result of a Forge run."""

    task_id: str
    run_id: str = Field(default_factory=lambda: uuid4().hex[:16])
    status: RunStatus = RunStatus.COMPLETED
    output: Any = None
    trace_id: str = Field(default_factory=lambda: uuid4().hex)
    cost: CostSummary = Field(default_factory=CostSummary)
    duration_ms: float = 0.0
    events: list[RunEvent] = Field(default_factory=list)
    topology: list[AgentCard] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Evolution
# ---------------------------------------------------------------------------


class MutationDiff(BaseModel):
    """A single semantic diff applied by a mutation.

    `field_path` uses dotted/bracketed notation:
      - `agents[id=agent-abc].model`
      - `agents[*].system_prompt`
      - `config.timeout_seconds`
    """

    field_path: str
    before: Any = None
    after: Any = None
    description: str = ""


class FitnessScore(BaseModel):
    """Composite fitness score produced by an Evaluator.

    `overall` is a weighted combination of `cost_score`, `latency_score`, and
    `quality_score`, each on [0, 1].
    """

    overall: float = Field(ge=0.0, le=1.0, default=0.0)
    cost_score: float = Field(ge=0.0, le=1.0, default=0.0)
    latency_score: float = Field(ge=0.0, le=1.0, default=0.0)
    quality_score: float = Field(ge=0.0, le=1.0, default=0.0)
    run_id: str | None = None
    confidence_interval: tuple[float, float] | None = None


class Mutation(BaseModel):
    """A proposed or applied change to the agent system."""

    id: str = Field(default_factory=lambda: uuid4().hex[:16])
    kind: MutationKind
    description: str = ""
    rationale: str = ""
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    diff: list[MutationDiff] = Field(default_factory=list)
    parent_id: str | None = None
    applied: bool = False
    fitness_before: FitnessScore | None = None
    fitness_after: FitnessScore | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Topology state (passed to mutators in β.3)
# ---------------------------------------------------------------------------


@dataclass
class TopologyState:
    """Mutable snapshot of the orchestrator's topology passed to Mutator.apply.

    Using a dataclass (not Pydantic) because mutators return modified states
    that need cheap `replace(...)` semantics without Pydantic validation cost.
    """

    agents: list[AgentCard]
    config: RunConfig
    prompts_map: dict[str, str] = field(default_factory=dict)

    def clone(self) -> TopologyState:
        """Return a deep-enough copy safe to mutate without affecting the original."""
        return TopologyState(
            agents=[a.model_copy(deep=True) for a in self.agents],
            config=self.config.model_copy(deep=True),
            prompts_map=dict(self.prompts_map),
        )


# ---------------------------------------------------------------------------
# Run context (propagated via contextvars — see forge_core.context)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunContext:
    """Immutable per-run context propagated via contextvars.

    Any downstream code (interceptors, tracer, memory ingester) reads this
    to get run_id/agent_id without the adapter having to plumb it through.
    """

    run_id: str
    task_id: str
    span_id: str | None = None
    agent_id: str | None = None
    parent_span_id: str | None = None
    envelope_task_id: str | None = None

    def child(self, *, span_id: str | None = None, agent_id: str | None = None) -> RunContext:
        """Create a derived context for a child span / agent scope."""
        return RunContext(
            run_id=self.run_id,
            task_id=self.task_id,
            span_id=span_id or self.span_id,
            agent_id=agent_id or self.agent_id,
            parent_span_id=self.span_id,
            envelope_task_id=self.envelope_task_id,
        )


# ---------------------------------------------------------------------------
# Memory types (shared across forge-core and forge-memory)
# ---------------------------------------------------------------------------


class MemoryEntry(BaseModel):
    """A single entry in the Living Collaborative Memory."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    content: str
    embedding: list[float] | None = None
    entities: list[str] = Field(default_factory=list)
    relations: list[tuple[str, str, str]] = Field(default_factory=list)
    source_run_id: str | None = None
    source_agent_id: str | None = None
    version: int = 1
    supersedes: str | None = None
    evidence: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime | None = None


class MemoryQuery(BaseModel):
    """Query against the Living Collaborative Memory."""

    text: str | None = None
    embedding: list[float] | None = None
    entity_filter: list[str] | None = None
    tag_filter: dict[str, str] | None = None
    tags: dict[str, str] | None = None  # alias for tag_filter for ergonomics
    top_k: int = 10
    limit: int | None = None  # alias for top_k
    min_score: float = 0.0
    include_expired: bool = False
