"""Mutation strategies for the Forge evolution loop.

Each mutator analyzes the run history and proposes a specific kind of change.
Mutators are composable — the evolution loop tries them in priority order
and picks the one with the highest confidence.

Built-in mutators:
  - PromptRewriteMutator   — rewrite system prompts based on failure patterns
  - ModelSwapMutator       — swap to a cheaper/faster model for low-complexity tasks
  - AgentCullMutator       — remove agents that never contribute
  - ParameterTuneMutator   — adjust timeout/max_steps based on observed patterns

Post-L7/L8 β.3 migration:

All mutators take and return :class:`TopologyState` (agents + config +
prompts_map) so mutations that affect run configuration
(``ParameterTuneMutator``) have a place to land. The orchestrator's
``swap_topology`` accepts a ``TopologyState`` directly, closing the
loop. The legacy ``list[AgentCard]`` signature is still accepted for
backwards compatibility via :func:`_coerce_to_state`.

β.2: :class:`PromptRewriteMutator` now makes a real structured LLM call
(via :class:`LLMClient`) to generate a rewritten prompt. Tests inject a
:class:`FakeLLMClient` with queued responses; production uses the
Anthropic-backed default.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any

import structlog
from pydantic import BaseModel, Field

from forge_core.evolution.llm_client import (
    LLMClient,
    LLMStructuredCallError,
    get_default_llm_client,
)
from forge_core.types import (
    AgentCard,
    Mutation,
    MutationDiff,
    MutationKind,
    RunConfig,
    RunResult,
    TopologyState,
)

logger = structlog.get_logger()

# Cost thresholds for model swap recommendations
_CHEAP_MODELS = {"claude-haiku-4-20250514", "gpt-4o-mini", "gemini-flash-1.5"}
_EXPENSIVE_MODELS = {"claude-opus-4-20250514", "gpt-4o", "gemini-ultra"}
_MODEL_DOWNGRADE_MAP = {
    "claude-opus-4-20250514": "claude-sonnet-4-20250514",
    "claude-sonnet-4-20250514": "claude-haiku-4-20250514",
    "gpt-4o": "gpt-4o-mini",
    "gemini-ultra": "gemini-flash-1.5",
}


# ---------------------------------------------------------------------------
# Signature compatibility shim
# ---------------------------------------------------------------------------


def _coerce_to_state(value: TopologyState | list[AgentCard]) -> TopologyState:
    """Accept either a :class:`TopologyState` or a bare agent list.

    The β.3 mutator protocol takes ``TopologyState``. Older call sites
    (and some tests) still pass ``list[AgentCard]``; we promote those
    to a minimal state with a default :class:`RunConfig` so the body of
    the mutator can treat everything uniformly.
    """
    if isinstance(value, TopologyState):
        return value
    return TopologyState(agents=list(value), config=RunConfig(), prompts_map={})


# ---------------------------------------------------------------------------
# PromptRewriteMutator (β.2: real LLM call)
# ---------------------------------------------------------------------------


class PromptRewriteResponse(BaseModel):
    """Structured response schema for the prompt-rewrite LLM call."""

    rewritten_prompt: str = Field(
        ...,
        description=(
            "Full replacement system prompt. Must address the failure "
            "pattern explicitly and preserve the agent's original role."
        ),
        min_length=20,
    )
    changes_summary: str = Field(
        default="",
        description="One-sentence summary of what changed vs. the original.",
    )


_PROMPT_REWRITE_SYSTEM = (
    "You are a prompt engineer helping an AI agent system recover from a "
    "recurring failure. Your job is to rewrite the agent's system prompt "
    "so the same failure stops happening, while keeping the agent's core "
    "role and capabilities intact. Be specific about what the agent must "
    "now do differently."
)

_PROMPT_REWRITE_TEMPLATE = """\
An agent keeps failing with the following error pattern:

    {error_pattern}

Its current system prompt is:
\"\"\"
{original_prompt}
\"\"\"

Rationale for this rewrite: {rationale}

