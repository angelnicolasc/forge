"""AutoGen adapter for Forge.

Wraps AutoGen AgentChat teams into Forge's uniform :class:`Adapter` interface
with Fase γ.2 instrumentation:

* A ``logging.Handler`` is attached to AutoGen's ``TRACE_LOGGER_NAME`` for
  the duration of each run. AutoGen 0.4+ emits structured ``LLMCallEvent``
  records through this logger containing model name, prompt tokens,
  completion tokens and agent id — exactly the data Forge needs to populate
  :class:`RunEventKind.LLM_CALL` events on the bus.
* We also listen for agent lifecycle events (``AgentStartEvent`` /
  ``AgentStopEvent``) that AutoGen emits on the same logger, producing the
  per-agent AGENT_START/AGENT_END bus events that CrewAI's adapter derives
  from monkey-patching.

Unlike the CrewAI adapter, which monkey-patches synchronous methods running
on a worker thread (where :func:`asyncio.run_coroutine_threadsafe` is the
natural bridge), the AutoGen log handler fires from inside the *same* event
loop that is awaiting ``team.run(...)``. Calling ``run_coroutine_threadsafe``
and blocking on the result there would deadlock the loop. So instead we
build populated :class:`RunEvent` objects directly — including a cost
computation we perform locally — and publish them via
:func:`asyncio.Task` scheduling (fire-and-forget). This preserves the bus
as the single source of truth for cost/token data without any async/sync
impedance mismatch.

The implementation is defensive: if an AutoGen version ships without the
expected logger or changes the event payload shape, missing fields degrade
gracefully to ``"unknown"`` / ``0`` rather than crashing the user's run.
The test-suite pushes :class:`logging.LogRecord` instances through the
handler directly, so instrumentation can be validated without standing up
a real AutoGen team.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from forge_adapters.base import BaseAdapter
from forge_core.context import current_run
from forge_core.types import AgentCard, RunEvent, RunEventKind, TaskEnvelope

if TYPE_CHECKING:
    from collections.abc import Iterator

    from forge_core.events import EventBus
    from forge_core.protocols import LLMCallInterceptor

logger = structlog.get_logger()

# AutoGen's structured trace logger name. Kept as a module-level constant so
# we don't depend on ``autogen_core`` being importable at module-load time.
_AUTOGEN_TRACE_LOGGER_NAME = "autogen_core.events"


class AutoGenAdapter(BaseAdapter):
    """Adapter for AutoGen agent teams with bus-level instrumentation."""

    def __init__(
        self,
        interceptor: LLMCallInterceptor | None = None,
        bus: EventBus | None = None,
    ) -> None:
        super().__init__()
        self._team: Any = None
        self._interceptor = interceptor
        self._bus = bus

    @property
    def name(self) -> str:
        return "autogen"

    # ------------------------------------------------------------------
    # Orchestrator wiring
    # ------------------------------------------------------------------

    def set_interceptor(self, interceptor: LLMCallInterceptor) -> None:
        self._interceptor = interceptor

    def set_bus(self, bus: EventBus) -> None:
        self._bus = bus

    # ------------------------------------------------------------------
    # Detection + loading
    # ------------------------------------------------------------------

    def detect(self, source: str | Path) -> bool:
        try:
            code = Path(source).read_text(encoding="utf-8")
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "autogen" in node.module:
                    return True
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "autogen" in alias.name:
                            return True
        except Exception:
            pass
        return False

    async def _load_impl(self, source: str | Path) -> list[AgentCard]:
        path = Path(source).resolve()
        spec = importlib.util.spec_from_file_location("_forge_user_flow", str(path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load module from {path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        team = getattr(module, "team", None)
        if team is None:
            build_fn = getattr(module, "build_team", None)
            if build_fn is not None:
                team = build_fn()

        if team is None:
            raise RuntimeError(
                f"No AutoGen team found in {path}. "
                "Expose a 'team' variable or 'build_team()' function."
            )

        self._team = team
        return self._extract_topology()

    def _extract_topology(self) -> list[AgentCard]:
        agents: list[AgentCard] = []
        try:
            participants = getattr(self._team, "_participants", []) or getattr(
                self._team, "participants", []
            )
            for agent in participants:
                agents.append(
                    AgentCard(
                        name=str(getattr(agent, "name", "unknown")),
                        role=str(getattr(agent, "description", "")),
                        metadata={"framework": "autogen"},
                    )
                )
        except Exception as exc:
            logger.warning("autogen.topology_extraction_failed", error=str(exc))
        return agents

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _execute(self, envelope: TaskEnvelope) -> tuple[Any, list[RunEvent]]:
        """Run the team with log-handler-based instrumentation."""
        events: list[RunEvent] = []
        ctx = current_run()
        run_id = ctx.run_id if ctx is not None else None

        events.append(
            RunEvent(
                kind=RunEventKind.AGENT_START,
                run_id=run_id,
                agent_id="autogen_team",
                data={"input": envelope.input},
            )
        )

        if self._interceptor is None and self._bus is None:
            logger.warning("autogen.no_wiring_running_uninstrumented")

        start = time.perf_counter()
        handler: _ForgeAutoGenLogHandler | None = None
        try:
            with self._instrument_autogen(run_id=run_id) as handler:
                task = envelope.input.get("task", str(envelope.input))
                result = await self._team.run(task=task)
        except Exception as exc:
            events.append(
                RunEvent(
                    kind=RunEventKind.ERROR,
                    run_id=run_id,
                    agent_id="autogen_team",
                    error=str(exc),
                )
            )
            if handler is not None:
                handler.close_pending_spans()
                await handler.drain()
            raise
        # Close orphaned spans and drain fire-and-forget publishes so the
        # caller sees a fully-populated event log (not a racy partial view).
        if handler is not None:
            handler.close_pending_spans()
            await handler.drain()

        elapsed_ms = (time.perf_counter() - start) * 1000
        events.append(
            RunEvent(
                kind=RunEventKind.AGENT_END,
                run_id=run_id,
                agent_id="autogen_team",
                latency_ms=elapsed_ms,
                data={"output_type": type(result).__name__},
            )
        )
        return result, events

    @contextmanager
    def _instrument_autogen(self, *, run_id: str | None) -> Iterator[_ForgeAutoGenLogHandler]:
        """Attach the AutoGen trace-log handler for the duration of the run.

        The logger name is a constant regardless of whether autogen-core is
        actually installed: ``logging.getLogger`` returns a logger in every
        case, and if nothing ever emits a record on it the handler simply
        never fires. That keeps this context manager safe even when AutoGen
        isn't on ``sys.path`` — the test-suite exploits this to drive the
        handler directly.
        """
        trace_logger = logging.getLogger(_AUTOGEN_TRACE_LOGGER_NAME)
        previous_level = trace_logger.level
        # AutoGen emits these at INFO/DEBUG; make sure we see them.
        if previous_level == logging.NOTSET or previous_level > logging.DEBUG:
            trace_logger.setLevel(logging.DEBUG)

        handler = _ForgeAutoGenLogHandler(
            bus=self._bus,
            interceptor=self._interceptor,
            run_id=run_id,
        )
        trace_logger.addHandler(handler)
        try:
            yield handler
        finally:
            trace_logger.removeHandler(handler)
            trace_logger.setLevel(previous_level)


Adapter = AutoGenAdapter


# ----------------------------------------------------------------------
# Log handler
# ----------------------------------------------------------------------


class _ForgeAutoGenLogHandler(logging.Handler):
    """Bridge from AutoGen's structured log records → Forge's bus/interceptor.

    AutoGen 0.4+ attaches the event object as ``record.event`` on each log
    record (an instance of ``LLMCallEvent`` etc., depending on the framework
    version). We duck-type on attribute names so the handler survives minor
    schema drift:

    * ``prompt_tokens`` / ``completion_tokens`` — preferred names, 0.4.x.
    * ``input_tokens`` / ``output_tokens`` — standardized in later versions.
    * ``agent_id``, ``model``, ``event_type`` — used when present.

    Unlike the LangChain callback (which runs in async contexts via
    ``AsyncCallbackHandler``), Python's :class:`logging.Handler.emit` is
    strictly synchronous — and AutoGen calls ``logger.info(...)`` from
    inside coroutines on the **same** event loop that is awaiting
    ``team.run(...)``. Blocking-waiting on the async interceptor from
    here would deadlock that loop. So we avoid the interceptor entirely:
    we compose the finalized :class:`RunEvent` ourselves (cost computed
    locally via :class:`DefaultCostModel`) and publish via fire-and-forget
    ``loop.create_task``. The bus subscribers then observe the event
    exactly as they would if it had come through the interceptor.
    """

    # Event-type discriminators AutoGen uses. We accept several spellings
    # because the schema has jittered across minor versions.
    _START_EVENT_TYPES = frozenset({"LLMCallEvent", "LLMCallStartEvent", "LLMStreamStartEvent"})
    _END_EVENT_TYPES = frozenset({"LLMCallEndEvent", "LLMStreamEndEvent"})
    _AGENT_START_TYPES = frozenset({"AgentStartEvent"})
    _AGENT_END_TYPES = frozenset({"AgentStopEvent", "AgentEndEvent"})

    def __init__(
        self,
        *,
        bus: EventBus | None,
        interceptor: LLMCallInterceptor | None,
        run_id: str | None,
    ) -> None:
        super().__init__(level=logging.DEBUG)
        self._bus = bus
        self._interceptor = interceptor
        self._run_id = run_id
        # call_id → start-time state we need to assemble the final LLM_CALL.
        # Stored directly rather than round-tripped through an async
        # interceptor to avoid sync/async deadlocks inside the handler.
        self._open_calls: dict[str, _PendingCall] = {}
        # Lazy-loaded cost model; resolved on first LLM_CALL end.
        self._cost_model: Any = None
        # Outstanding fire-and-forget tasks we scheduled. ``drain()`` awaits
        # them — tests and the adapter's teardown use this to avoid racing
        # against scheduled publishes that the event loop hasn't pumped yet.
        self._pending_tasks: list[asyncio.Task[Any]] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial wrapper
        """Dispatch a record; swallow any error so logging never breaks the run."""
        try:
            self._dispatch(record)
        except Exception as exc:
            # Never let an instrumentation bug bubble into the user's logger.
            structlog.get_logger().warning("autogen.handler_error", error=str(exc))

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, record: logging.LogRecord) -> None:
        event = getattr(record, "event", None)
        # Without a structured event object there's nothing actionable for us —
        # AutoGen also emits normal unstructured log lines.
        if event is None:
            return
        event_type = _attr(event, "event_type") or type(event).__name__

        if event_type in self._AGENT_START_TYPES:
            self._dispatch_agent_start(event)
            return
        if event_type in self._AGENT_END_TYPES:
            self._dispatch_agent_end(event)
            return
        if event_type in self._START_EVENT_TYPES:
            self._dispatch_llm_start(event)
            return
        if event_type in self._END_EVENT_TYPES:
            self._dispatch_llm_end(event)
            return

    def _dispatch_agent_start(self, event: Any) -> None:
        if self._bus is None:
            return
        agent_id = str(_attr(event, "agent_id") or _attr(event, "source") or "autogen_agent")
        self._publish(
            RunEvent(
                kind=RunEventKind.AGENT_START,
                run_id=self._run_id,
                agent_id=agent_id,
            )
        )

    def _dispatch_agent_end(self, event: Any) -> None:
        if self._bus is None:
            return
        agent_id = str(_attr(event, "agent_id") or _attr(event, "source") or "autogen_agent")
        latency_ms = _attr(event, "duration_ms") or 0.0
        self._publish(
            RunEvent(
                kind=RunEventKind.AGENT_END,
                run_id=self._run_id,
                agent_id=agent_id,
                latency_ms=float(latency_ms),
            )
        )

    def _dispatch_llm_start(self, event: Any) -> None:
        call_id = str(_attr(event, "call_id") or _attr(event, "id") or id(event))
        model = str(_attr(event, "model") or _attr(event, "model_name") or "unknown")
        agent_id = _attr(event, "agent_id")
        # span_id is a local UUID — we own its namespace, not AutoGen's.
        span_id = uuid.uuid4().hex[:16]
        self._open_calls[call_id] = _PendingCall(
            span_id=span_id,
            model=model,
            agent_id=str(agent_id) if agent_id is not None else None,
            start_perf=time.perf_counter(),
        )
        # Fire-and-forget interceptor notification so OTel spans still get
        # opened when the user wired one up. Because the interceptor is
        # idempotent on unknown span_ids at ``on_llm_end`` (the
        # ``orphan_end`` branch), skew between the handler and the
        # interceptor's own span_id is safe.
        if self._interceptor is not None:
            self._schedule(
                self._interceptor.on_llm_start(
                    model=model,
                    agent_id=str(agent_id) if agent_id is not None else None,
                    metadata={"source": "autogen", "call_id": call_id},
                )
            )

    def _dispatch_llm_end(self, event: Any) -> None:
        call_id = str(_attr(event, "call_id") or _attr(event, "id") or id(event))
        pending = self._open_calls.pop(call_id, None)
        if pending is None:
            # Orphaned end event — nothing to pair with. AutoGen sometimes
            # emits only end for streamed calls we didn't see start for.
            return

        input_tokens = int(
            _attr(event, "prompt_tokens")
            or _attr(event, "input_tokens")
            or _attr(event, "usage_input_tokens")
            or 0
        )
        output_tokens = int(
            _attr(event, "completion_tokens")
            or _attr(event, "output_tokens")
            or _attr(event, "usage_output_tokens")
            or 0
        )
        output = _attr(event, "response") or _attr(event, "output")
        latency_ms = (time.perf_counter() - pending.start_perf) * 1000.0

        cost = self._compute_cost(pending.model, input_tokens, output_tokens)

        self._publish(
            RunEvent(
                kind=RunEventKind.LLM_CALL,
                run_id=self._run_id,
                agent_id=pending.agent_id,
                model=pending.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
                latency_ms=latency_ms,
                span_id=pending.span_id,
                data={"source": "autogen", "call_id": call_id},
            )
        )

        # Schedule the interceptor's on_llm_end too so OTel spans it may
        # have opened get closed. We can't know its span_id (it generated
        # its own inside on_llm_start); pass ours — the orphan_end branch
        # will log and move on. Not ideal, but it's the only honest option
        # given the API.
        if self._interceptor is not None:
            self._schedule(
                self._interceptor.on_llm_end(
                    pending.span_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    output=output,
                )
            )

    def _compute_cost(self, model: str, input_tokens: int, output_tokens: int) -> Any:
        """Resolve pricing via :class:`forge_observe.cost_model.DefaultCostModel`.

        Lazy-imported the first time to avoid forcing ``forge-observe`` as a
        hard dependency for users who only install ``forge-adapters``. If
        the import fails, we fall back to zero cost with a one-shot warning
        (matching the behavior of the unknown-model path in the default
        cost model itself).
        """
        if self._cost_model is None:
            try:
                from forge_observe.cost_model import DefaultCostModel

                self._cost_model = DefaultCostModel()
            except ImportError:
                structlog.get_logger().warning(
                    "autogen.cost_model_unavailable_forge_observe_missing"
                )
                self._cost_model = _ZeroCostModel()
        return self._cost_model.cost(model, input_tokens, output_tokens)

    # ------------------------------------------------------------------
    # Scheduling helpers (tracked variants of the module-level fns so
    # ``drain()`` can await every fire-and-forget we emitted).
    # ------------------------------------------------------------------

    def _schedule(self, coro: Any) -> None:
        if coro is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — sync fallback (only in purely sync test harnesses).
            with suppress(Exception):
                asyncio.run(coro)
            return
        try:
            task = loop.create_task(coro)
        except RuntimeError:
            with suppress(Exception):
                asyncio.run(coro)
            return
        task.add_done_callback(_swallow_task_result)
        self._pending_tasks.append(task)

    def _publish(self, event: RunEvent) -> None:
        if self._bus is None:
            return
        self._schedule(self._bus.publish(event))

    async def drain(self) -> None:
        """Await every fire-and-forget task this handler has queued.

        Safe to call repeatedly. Cancelled or already-finished tasks are
        tolerated — we only care that no publish is still in flight when
        control returns.
        """
        if not self._pending_tasks:
            return
        pending = [t for t in self._pending_tasks if not t.done()]
        self._pending_tasks = []
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close_pending_spans(self) -> int:
        """Close any LLM calls we saw ``start`` for but never ``end`` for.

        Returns the number of orphans. The bus gets a final LLM_CALL event
        with zero tokens so downstream dashboards don't show a permanently
        open call, matching the ``abandoned_span`` path in ForgeLLMInterceptor.
        """
        orphans = list(self._open_calls.items())
        self._open_calls.clear()
        for call_id, pending in orphans:
            self._publish(
                RunEvent(
                    kind=RunEventKind.LLM_CALL,
                    run_id=self._run_id,
                    agent_id=pending.agent_id,
                    model=pending.model,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=(time.perf_counter() - pending.start_perf) * 1000.0,
                    span_id=pending.span_id,
                    data={"source": "autogen", "call_id": call_id, "abandoned": True},
                )
            )
            if self._interceptor is not None:
                self._schedule(
                    self._interceptor.on_llm_end(
                        pending.span_id, input_tokens=0, output_tokens=0, output=None
                    )
                )
        return len(orphans)


class _PendingCall:
    """Per-in-flight-call state for ``_ForgeAutoGenLogHandler``.

    Explicit class rather than a tuple/dataclass so ``__slots__`` keeps the
    hot path allocation small — AutoGen can emit thousands of LLM events
    across a long-running team session.
    """

    __slots__ = ("agent_id", "model", "span_id", "start_perf")

    def __init__(
        self,
        *,
        span_id: str,
        model: str,
        agent_id: str | None,
        start_perf: float,
    ) -> None:
        self.span_id = span_id
        self.model = model
        self.agent_id = agent_id
        self.start_perf = start_perf


class _ZeroCostModel:
    """Fallback when ``forge-observe`` isn't available in the install.

    Returns ``Decimal(0)`` for any call so the rest of the cost pipeline
    keeps working; the unknown-model warning at import is the only signal
    to the operator that pricing isn't active.
    """

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> Any:
        from decimal import Decimal

        return Decimal("0")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _attr(obj: Any, name: str) -> Any:
    """Look up ``name`` on ``obj`` or ``obj[name]`` — AutoGen events vary."""
    if obj is None:
        return None
    value = getattr(obj, name, None)
    if value is not None:
        return value
    if isinstance(obj, dict):
        return obj.get(name)
    return None


def _schedule_task(coro: Any) -> None:
    """Schedule a coroutine on the running loop, fire-and-forget.

    Used for interceptor notifications that can't block the log handler —
    the handler runs inside the same event loop that's awaiting team.run,
    so blocking on :func:`asyncio.run_coroutine_threadsafe` from here would
    deadlock it. The correct pattern for same-loop sync callsites is
    :meth:`asyncio.AbstractEventLoop.create_task`.

    Exceptions inside the task are silently collected — we don't want the
    user's logger to blow up because of instrumentation drift.
    """
    if not asyncio.iscoroutine(coro):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — nothing to schedule on. Run synchronously via
        # asyncio.run as a best-effort fallback (this path is only hit in
        # purely sync test contexts).
        with suppress(Exception):
            asyncio.run(coro)
        return
    try:
        task = loop.create_task(coro)
        # Attach a no-op done callback so the task is awaitable-if-anyone-cares
        # and so unhandled exceptions are swallowed with a warning rather
        # than printed as "Task exception was never retrieved".
        task.add_done_callback(_swallow_task_result)
    except RuntimeError:
        # Loop not accepting tasks (closing) — best effort only.
        with suppress(Exception):
            asyncio.run(coro)


def _swallow_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except Exception as exc:  # pragma: no cover - defensive
        structlog.get_logger().warning("autogen.task_failed", error=str(exc))


def _publish_sync(bus: EventBus, event: RunEvent) -> None:
    """Publish an event to the bus from a sync context.

    Unlike CrewAI's adapter (where publishes originate from ``asyncio.to_thread``
    and must cross the loop boundary), AutoGen's handler runs on the same
    loop as ``team.run``, so we use :meth:`asyncio.AbstractEventLoop.create_task`
    to queue the publish non-blockingly.
    """
    coro = bus.publish(event)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(coro)
        except RuntimeError as exc:
            structlog.get_logger().warning("autogen.publish_failed", error=str(exc))
        return
    try:
        task = loop.create_task(coro)
        task.add_done_callback(_swallow_task_result)
    except RuntimeError as exc:
        structlog.get_logger().warning("autogen.publish_failed", error=str(exc))
