"""Integration-test scaffolding for Forge (fase ε.3).

Two primitives live here:

* :class:`MockLLMProvider` — a programmable, deterministic LLM stand-in.
  Feed it a script of responses; every call returns the next one with
  token counts you specify. Also implements the structured_call contract
  from :class:`forge_core.evolution.llm_client.LLMClient`, so the same
  fixture drives the evolution loop's structured mutations.

* :class:`ForgeTestHarness` — a thin wrapper around
  :class:`MetaOrchestrator` that wires up the mock provider, binds a
  test-only interceptor that publishes populated ``LLM_CALL`` events to
  the bus, and exposes quality-of-life helpers (``run_flow``, access to
  collected events and the journal).

Why this belongs in ``forge-core`` and not in a test package: users of
Forge writing their own adapters need the same harness to write their
own acceptance tests. Keeping it in-library (and covered by mypy/ruff)
means it can't rot the way fixture-only test helpers typically do.

Design notes
------------

The mock provider does **not** monkey-patch any real SDK. Integration
tests that want to exercise the LangChain/Anthropic paths should use the
real SDKs against a recorded cassette or a local stub server — that's a
different class of test (``integration`` marker) and is not what this
harness is for. Here we mock at the Forge boundary: the orchestrator
gets real events with real token counts, and the rest of the stack
(metrics, tracer, cost model, evolution) sees genuine data.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, TypeVar
from uuid import uuid4

import structlog
from pydantic import BaseModel

from forge_core.context import RunContext, run_scope
from forge_core.events import EventBus
from forge_core.evolution.llm_client import LLMStructuredCallError
from forge_core.types import RunEvent, RunEventKind

logger = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# MockResponse + MockLLMProvider
# ---------------------------------------------------------------------------


@dataclass
class MockResponse:
    """One scripted LLM response.

    ``content`` is the text the model "returned"; ``input_tokens`` /
    ``output_tokens`` are what the harness will report to the
    interceptor. Set them to realistic values so downstream cost
    calculations produce realistic numbers.

    ``structured`` is an optional Pydantic payload for structured calls.
    When :meth:`MockLLMProvider.structured_call` is invoked and this
    field is populated, it's validated against the requested schema and
    returned directly (skipping ``content``). Use one or the other in a
    given queued response, not both.
    """

    content: str = ""
    input_tokens: int = 10
    output_tokens: int = 20
    model: str = "mock-model"
    structured: dict[str, Any] | None = None


class MockLLMProvider:
    """Deterministic LLM provider for tests.

    Scripted responses are consumed in FIFO order. Reaching the end of
    the script raises :class:`LLMStructuredCallError` (for structured
    calls) or :class:`RuntimeError` (for :meth:`complete`) — never
    silently returning empty — because a test running out of scripted
    responses is always a bug, not a feature.

    The provider is thread-safe within a single event loop; it is not
    designed for cross-loop concurrency (tests that need that should
    instantiate one per loop).
    """

    def __init__(self, responses: list[MockResponse] | None = None) -> None:
        self._queue: list[MockResponse] = list(responses or [])
        self._calls: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    # ---- programming API -------------------------------------------------

    def program(self, responses: list[str | MockResponse]) -> MockLLMProvider:
        """Append a batch of responses. Strings become default :class:`MockResponse`."""
        for r in responses:
            self._queue.append(r if isinstance(r, MockResponse) else MockResponse(content=r))
        return self

    def respond_with(
        self,
        content: str,
        *,
        input_tokens: int = 10,
        output_tokens: int = 20,
        model: str = "mock-model",
    ) -> MockLLMProvider:
        """Append a single response with explicit token counts."""
        self._queue.append(
            MockResponse(
                content=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
            )
        )
        return self

    def queue_structured(self, payload: dict[str, Any]) -> MockLLMProvider:
        """Append a structured-only response (for evolution's LLMClient calls)."""
        self._queue.append(MockResponse(structured=payload))
        return self

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Immutable view of every call this provider has served, in order."""
        return list(self._calls)

    @property
    def pending(self) -> int:
        return len(self._queue)

    # ---- call APIs --------------------------------------------------------

    async def complete(
        self,
        *,
        prompt: str,
        model: str | None = None,
        agent_id: str | None = None,
    ) -> MockResponse:
        """Return the next queued response and record the call.

        If ``interceptor`` was attached via :meth:`with_interceptor`, a
        populated ``LLM_CALL`` event is emitted through it. Otherwise,
        the caller is responsible for dispatching events.
        """
        async with self._lock:
            if not self._queue:
                raise RuntimeError(
                    "MockLLMProvider script exhausted — more LLM calls were made "
                    "than the test programmed responses for."
                )
            response = self._queue.pop(0)
            self._calls.append(
                {
                    "kind": "complete",
                    "model": model or response.model,
                    "agent_id": agent_id,
                    "prompt": prompt,
                }
            )
        return response

    async def structured_call(
        self,
        *,
        model: str,
        schema: type[T],
        system: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> T:
        """LLMClient-compatible structured call.

        Consumes the next queued response's ``structured`` payload and
        validates it against ``schema``. Falls back to ``content`` if no
        structured payload is queued and ``content`` parses as the
        schema — useful for simple test shapes.
        """
        async with self._lock:
            if not self._queue:
                raise LLMStructuredCallError(
                    f"MockLLMProvider has no queued response for schema {schema.__name__}"
                )
            response = self._queue.pop(0)
            self._calls.append(
                {
                    "kind": "structured",
                    "model": model,
                    "schema": schema.__name__,
                    "system": system,
                    "prompt": prompt,
                }
            )
        if response.structured is None:
            raise LLMStructuredCallError(
                f"Queued MockResponse has no 'structured' payload for schema {schema.__name__}"
            )
        try:
            return schema.model_validate(response.structured)
        except Exception as exc:
            raise LLMStructuredCallError(
                f"Mock payload did not match {schema.__name__}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Test interceptor — publishes real events without touching any SDK
# ---------------------------------------------------------------------------


class _TestInterceptor:
    """Minimal :class:`LLMCallInterceptor` that emits events to a bus.

    Not a mock of the real :class:`ForgeLLMInterceptor` — it is the
    contract, simplified: open/close span_ids are UUIDs, events go
    straight onto the bus with accurate tokens and a synthetic cost
    derived from ``cost_per_call`` so tests can assert > 0 without
    depending on the pricing table.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        cost_per_call: Decimal = Decimal("0.001"),
    ) -> None:
        self._bus = bus
        self._cost_per_call = cost_per_call
        self._open: dict[str, dict[str, Any]] = {}

    async def on_llm_start(
        self,
        *,
        model: str,
        agent_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        span_id = uuid4().hex
        self._open[span_id] = {
            "model": model,
            "agent_id": agent_id,
            "tool_name": tool_name,
            "metadata": dict(metadata or {}),
        }
        return span_id

    async def on_llm_end(
        self,
        span_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        output: Any = None,
    ) -> None:
        ctx = self._open.pop(span_id, {})
        event = RunEvent(
            kind=RunEventKind.LLM_CALL,
            agent_id=ctx.get("agent_id"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=self._cost_per_call,
            latency_ms=0.0,
            model=ctx.get("model"),
            data={**(ctx.get("metadata") or {})},
        )
        await self._bus.publish(event)

    async def on_llm_error(self, span_id: str, error: BaseException) -> None:
        ctx = self._open.pop(span_id, {})
        event = RunEvent(
            kind=RunEventKind.ERROR,
            agent_id=ctx.get("agent_id"),
            model=ctx.get("model"),
            error=repr(error),
        )
        await self._bus.publish(event)


# ---------------------------------------------------------------------------
# ForgeTestHarness
# ---------------------------------------------------------------------------


FlowCallable = Callable[[dict[str, Any], MockLLMProvider], Awaitable[Any]]


@dataclass
class HarnessRun:
    """The outcome of a single :meth:`ForgeTestHarness.run_flow` call."""

    output: Any
    events: list[RunEvent] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: Decimal = Decimal("0")


class ForgeTestHarness:
    """End-to-end harness that avoids real SDKs.

    Usage::

        harness = ForgeTestHarness()
        harness.llm.program(["first answer", "second answer"])

        async def my_flow(inp, llm):
            r = await llm.complete(prompt=inp["q"], agent_id="planner")
            return r.content

        result = await harness.run_flow(my_flow, {"q": "hi"})
        assert result.total_cost > Decimal("0")

    The harness composes:

    * an :class:`EventBus` that the test reads from via
      :attr:`HarnessRun.events`;
    * a :class:`_TestInterceptor` subscribed to nothing and open for the
      flow callable to drive;
    * a :class:`MockLLMProvider` whose ``complete`` auto-dispatches
      ``on_llm_start`` / ``on_llm_end`` so authors of flow callables get
      instrumentation for free.
    """

    def __init__(
        self,
        *,
        cost_per_call: Decimal = Decimal("0.001"),
        bus: EventBus | None = None,
    ) -> None:
        self._bus = bus or EventBus()
        self._interceptor = _TestInterceptor(self._bus, cost_per_call=cost_per_call)
        self._llm = MockLLMProvider()
        self._collected: list[RunEvent] = []
        self._bus.subscribe("*", self._collect)

    # ---- accessors --------------------------------------------------------

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def llm(self) -> MockLLMProvider:
        return self._llm

    @property
    def interceptor(self) -> _TestInterceptor:
        return self._interceptor

    @property
    def events(self) -> list[RunEvent]:
        return list(self._collected)

    async def _collect(self, event: RunEvent) -> None:
        self._collected.append(event)

    # ---- public API -------------------------------------------------------

    async def run_flow(
        self,
        flow: FlowCallable,
        input: dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> HarnessRun:
        """Execute ``flow`` under a fresh :func:`run_scope` and collect results.

        Every :meth:`MockLLMProvider.complete` call inside ``flow`` is
        automatically wrapped with start/end interceptor hooks, so tests
        that assert on ``RunEvent`` streams see realistic data without
        the flow author writing interceptor glue.
        """
        run_id = run_id or uuid4().hex
        ctx = RunContext(run_id=run_id, task_id=uuid4().hex)

        # Wrap the provider's complete() so each call emits start/end events.
        original_complete = self._llm.complete

        async def instrumented_complete(
            *, prompt: str, model: str | None = None, agent_id: str | None = None
        ) -> MockResponse:
            span_id = await self._interceptor.on_llm_start(
                model=model or "mock-model", agent_id=agent_id
            )
            try:
                response = await original_complete(prompt=prompt, model=model, agent_id=agent_id)
            except BaseException as exc:
                await self._interceptor.on_llm_error(span_id, exc)
                raise
            await self._interceptor.on_llm_end(
                span_id,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                output=response.content,
            )
            return response

        self._llm.complete = instrumented_complete  # type: ignore[method-assign]

        before = len(self._collected)
        try:
            with run_scope(ctx):
                output = await flow(input, self._llm)
        finally:
            self._llm.complete = original_complete  # type: ignore[method-assign]

        new_events = self._collected[before:]
        total_in = sum(e.input_tokens for e in new_events)
        total_out = sum(e.output_tokens for e in new_events)
        total_cost = sum((e.cost for e in new_events), Decimal("0"))

        return HarnessRun(
            output=output,
            events=new_events,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_cost=total_cost,
        )


__all__ = [
    "ForgeTestHarness",
    "HarnessRun",
    "MockLLMProvider",
    "MockResponse",
]