Rewrite the system prompt so this failure pattern cannot recur. Preserve
the agent's role, keep the tone professional, and do not add preamble
text like "Here is the new prompt" — return only the prompt itself via
the structured tool.\
"""


class PromptRewriteMutator:
    """Proposes system prompt rewrites based on repeated failure patterns.

    The ``propose`` phase is purely statistical (frequency of error
    strings across recent runs). The ``apply`` phase makes a structured
    LLM call to generate a concrete rewritten prompt — no more no-op
    stubs. If the LLM is unreachable or returns an invalid response,
    the mutation is skipped cleanly (state returned unchanged) so the
    evolution loop can move on.
    """

    mutation_kind = MutationKind.PROMPT_REWRITE

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        *,
        model: str = "claude-sonnet-4-20250514",
    ) -> None:
        self._llm_client = llm_client
        self._model = model

    def _client(self) -> LLMClient:
        return self._llm_client or get_default_llm_client()

    # ---- propose -----------------------------------------------------
    def propose(self, run_results: list[RunResult]) -> Mutation | None:
        if len(run_results) < 2:
            return None

        error_counts: Counter[str] = Counter()
        for result in run_results[-10:]:
            for error in result.errors:
                key = error[:80]
                error_counts[key] += 1

        if not error_counts:
            return None

        most_common_error, count = error_counts.most_common(1)[0]
        if count < 2:
            return None

        confidence = min(0.9, 0.3 + count * 0.1)
        target_agent_id = self._pick_target_agent(run_results)

        return Mutation(
            kind=MutationKind.PROMPT_REWRITE,
            description=f"Rewrite prompt to handle: '{most_common_error[:60]}...'",
            rationale=(
                f"This error appeared {count} times in the last "
                f"{min(10, len(run_results))} runs. A more specific system "
                "prompt should prevent it."
            ),
            confidence=confidence,
            diff=[
                MutationDiff(
                    field_path=(
                        f"agents[id={target_agent_id}].system_prompt"
                        if target_agent_id
                        else "agents[*].system_prompt"
                    ),
                    description=f"Add explicit handling for: {most_common_error[:100]}",
                )
            ],
        )

    # ---- apply (β.2: real LLM call) ----------------------------------
    async def apply(
        self,
        mutation: Mutation,
        state: TopologyState | list[AgentCard],
    ) -> TopologyState:
        """Rewrite the target agent's system prompt via a structured LLM call."""
        current = _coerce_to_state(state)
        target = self._select_target(mutation, current.agents)
        if target is None:
            logger.info(
                "mutation.prompt_rewrite.no_target",
                reason="no agent matched mutation diff — nothing to rewrite",
            )
            return current

        error_pattern = self._extract_error_pattern(mutation)
        original_prompt = target.system_prompt or (
            f"You are {target.name or target.role or 'an agent'}. Do your job."
        )

        try:
            response = await self._client().structured_call(
                model=self._model,
                schema=PromptRewriteResponse,
                system=_PROMPT_REWRITE_SYSTEM,
                prompt=_PROMPT_REWRITE_TEMPLATE.format(
                    error_pattern=error_pattern,
                    original_prompt=original_prompt,
                    rationale=mutation.rationale or "(no rationale provided)",
                ),
                max_tokens=1024,
                temperature=0.3,
            )
        except LLMStructuredCallError as exc:
            # Well-known failure mode — skip mutation, keep topology.
            logger.warning(
                "mutation.prompt_rewrite.llm_failed",
                error=str(exc),
                agent_id=target.id,
            )
            return current

        new_prompt = response.rewritten_prompt.strip()
        if new_prompt == original_prompt.strip():
            logger.info("mutation.prompt_rewrite.noop", agent_id=target.id)
            return current

        updated_agent = target.model_copy(update={"system_prompt": new_prompt})
        new_agents = [updated_agent if a.id == target.id else a for a in current.agents]
        new_prompts = dict(current.prompts_map)
        new_prompts[target.id] = new_prompt

        logger.info(
            "mutation.prompt_rewrite.applied",
            agent_id=target.id,
            summary=response.changes_summary[:120],
        )
        return replace(current, agents=new_agents, prompts_map=new_prompts)

    # ---- rollback ----------------------------------------------------
    async def rollback(
        self,
        mutation: Mutation,
        state: TopologyState | list[AgentCard],
    ) -> TopologyState:
        """No-op — the orchestrator's snapshot restore is authoritative."""
        return _coerce_to_state(state)

    # ---- helpers -----------------------------------------------------
    @staticmethod
    def _select_target(mutation: Mutation, agents: list[AgentCard]) -> AgentCard | None:
        """Pick the agent named in the mutation diff, else the first agent.

        Accepts diff field_paths of the form ``agents[id=XYZ].system_prompt``.
        Falls back to the first agent with a system_prompt, then the first
        agent overall. Returns ``None`` if the topology is empty.
        """
        if not agents:
            return None
        for diff in mutation.diff:
            target_id = _parse_agent_id(diff.field_path)
            if target_id:
                match = next((a for a in agents if a.id == target_id), None)
                if match is not None:
                    return match
        # Prefer agents that actually have a prompt to rewrite.
        with_prompt = [a for a in agents if a.system_prompt]
        return with_prompt[0] if with_prompt else agents[0]

    @staticmethod
    def _extract_error_pattern(mutation: Mutation) -> str:
        """Pull the error description out of the mutation's description."""
        desc = mutation.description or ""
        if "'" in desc:
            start = desc.find("'") + 1
            end = desc.rfind("'")
            if end > start:
                return desc[start:end]
        return desc or "(unspecified error pattern)"

    @staticmethod
    def _pick_target_agent(run_results: list[RunResult]) -> str | None:
        """Best-effort: the agent id that appears most in recent error events."""
        for result in reversed(run_results[-10:]):
            for event in result.events:
                if event.error and event.agent_id:
                    return event.agent_id
        # Fallback: pick the busiest agent in the topology.
        if run_results and run_results[-1].topology:
            return run_results[-1].topology[0].id
        return None


