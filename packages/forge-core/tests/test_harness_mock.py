"""Tests for ForgeTestHarness + MockLLMProvider (fase ε.3)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel

from forge_core.evolution.llm_client import LLMStructuredCallError
from forge_core.testing import (
    ForgeTestHarness,
    HarnessRun,
    MockLLMProvider,
    MockResponse,
)
from forge_core.types import RunEventKind

# ---------------------------------------------------------------------------
# MockLLMProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_returns_scripted_responses_in_order() -> None:
    p = MockLLMProvider()
    p.program(["one", "two"])
    a = await p.complete(prompt="q1")
    b = await p.complete(prompt="q2")
    assert a.content == "one"
    assert b.content == "two"
    assert p.pending == 0


@pytest.mark.asyncio
async def test_respond_with_sets_token_counts() -> None:
    p = MockLLMProvider()
    p.respond_with("answer", input_tokens=100, output_tokens=50, model="claude-haiku")
    r = await p.complete(prompt="x", model="claude-haiku")
    assert r.input_tokens == 100
    assert r.output_tokens == 50
    assert r.content == "answer"


@pytest.mark.asyncio
async def test_complete_raises_when_script_exhausted() -> None:
    p = MockLLMProvider()
    with pytest.raises(RuntimeError, match="script exhausted"):
        await p.complete(prompt="x")


@pytest.mark.asyncio
async def test_calls_log_records_every_call() -> None:
    p = MockLLMProvider()
    p.program(["a", "b"])
    await p.complete(prompt="first", agent_id="a1")
    await p.complete(prompt="second", agent_id="a2")
    assert len(p.calls) == 2
    assert p.calls[0]["agent_id"] == "a1"
    assert p.calls[1]["prompt"] == "second"


class _Schema(BaseModel):
    answer: str
    score: float


@pytest.mark.asyncio
async def test_structured_call_validates_payload() -> None:
    p = MockLLMProvider()
    p.queue_structured({"answer": "yes", "score": 0.9})
    result = await p.structured_call(model="m", schema=_Schema, system="sys", prompt="p")
    assert isinstance(result, _Schema)
    assert result.answer == "yes"
    assert result.score == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_structured_call_rejects_missing_payload() -> None:
    p = MockLLMProvider()
    p.program(["plain text, not structured"])  # queued as content-only
    with pytest.raises(LLMStructuredCallError):
        await p.structured_call(model="m", schema=_Schema, system="sys", prompt="p")


@pytest.mark.asyncio
async def test_structured_call_rejects_invalid_payload() -> None:
    p = MockLLMProvider()
    p.queue_structured({"answer": "ok"})  # missing required `score`
    with pytest.raises(LLMStructuredCallError):
        await p.structured_call(model="m", schema=_Schema, system="sys", prompt="p")


# ---------------------------------------------------------------------------
# ForgeTestHarness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_flow_emits_llm_call_events_with_real_tokens() -> None:
    harness = ForgeTestHarness()
    harness.llm.program(
        [
            MockResponse(content="hello", input_tokens=12, output_tokens=5),
            MockResponse(content="world", input_tokens=8, output_tokens=7),
        ]
    )

    async def flow(inp, llm):
        a = await llm.complete(prompt=inp["q"], agent_id="planner")
        b = await llm.complete(prompt="step 2", agent_id="executor")
        return [a.content, b.content]

    result: HarnessRun = await harness.run_flow(flow, {"q": "hi"})

    assert result.output == ["hello", "world"]
    llm_events = [e for e in result.events if e.kind == RunEventKind.LLM_CALL]
    assert len(llm_events) == 2
    assert result.total_input_tokens == 20
    assert result.total_output_tokens == 12
    assert result.total_cost > Decimal("0")


@pytest.mark.asyncio
async def test_run_flow_assigns_agent_id_on_events() -> None:
    harness = ForgeTestHarness()
    harness.llm.program(["ok"])

    async def flow(_inp, llm):
        await llm.complete(prompt="x", agent_id="solver")
        return "done"

    result = await harness.run_flow(flow, {})
    assert result.events[0].agent_id == "solver"


@pytest.mark.asyncio
async def test_run_flow_emits_error_event_on_exception_inside_llm_call() -> None:
    harness = ForgeTestHarness()
    # No responses programmed → complete() raises, which should emit an error.

    async def flow(_inp, llm):
        await llm.complete(prompt="x")
        return "never"

    with pytest.raises(RuntimeError):
        await harness.run_flow(flow, {})

    error_events = [e for e in harness.events if e.kind == RunEventKind.ERROR]
    assert len(error_events) == 1
    assert "script exhausted" in (error_events[0].error or "")


@pytest.mark.asyncio
async def test_harness_cost_per_call_configurable() -> None:
    harness = ForgeTestHarness(cost_per_call=Decimal("0.05"))
    harness.llm.program(["a", "b", "c"])

    async def flow(_inp, llm):
        for _ in range(3):
            await llm.complete(prompt="p")
        return None

    result = await harness.run_flow(flow, {})
    assert result.total_cost == Decimal("0.15")
