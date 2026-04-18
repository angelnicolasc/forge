"""Acceptance tests for β.2 (real LLM prompt rewrite) and β.3 (TopologyState signature).

The single most important assertion here is
``test_prompt_rewrite_actually_changes_prompt``: pre-remediation it was
a no-op (``return agents`` unchanged). If this test regresses to red,
evolution is theater again.

The ``ParameterTuneMutator`` cases guard against another silent
regression — pre-β.3 the mutator took ``list[AgentCard]`` so there was
no place to put a tuned ``timeout_seconds``. Now it returns a full
``TopologyState`` with a mutated ``config``, and the orchestrator's
``swap_topology`` projects that onto the process-wide config.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fixtures.mock_llm_adapter import MockLLMFlowAdapter

from forge_core.evolution.llm_client import (
    FakeLLMClient,
    LLMStructuredCallError,
)
from forge_core.evolution.mutations import (
    AgentCullMutator,
    ModelSwapMutator,
    ParameterTuneMutator,
    PromptRewriteMutator,
    PromptRewriteResponse,
)
from forge_core.harness import MetaOrchestrator
from forge_core.types import (
    AgentCard,
    CostSummary,
    Mutation,
    MutationDiff,
    MutationKind,
    RunConfig,
    RunEvent,
    RunEventKind,
    RunResult,
    RunStatus,
    TopologyState,
)


def _state(agents: list[AgentCard], **config_overrides: object) -> TopologyState:
    return TopologyState(
        agents=agents,
        config=RunConfig(**config_overrides),  # type: ignore[arg-type]
        prompts_map={},
    )


# ---------------------------------------------------------------------------
# β.2 — PromptRewriteMutator with real (faked) LLM call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_rewrite_actually_changes_prompt() -> None:
    """The canonical β.2 assertion: apply() produces a *different* prompt.

    Pre-β.2 the implementation was ``return agents`` — this test would
    have failed immediately. With a FakeLLMClient queued, it now
    exercises the real structured-call path.
    """
    fake = FakeLLMClient()
    fake.queue(
        PromptRewriteResponse,
        {
            "rewritten_prompt": (
                "You are a careful researcher. You MUST NEVER exceed 5 "
                "requests per minute to external APIs. If you hit a rate "
                "limit, back off exponentially rather than retrying immediately."
            ),
            "changes_summary": "Added rate-limit handling guidance.",
        },
    )
    mutator = PromptRewriteMutator(llm_client=fake)
    state = _state(
        [
            AgentCard(
                id="a1",
                name="researcher",
                role="worker",
                system_prompt="You are a helper.",
            )
        ]
    )
    mutation = Mutation(
        kind=MutationKind.PROMPT_REWRITE,
        description="Rewrite prompt to handle: 'Rate limit exceeded'",
        rationale="Rate limit errors in 4/10 runs.",
        diff=[MutationDiff(field_path="agents[id=a1].system_prompt")],
    )

    new_state = await mutator.apply(mutation, state)

    assert isinstance(new_state, TopologyState)
    rewritten = new_state.agents[0].system_prompt or ""
    assert rewritten != "You are a helper."
    assert "rate limit" in rewritten.lower()
    # prompts_map carries the new prompt keyed by agent id.
    assert new_state.prompts_map["a1"] == rewritten
    # Exactly one LLM call happened.
    assert len(fake.calls) == 1
    assert fake.calls[0]["schema"] == "PromptRewriteResponse"


@pytest.mark.asyncio
async def test_prompt_rewrite_skips_on_llm_failure() -> None:
    """If the LLM call fails, the state is returned unchanged — no crash."""
    fake = FakeLLMClient()  # empty queue → raises LLMStructuredCallError
    mutator = PromptRewriteMutator(llm_client=fake)
    original = AgentCard(id="a1", system_prompt="You are a helper.")
    state = _state([original])
    mutation = Mutation(
        kind=MutationKind.PROMPT_REWRITE,
        diff=[MutationDiff(field_path="agents[id=a1].system_prompt")],
    )

    new_state = await mutator.apply(mutation, state)

    assert new_state.agents[0].system_prompt == "You are a helper."


@pytest.mark.asyncio
async def test_prompt_rewrite_noop_when_llm_returns_same() -> None:
    """If the LLM returns the same prompt, the mutator treats it as a no-op."""
    same = "You are a careful researcher who respects rate limits."
    fake = FakeLLMClient()
    fake.queue(PromptRewriteResponse, {"rewritten_prompt": same})
    mutator = PromptRewriteMutator(llm_client=fake)
    state = _state([AgentCard(id="a1", system_prompt=same)])
    mutation = Mutation(
        kind=MutationKind.PROMPT_REWRITE,
        diff=[MutationDiff(field_path="agents[id=a1].system_prompt")],
    )
    new_state = await mutator.apply(mutation, state)
    # Unchanged — the mutator logged a noop but didn't return a "corrupted" state.
    assert new_state.agents[0].system_prompt == same
    assert "a1" not in new_state.prompts_map  # prompts_map only updates on change


def test_llm_structured_call_error_rejects_bad_schema_payload() -> None:
    """FakeLLMClient raises when the queued schema doesn't match the call."""
    fake = FakeLLMClient()
    fake.queue(PromptRewriteResponse, {"rewritten_prompt": "ok" * 20})

    class OtherSchema(PromptRewriteResponse):
        pass

    async def run() -> None:
        await fake.structured_call(
            model="claude-sonnet-4-20250514",
            schema=OtherSchema,
            system="",
            prompt="",
        )

    import asyncio

    with pytest.raises(LLMStructuredCallError):
        asyncio.run(run())


