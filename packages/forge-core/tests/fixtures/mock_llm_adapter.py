"""Mock adapter fixture: mimics what a framework-specific adapter looks
like after Fase α wiring.

Instead of requiring LangGraph to be installed (heavy, slow, and flaky
under CI), we build a minimal adapter that exercises the same integration
points the real LangGraph adapter does:

* Accepts an :class:`LLMCallInterceptor` via ``set_interceptor``
* Reads ``current_run()`` to tag agent events with the active run_id
* Calls ``interceptor.on_llm_start`` / ``on_llm_end`` the way a callback
  handler would, producing real ``LLM_CALL`` events with populated tokens

If the cost aggregation works for this mock, it works for any adapter
that honors the :class:`LLMCallInterceptor` protocol — which is exactly
the architectural promise of Fase α.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from forge_adapters.base import BaseAdapter
from forge_core.context import current_run
from forge_core.types import (
    AgentCard,
    RunEvent,
    RunEventKind,
    TaskEnvelope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from forge_core.protocols import LLMCallInterceptor


class MockLLMFlowAdapter(BaseAdapter):
    """Fixture adapter that simulates an N-agent flow with predictable LLM usage.

    Each call to :meth:`run` invokes ``num_llm_calls`` simulated LLM calls
    through the interceptor, each with the configured ``model``,
    ``input_tokens``, ``output_tokens``. Useful for integration tests that
    need to assert on cost aggregation without spinning up a real framework.
    """

    def __init__(
        self,
        *,
        model: str = "claude-haiku-4-20250514",
        input_tokens: int = 100,
        output_tokens: int = 50,
        num_llm_calls: int = 2,
        agent_names: tuple[str, ...] = ("planner", "researcher"),
    ) -> None:
        super().__init__()
        self._interceptor: LLMCallInterceptor | None = None
        self._model = model
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._num_llm_calls = num_llm_calls
        self._agent_names = agent_names

    @property
    def name(self) -> str:
        return "mock-llm-flow"

    def detect(self, source: str | Path) -> bool:
        # This adapter is for tests only — never auto-detected.
        return False

    def set_interceptor(self, interceptor: LLMCallInterceptor) -> None:
        """Accept the orchestrator-owned interceptor (the Fase α contract)."""
        self._interceptor = interceptor

    async def _load_impl(self, source: str | Path) -> list[AgentCard]:
        # Build one AgentCard per configured agent name.
        return [
            AgentCard(id=f"agent-{i}", name=name, role="worker", model=self._model)
            for i, name in enumerate(self._agent_names)
        ]

    async def _execute(self, envelope: TaskEnvelope) -> tuple[Any, list[RunEvent]]:
        """Run ``num_llm_calls`` simulated calls through the interceptor."""
        events: list[RunEvent] = []
        ctx = current_run()
        run_id = ctx.run_id if ctx is not None else None

        events.append(
            RunEvent(
                kind=RunEventKind.AGENT_START,
                run_id=run_id,
                agent_id="orchestrator",
                data={"input": envelope.input},
            )
        )

        assert self._interceptor is not None, (
            "Test adapter was never wired to an interceptor — "
            "this indicates the orchestrator failed to call set_interceptor. "
            "Fixing that is the whole point of Fase α."
        )

        outputs: list[str] = []
        for i in range(self._num_llm_calls):
            agent_id = self._agent_names[i % len(self._agent_names)]
            span_id = await self._interceptor.on_llm_start(
                model=self._model,
                agent_id=agent_id,
                metadata={"call_index": i},
            )
            await self._interceptor.on_llm_end(
                span_id,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                output=f"mock-response-{i}",
            )
            outputs.append(f"mock-response-{i}")

        events.append(
            RunEvent(
                kind=RunEventKind.AGENT_END,
                run_id=run_id,
                agent_id="orchestrator",
                data={"num_llm_calls": self._num_llm_calls},
            )
        )

        return {"responses": outputs}, events

    async def stream(self, envelope: TaskEnvelope) -> AsyncIterator[RunEvent]:
        result = await self.run(envelope)
        for event in result.events:
            yield event
