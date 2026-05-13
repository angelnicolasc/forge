"""Generic callable adapter for Forge.

Wraps any async or sync callable into Forge's Adapter interface. Useful for
custom agent flows that don't go through LangGraph, CrewAI, or AutoGen —
anything you can express as "a function that takes an input dict and returns
an answer."

Instrumentation strategy
------------------------

Unlike the LangGraph adapter (which has LangChain callbacks) or the CrewAI /
AutoGen adapters (which have monkey-patch targets inside well-known library
classes), a generic callable is opaque. Forge has no idea whether the user's
function calls ``anthropic.Anthropic(...)``, ``openai.OpenAI(...)``, a local
model, or nothing at all.

To close this gap without requiring the user to thread a callback through
their code, the generic adapter scopes a monkey-patch of the common LLM SDKs
(``anthropic``, ``openai``, ``google.generativeai``) for the duration of the
run, routed through :func:`forge_observe.llm_clients.instrument_llm_clients`.
Any ``.create()`` / ``.generate_content()`` call inside the user's callable
flows through Forge's :class:`LLMCallInterceptor`, so cost and tokens populate
exactly the same way they would in a LangGraph flow.

This is imperfect by construction: we can only instrument SDKs we know about,
and we rely on the SDKs being imported by the time the user's callable runs.
The instrumentation degrades silently (no events) rather than failing when an
SDK isn't present.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import TYPE_CHECKING, Any

from forge_adapters.base import BaseAdapter
from forge_core.context import current_run
from forge_core.types import AgentCard, RunEvent, RunEventKind, TaskEnvelope

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from forge_core.events import EventBus
    from forge_core.protocols import LLMCallInterceptor


class GenericCallableAdapter(BaseAdapter):
    """Adapter that wraps any callable into a Forge flow."""

    def __init__(
        self,
        fn: Callable[..., Any] | None = None,
        adapter_name: str = "generic",
    ) -> None:
        super().__init__()
        self._fn = fn
        self._adapter_name = adapter_name
        self._interceptor: LLMCallInterceptor | None = None
        self._bus: EventBus | None = None

    @property
    def name(self) -> str:
        return self._adapter_name

    # ------------------------------------------------------------------
    # Orchestrator wiring (Fase α contract — mirrored across all adapters)
    # ------------------------------------------------------------------

    def set_interceptor(self, interceptor: LLMCallInterceptor) -> None:
        """Inject the orchestrator's LLM interceptor.

        Called by ``MetaOrchestrator._wire_adapter`` after instantiation.
        The interceptor is what turns raw SDK calls into bus events — we
        pass it straight through to
        :func:`forge_observe.llm_clients.instrument_llm_clients` on each
        run.
        """
        self._interceptor = interceptor

    def set_bus(self, bus: EventBus) -> None:
        """Accept the orchestrator's event bus.

        The generic adapter itself doesn't publish directly; all telemetry
        originates in the interceptor which owns the bus reference. We
        cache this only so tests and debug tooling can inspect wiring.
        """
        self._bus = bus

    # ------------------------------------------------------------------
    # BaseAdapter contract
    # ------------------------------------------------------------------

    def detect(self, source: str | Path) -> bool:
        return False  # Generic adapter is never auto-detected.

    async def _load_impl(self, source: str | Path) -> list[AgentCard]:
        return [
            AgentCard(
                name=self._adapter_name,
                role="Generic callable flow",
                metadata={"framework": "generic", "source": str(source)},
            )
        ]

    async def _execute(self, envelope: TaskEnvelope) -> tuple[Any, list[RunEvent]]:
        if self._fn is None:
            raise RuntimeError("No callable provided to GenericCallableAdapter.")

        events: list[RunEvent] = []
        run_ctx = current_run()
        run_id = run_ctx.run_id if run_ctx is not None else None

        events.append(
            RunEvent(
                kind=RunEventKind.AGENT_START,
                run_id=run_id,
                agent_id=self._adapter_name,
                data={"input": envelope.input},
            )
        )

        start = time.perf_counter()
        async with self._instrumented_scope() as instrumentation_report:
            # We probe coroutine-function status on the ORIGINAL callable
            # rather than whatever the user stored, because an SDK wrapper
            # isn't a coroutine function itself even when its ``original``
            # was. The user's function hasn't been wrapped — just the SDKs
            # it calls — so ``self._fn`` is untouched here.
            if inspect.iscoroutinefunction(self._fn):
                output = await self._fn(envelope.input)
            else:
                # Sync callables run on a worker thread; any in-flight SDK
                # call inside them will see the patched method and marshal
                # telemetry back via ``asyncio.run_coroutine_threadsafe``
                # (see ``forge_observe.llm_clients._make_sync_wrapper``).
                output = await asyncio.to_thread(self._fn, envelope.input)

        elapsed_ms = (time.perf_counter() - start) * 1000

        # Publish ADAPTER_DEGRADED for each SDK whose patch failed so the
        # problem is visible in the SSE stream, not just in log lines.
        if (
            instrumentation_report is not None
            and self._bus is not None
            and instrumentation_report.failed
        ):
            for sdk_name in instrumentation_report.failed:
                events.append(
                    RunEvent(
                        kind=RunEventKind.ADAPTER_DEGRADED,
                        run_id=run_id,
                        agent_id=self._adapter_name,
                        data={
                            "adapter": "generic",
                            "sdk": sdk_name,
                            "reason": "patch_failed",
                            "impact": "cost_tracking_disabled_for_this_sdk",
                        },
                    )
                )
        events.append(
            RunEvent(
                kind=RunEventKind.AGENT_END,
                run_id=run_id,
                agent_id=self._adapter_name,
                latency_ms=elapsed_ms,
                data={"output_type": type(output).__name__},
            )
        )

        return output, events

    # ------------------------------------------------------------------
    # Instrumentation scope
    # ------------------------------------------------------------------

    class _NoOpScope:
        async def __aenter__(self) -> GenericCallableAdapter._NoOpScope:
            return self

        async def __aexit__(self, *exc_info: Any) -> None:
            return None

    def _instrumented_scope(self) -> Any:
        """Return an async context manager wrapping the user callable.

        When no interceptor is wired (e.g., the adapter is used standalone
        without a MetaOrchestrator) we return a no-op so the adapter still
        works. When wired, we delegate to
        :func:`forge_observe.llm_clients.instrument_llm_clients`.

        The import is lazy to keep ``forge-adapters`` installable without
        ``forge-observe`` — users who don't care about cost attribution
        shouldn't be forced to pull in OpenTelemetry.
        """
        if self._interceptor is None:
            return GenericCallableAdapter._NoOpScope()
        try:
            from forge_observe.llm_clients import instrument_llm_clients
        except ImportError:
            # forge-observe isn't installed; can't instrument SDKs. Still
            # run the callable — we just won't see LLM events.
            return GenericCallableAdapter._NoOpScope()

        # ``instrument_llm_clients`` is a sync contextmanager yielding a
        # report; wrap it in an async facade so ``async with`` works.
        return _AsyncCMAdapter(
            instrument_llm_clients(self._interceptor, agent_id=self._adapter_name)
        )


class _AsyncCMAdapter:
    """Turn a sync ``contextmanager`` into an async one.

    ``instrument_llm_clients`` is a ``@contextmanager`` (sync) because its
    setup/teardown touches no I/O — just attribute swaps. Wrapping it here
    is cheaper than making it an ``@asynccontextmanager`` upstream (which
    would force awaits at every call site, some of which are themselves
    sync).
    """

    __slots__ = ("_cm", "_report")

    def __init__(self, cm: Any) -> None:
        self._cm = cm
        self._report: Any = None

    async def __aenter__(self) -> Any:
        self._report = self._cm.__enter__()
        return self._report

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._cm.__exit__(exc_type, exc, tb)


Adapter = GenericCallableAdapter