# ---------------------------------------------------------------------------
# β.3 — ParameterTuneMutator actually modifies RunConfig
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parameter_tune_increases_timeout() -> None:
    mutator = ParameterTuneMutator()
    state = _state([], timeout_seconds=60.0)
    mutation = Mutation(
        kind=MutationKind.PARAMETER_TUNE,
        diff=[MutationDiff(field_path="config.timeout_seconds")],
    )
    new_state = await mutator.apply(mutation, state)
    assert new_state.config.timeout_seconds == pytest.approx(60.0 * 1.5)
    # Original state is untouched — mutator is pure w.r.t. input.
    assert state.config.timeout_seconds == 60.0


@pytest.mark.asyncio
async def test_parameter_tune_trims_max_steps_with_floor() -> None:
    mutator = ParameterTuneMutator()
    state = _state([], max_steps=10)
    mutation = Mutation(
        kind=MutationKind.PARAMETER_TUNE,
        diff=[MutationDiff(field_path="config.max_steps")],
    )
    new_state = await mutator.apply(mutation, state)
    assert new_state.config.max_steps == 8  # 10 * 0.8 = 8


@pytest.mark.asyncio
async def test_parameter_tune_max_steps_respects_minimum() -> None:
    mutator = ParameterTuneMutator()
    state = _state([], max_steps=2)
    mutation = Mutation(
        kind=MutationKind.PARAMETER_TUNE,
        diff=[MutationDiff(field_path="config.max_steps")],
    )
    new_state = await mutator.apply(mutation, state)
    # Floor at MIN_MAX_STEPS=3 even when the multiplier would go lower.
    assert new_state.config.max_steps == 3


# ---------------------------------------------------------------------------
# β.3 — ModelSwapMutator now on TopologyState
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_swap_applies_to_matching_agent_and_leaves_others() -> None:
    mutator = ModelSwapMutator()
    agents = [
        AgentCard(id="a1", model="claude-opus-4-20250514"),
        AgentCard(id="a2", model="claude-haiku-4-20250514"),
    ]
    state = _state(agents)
    mutation = Mutation(
        kind=MutationKind.MODEL_SWAP,
        diff=[
            MutationDiff(
                field_path="agents[id=a1].model",
                before="claude-opus-4-20250514",
                after="claude-sonnet-4-20250514",
            )
        ],
    )
    new_state = await mutator.apply(mutation, state)
    by_id = {a.id: a for a in new_state.agents}
    assert by_id["a1"].model == "claude-sonnet-4-20250514"
    assert by_id["a2"].model == "claude-haiku-4-20250514"  # unchanged


@pytest.mark.asyncio
async def test_model_swap_rollback_restores_before_values() -> None:
    mutator = ModelSwapMutator()
    # Simulate the post-apply state: a1 is on sonnet (the "after").
    agents = [AgentCard(id="a1", model="claude-sonnet-4-20250514")]
    state = _state(agents)
    mutation = Mutation(
        kind=MutationKind.MODEL_SWAP,
        diff=[
            MutationDiff(
                field_path="agents[id=a1].model",
                before="claude-opus-4-20250514",
                after="claude-sonnet-4-20250514",
            )
        ],
    )
    reverted = await mutator.rollback(mutation, state)
    assert reverted.agents[0].model == "claude-opus-4-20250514"