def _parse_agent_id(field_path: str) -> str | None:
    """Extract the agent id from ``agents[id=foo].xxx`` paths."""
    marker = "agents[id="
    idx = field_path.find(marker)
    if idx < 0:
        return None
    rest = field_path[idx + len(marker) :]
    end = rest.find("]")
    if end <= 0:
        return None
    return rest[:end]


# ---------------------------------------------------------------------------
# ModelSwapMutator
# ---------------------------------------------------------------------------


class ModelSwapMutator:
    """Proposes model downgrades for agents running expensive models on simple tasks."""

    mutation_kind = MutationKind.MODEL_SWAP

    def propose(self, run_results: list[RunResult]) -> Mutation | None:
        if len(run_results) < 3:
            return None

        agent_costs: dict[str, list[float]] = {}
        agent_models: dict[str, str] = {}

        for result in run_results[-5:]:
            for event in result.events:
                if event.model and event.model in _EXPENSIVE_MODELS and event.agent_id:
                    agent_costs.setdefault(event.agent_id, []).append(float(event.cost))
                    agent_models[event.agent_id] = event.model

        if not agent_costs:
            return None

        costliest_agent = max(agent_costs, key=lambda aid: sum(agent_costs[aid]))
        current_model = agent_models.get(costliest_agent)
        if not current_model or current_model not in _MODEL_DOWNGRADE_MAP:
            return None

        target_model = _MODEL_DOWNGRADE_MAP[current_model]
        total_cost = sum(agent_costs[costliest_agent])

        return Mutation(
            kind=MutationKind.MODEL_SWAP,
            description=(
                f"Swap agent '{costliest_agent[:12]}' from {current_model} → {target_model}"
            ),
            rationale=(
                f"Agent '{costliest_agent[:12]}' spent ${total_cost:.4f} "
                f"using {current_model} in the last 5 runs. "
                f"Downgrading to {target_model} could reduce cost by ~50-70%."
            ),
            confidence=0.65,
            diff=[
                MutationDiff(
                    field_path=f"agents[id={costliest_agent}].model",
                    before=current_model,
                    after=target_model,
                    description=f"Switch from {current_model} to {target_model}",
                )
            ],
        )

    async def apply(
        self,
        mutation: Mutation,
        state: TopologyState | list[AgentCard],
    ) -> TopologyState:
        current = _coerce_to_state(state)
        updated_agents: list[AgentCard] = []
        for agent in current.agents:
            new_agent = agent
            for diff in mutation.diff:
                target_id = _parse_agent_id(diff.field_path)
                if target_id == agent.id and diff.after:
                    new_agent = agent.model_copy(update={"model": str(diff.after)})
                    break
            updated_agents.append(new_agent)
        logger.info("mutation.model_swap.applied", description=mutation.description)
        return replace(current, agents=updated_agents)

    async def rollback(
        self,
        mutation: Mutation,
        state: TopologyState | list[AgentCard],
    ) -> TopologyState:
        current = _coerce_to_state(state)
        reverted: list[AgentCard] = []
        for agent in current.agents:
            new_agent = agent
            for diff in mutation.diff:
                target_id = _parse_agent_id(diff.field_path)
                if target_id == agent.id and diff.before:
                    new_agent = agent.model_copy(update={"model": str(diff.before)})
                    break
            reverted.append(new_agent)
        return replace(current, agents=reverted)