# ---------------------------------------------------------------------------
# β.3 — AgentCullMutator returns TopologyState
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_cull_removes_target_agent() -> None:
    mutator = AgentCullMutator()
    agents = [
        AgentCard(id="keep-1"),
        AgentCard(id="cull-me"),
        AgentCard(id="keep-2"),
    ]
    state = _state(agents)
    mutation = Mutation(
        kind=MutationKind.AGENT_CULL,
        diff=[
            MutationDiff(
                field_path="agents[id=cull-me]",
                before="cull-me",
                after=None,
            )
        ],
    )
    new_state = await mutator.apply(mutation, state)
    assert [a.id for a in new_state.agents] == ["keep-1", "keep-2"]


# ---------------------------------------------------------------------------
# Legacy list[AgentCard] signature still accepted (backwards compat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_list_signature_still_works() -> None:
    """Old callers passing bare agent lists keep working post-β.3.

    This is the compat shim in ``_coerce_to_state`` — without it, every
    caller site in the codebase would break at the same time.
    """
    mutator = ModelSwapMutator()
    agents = [AgentCard(id="a1", model="claude-opus-4-20250514")]
    mutation = Mutation(
        kind=MutationKind.MODEL_SWAP,
        diff=[
            MutationDiff(
                field_path="agents[id=a1].model",
                before="claude-opus-4-20250514",
                after="claude-sonnet-4-20250514",
            )
        ],
    )
    result = await mutator.apply(mutation, agents)  # type: ignore[arg-type]
    assert isinstance(result, TopologyState)
    assert result.agents[0].model == "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# End-to-end: mutator + orchestrator.swap_topology
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parameter_tune_plus_swap_actually_changes_config() -> None:
    """Gap #5 fix: a timeout mutation makes it into the orchestrator config."""
    orch = MetaOrchestrator()
    adapter = MockLLMFlowAdapter()
    orch.set_adapter(adapter)
    await adapter.load("unused")

    original_timeout = orch.config.default_timeout_seconds
    current = orch.current_state()
    mutator = ParameterTuneMutator()
    mutation = Mutation(
        kind=MutationKind.PARAMETER_TUNE,
        diff=[MutationDiff(field_path="config.timeout_seconds")],
    )

    new_state = await mutator.apply(mutation, current)
    orch.swap_topology(new_state)

    assert orch.config.default_timeout_seconds == pytest.approx(original_timeout * 1.5)


@pytest.mark.asyncio
async def test_prompt_rewrite_plus_swap_updates_agent_in_topology() -> None:
    """β.2 + β.1: a prompt rewrite landed via orchestrator is visible on next read."""
    fake = FakeLLMClient()
    fake.queue(
        PromptRewriteResponse,
        {
            "rewritten_prompt": (
                "You are a rate-limit-aware researcher. Always back off "
                "exponentially on 429 responses before retrying."
            ),
        },
    )
    mutator = PromptRewriteMutator(llm_client=fake)

    orch = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(agent_names=("researcher",))
    orch.set_adapter(adapter)
    await adapter.load("unused")

    # Seed a prompt on the live agent so the mutator has something to rewrite.
    state = orch.current_state()
    state.agents[0].system_prompt = "You are a helper."
    orch.swap_topology(state)

    mutation = Mutation(
        kind=MutationKind.PROMPT_REWRITE,
        description="Rewrite prompt to handle: 'Rate limit exceeded'",
        diff=[MutationDiff(field_path=f"agents[id={state.agents[0].id}].system_prompt")],
    )

    new_state = await mutator.apply(mutation, orch.current_state())
    orch.swap_topology(new_state)

    agent = orch.topology()[0]
    assert agent.system_prompt is not None
    assert "rate" in agent.system_prompt.lower()


# ---------------------------------------------------------------------------
# Propose step: PromptRewriteMutator picks an agent id from recent error events
# ---------------------------------------------------------------------------


def test_propose_picks_agent_from_recent_error_event() -> None:
    mutator = PromptRewriteMutator()
    err_event = RunEvent(
        kind=RunEventKind.ERROR,
        agent_id="researcher",
        error="Rate limit exceeded",
        run_id="r1",
    )
    results = [
        RunResult(
            task_id=f"t{i}",
            status=RunStatus.FAILED,
            errors=["Rate limit exceeded"],
            events=[err_event],
            cost=CostSummary(total_cost=Decimal("0.01")),
        )
        for i in range(3)
    ]
    proposal = mutator.propose(results)
    assert proposal is not None
    assert "researcher" in proposal.diff[0].field_path