# ---------------------------------------------------------------------------
# AgentCullMutator
# ---------------------------------------------------------------------------


class AgentCullMutator:
    """Proposes removing agents that never emit useful output.

    With snapshot-based rollback (β.1), the historical gap — inability
    to reconstruct culled agents — is moot. ``rollback`` here is a
    no-op: the orchestrator restores from the pre-mutation snapshot.
    """

    mutation_kind = MutationKind.AGENT_CULL

    def propose(self, run_results: list[RunResult]) -> Mutation | None:
        if len(run_results) < 4:
            return None

        active_agents: Counter[str] = Counter()
        all_agents: set[str] = set()

        for result in run_results[-8:]:
            for agent in result.topology:
                all_agents.add(agent.id)
            for event in result.events:
                if event.agent_id and event.input_tokens > 0:
                    active_agents[event.agent_id] += 1

        inactive = all_agents - set(active_agents.keys())
        if not inactive:
            return None

        target_agent = next(iter(sorted(inactive)))
        return Mutation(
            kind=MutationKind.AGENT_CULL,
            description=f"Remove idle agent '{target_agent[:16]}' from topology",
            rationale=(
                f"Agent '{target_agent[:16]}' emitted 0 LLM calls across the last "
                f"{min(8, len(run_results))} runs. It may be dead code or have "
                "overlapping responsibilities with another agent."
            ),
            confidence=0.55,
            diff=[
                MutationDiff(
                    field_path=f"agents[id={target_agent}]",
                    before=target_agent,
                    after=None,
                    description="Remove agent from topology",
                )
            ],
        )

    async def apply(
        self,
        mutation: Mutation,
        state: TopologyState | list[AgentCard],
    ) -> TopologyState:
        current = _coerce_to_state(state)
        target_ids = {
            _parse_agent_id(d.field_path) or d.before for d in mutation.diff if d.after is None
        }
        target_ids = {t for t in target_ids if t}
        remaining = [a for a in current.agents if a.id not in target_ids]
        logger.info(
            "mutation.agent_cull.applied",
            removed=len(current.agents) - len(remaining),
            description=mutation.description,
        )
        return replace(current, agents=remaining)

    async def rollback(
        self,
        mutation: Mutation,
        state: TopologyState | list[AgentCard],
    ) -> TopologyState:
        # Authoritative rollback lives in the orchestrator via snapshot
        # restore. This hook is kept for protocol symmetry.
        return _coerce_to_state(state)


# ---------------------------------------------------------------------------
# ParameterTuneMutator (β.3: now actually modifies RunConfig)
# ---------------------------------------------------------------------------


class ParameterTuneMutator:
    """Adjusts ``RunConfig.timeout_seconds`` / ``max_steps`` based on patterns.

    Pre-β.3 this mutator was a no-op — the protocol only allowed it to
    touch agents. Now it operates on the full :class:`TopologyState`, so
    a timeout bump actually takes effect on the next run.
    """

    mutation_kind = MutationKind.PARAMETER_TUNE

    #: Fraction to multiply ``timeout_seconds`` by when bumping.
    TIMEOUT_MULTIPLIER: float = 1.5
    #: Fraction to multiply ``max_steps`` by when trimming (floor at 3).
    MAX_STEPS_TRIM_MULTIPLIER: float = 0.8
    MIN_MAX_STEPS: int = 3

    def propose(self, run_results: list[RunResult]) -> Mutation | None:
        if len(run_results) < 5:
            return None

        timeout_count = sum(1 for r in run_results[-5:] if "timeout" in " ".join(r.errors).lower())
        if timeout_count >= 3:
            return Mutation(
                kind=MutationKind.PARAMETER_TUNE,
                description="Increase timeout (3+ runs timed out in last 5)",
                rationale=(
                    f"{timeout_count}/5 recent runs timed out. "
                    f"Increase timeout_seconds by "
                    f"{int((self.TIMEOUT_MULTIPLIER - 1) * 100)}%."
                ),
                confidence=0.80,
                diff=[
                    MutationDiff(
                        field_path="config.timeout_seconds",
                        description=(f"Increase by {int((self.TIMEOUT_MULTIPLIER - 1) * 100)}%"),
                    )
                ],
            )

        avg_duration = sum(r.duration_ms or 0 for r in run_results[-5:]) / 5
        if avg_duration < 500:
            return Mutation(
                kind=MutationKind.PARAMETER_TUNE,
                description="Trim max_steps (runs completing suspiciously fast)",
                rationale=(
                    f"Avg duration {avg_duration:.0f}ms suggests runs may be "
                    f"failing early. Trim max_steps by "
                    f"{int((1 - self.MAX_STEPS_TRIM_MULTIPLIER) * 100)}%."
                ),
                confidence=0.45,
                diff=[
                    MutationDiff(
                        field_path="config.max_steps",
                        description=(
                            f"Reduce by {int((1 - self.MAX_STEPS_TRIM_MULTIPLIER) * 100)}%"
                        ),
                    )
                ],
            )

        return None

    async def apply(
        self,
        mutation: Mutation,
        state: TopologyState | list[AgentCard],
    ) -> TopologyState:
        current = _coerce_to_state(state)
        new_config = current.config.model_copy(deep=True)

        for diff in mutation.diff:
            if diff.field_path == "config.timeout_seconds":
                new_config.timeout_seconds = float(
                    current.config.timeout_seconds * self.TIMEOUT_MULTIPLIER
                )
            elif diff.field_path == "config.max_steps":
                new_config.max_steps = max(
                    self.MIN_MAX_STEPS,
                    int(current.config.max_steps * self.MAX_STEPS_TRIM_MULTIPLIER),
                )

        logger.info(
            "mutation.parameter_tune.applied",
            description=mutation.description,
            timeout_before=current.config.timeout_seconds,
            timeout_after=new_config.timeout_seconds,
            max_steps_before=current.config.max_steps,
            max_steps_after=new_config.max_steps,
        )
        return replace(current, config=new_config)

    async def rollback(
        self,
        mutation: Mutation,
        state: TopologyState | list[AgentCard],
    ) -> TopologyState:
        return _coerce_to_state(state)


# Registry of all built-in mutators, sorted by typical usefulness
BUILTIN_MUTATORS: list[Any] = [
    ModelSwapMutator(),
    PromptRewriteMutator(),
    AgentCullMutator(),
    ParameterTuneMutator(),
]
