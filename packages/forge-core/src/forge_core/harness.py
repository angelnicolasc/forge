"""MetaOrchestrator — the central brain of Forge.

The MetaOrchestrator accepts any wrapped agent flow (via an Adapter),
runs it with full instrumentation, tracks costs, and feeds results
into the evolution loop.

Post-L7/L8 fase α, the orchestrator owns three collaborators that every
run passes through:

1.  **EventBus** — the in-process fan-out channel. Every interesting
    moment of a run (LLM call, tool call, agent boundary, run start/end)
    is published here. Subscribers are registered once at construction
    time and receive every future run's events.

2.  **LLMCallInterceptor** — the canonical ``ForgeLLMInterceptor`` that
    adapters call into from their framework-specific callbacks. It
    computes cost, opens/closes OTel spans nested under the active run
    span, and publishes populated ``LLM_CALL`` events to the bus.

3.  **RunContext / run_scope** — establishes the per-run contextvars so
    interceptors called from deep async stacks know which run they're
    inside without the adapter having to plumb ``run_id`` through.

The aggregation of ``RunResult.cost`` now comes from bus events collected
during the run, not from the adapter's returned event list. This is what
makes "$0 always" a thing of the past: any bus event is a fact that an
LLM call happened and that it cost money, regardless of where inside the
graph it was emitted.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from forge_core.circuit_breaker import CircuitBreaker
from forge_core.config import ForgeConfig
from forge_core.context import run_scope
from forge_core.events import ALL_EVENTS, EventBus
from forge_core.evolution.snapshot import TopologySnapshot
from forge_core.guards import BudgetExceeded, TimeoutExceeded, run_with_guards
from forge_core.registry import get_registry
from forge_core.types import (
    AgentCard,
    CostSummary,
    RunConfig,
    RunContext,
    RunEvent,
    RunEventKind,
    RunResult,
    RunStatus,
    TaskEnvelope,
    TopologyState,
)

logger = structlog.get_logger()

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from forge_core.evolution.loop import EvolutionLoop
    from forge_core.protocols import Adapter, LLMCallInterceptor


class MetaOrchestrator:
    """The central orchestration engine for Forge.

    Responsibilities:
    - Accept any Adapter-wrapped flow
    - Execute with full tracing and cost tracking via EventBus + Interceptor
    - Enforce budgets and timeouts
    - Feed results to evolution loop (when enabled)
    - Expose topology for visualization
    """

    def __init__(
        self,
        config: ForgeConfig | None = None,
        adapter: Adapter | None = None,
        *,
        bus: EventBus | None = None,
        interceptor: LLMCallInterceptor | None = None,
    ) -> None:
        self._config = config or ForgeConfig()
        self._bus = bus or EventBus()
        # Lazily construct the default interceptor so `forge-core` doesn't
        # hard-depend on `forge-observe` at import time. Most users get the
        # default; advanced users inject their own.
        if interceptor is None:
            interceptor = _default_interceptor(self._bus)
        self._interceptor = interceptor
        self._adapter = adapter
        self._run_history: list[RunResult] = []

        # Per-run event collector; attached to the bus for the lifetime of
        # each run and replaced on the next one. Using a subscription lets
        # us collect events emitted from *anywhere* (interceptor, adapter,
        # hooks) without coordinating return values.
        self._current_run_events: list[RunEvent] | None = None
        self._event_collector_handle = self._bus.subscribe(ALL_EVENTS, self._collect_event)

        # Topology versioning for the evolution loop. Every snapshot the
        # orchestrator captures gets a monotonic version assigned here. A
        # swap increments it; a restore does *not* rewind the counter
        # (the journal's job is to link versions to snapshots, not
        # pretend time went backwards).
        self._topology_version: int = 0
        self._prompts_map: dict[str, str] = {}

        # β.4 — auto-trigger wiring. The evolution loop is attached via
        # ``attach_evolution_loop``; until then it's None and auto-trigger
        # is an inert code path. ``_last_evolution_at`` drives the interval
        # debouncer. ``_pending_evolution_tasks`` tracks background
        # ``step()`` coroutines so tests (and orderly shutdown) can await
        # them deterministically without leaking tasks.
        self._evolution_loop: EvolutionLoop | None = None
        self._last_evolution_at: datetime | None = None
        self._pending_evolution_tasks: set[asyncio.Task[Any]] = set()

        # ζ.5b — evolution auto-suspend breaker. If the evolution loop
        # tips over (consecutive rollbacks, unhandled exception in a
        # mutator, etc.) we stop auto-triggering until the operator
        # explicitly calls ``resume_evolution``. Three failures in a row
        # is enough signal — the breaker is not shared with any other
        # site so its counters only reflect evolution health.
        self._evolution_breaker: CircuitBreaker = CircuitBreaker(
            "evolution",
            failure_threshold=3,
            recovery_timeout_seconds=300.0,
        )

        # δ.1 — memory context injection. The injector is attached via
        # :meth:`attach_memory`; until then the pre-run hook is a no-op.
        # Gated by the per-run ``config.memory.enable_context_injection``
        # plus the ``FORGE_ENABLE_MEMORY_INJECTION`` env flag.
        self._memory: Any = None
        self._memory_injector: Any = None

        # Wire the adapter to the same interceptor if it supports it.
        if adapter is not None:
            self._wire_adapter(adapter)

    # ------------------------------------------------------------------
    # Properties and attachment helpers
    # ------------------------------------------------------------------

    @property
    def config(self) -> ForgeConfig:
        return self._config

    @property
    def adapter(self) -> Adapter | None:
        return self._adapter

    @property
    def bus(self) -> EventBus:
        """The event bus that external observers (tracer, dashboard) use."""
        return self._bus

    @property
    def interceptor(self) -> LLMCallInterceptor:
        """The LLM interceptor shared with every adapter this orchestrator drives."""
        return self._interceptor

    def set_adapter(self, adapter: Adapter) -> None:
        """Set or swap the active adapter and wire it to the interceptor."""
        self._adapter = adapter
        self._wire_adapter(adapter)
        logger.info("orchestrator.adapter_set", adapter=adapter.name)

    def _wire_adapter(self, adapter: Adapter) -> None:
        """Propagate the interceptor and bus to adapters that accept them.

        Adapters that predate fase α (and so lack ``set_interceptor``)
        simply aren't instrumented — their LLM calls fall through the
        legacy uninstrumented path. Every built-in adapter overrides this
        to inject the interceptor.

        Fase γ.1 adds the sibling ``set_bus`` hook: some adapters (CrewAI,
        AutoGen) emit per-agent / per-tool events from sync callbacks
        deep inside framework internals and need the bus directly rather
        than returning events from ``_execute``.
        """
        setter = getattr(adapter, "set_interceptor", None)
        if callable(setter):
            setter(self._interceptor)
        bus_setter = getattr(adapter, "set_bus", None)
        if callable(bus_setter):
            bus_setter(self._bus)

    async def load(self, source: str) -> Adapter:
        """Auto-detect and load an adapter for the given source."""
        registry = get_registry()
        adapter = registry.detect_adapter(source)
        await adapter.load(source)
        self._adapter = adapter
        self._wire_adapter(adapter)
        logger.info("orchestrator.loaded", adapter=adapter.name, source=source)
        return adapter

    # ------------------------------------------------------------------
    # Evolution auto-trigger wiring  (fase β.4)
    # ------------------------------------------------------------------

    def attach_evolution_loop(self, loop: EvolutionLoop) -> None:
        """Bind an :class:`EvolutionLoop` to this orchestrator.

        After attachment, successful runs consult
        :meth:`_should_trigger_evolution` and — when all guards pass — fire
        ``loop.step()`` as a background task. The trigger is **only** armed
        when the loop is attached AND ``config.evolution.auto_trigger_enabled``
        is True (or ``FORGE_ENABLE_EVOLUTION_AUTO=1`` is set). This keeps the
        default posture conservative: the loop is a zero-cost observer until
        someone flips the flag.
        """
        self._evolution_loop = loop
        logger.info(
            "orchestrator.evolution_attached",
            auto_trigger=self._config.evolution.auto_trigger_enabled,
            mode=self._config.evolution.mode,
        )

    @property
    def evolution_loop(self) -> EvolutionLoop | None:
        """Currently attached evolution loop, if any."""
        return self._evolution_loop

    @property
    def evolution_breaker(self) -> CircuitBreaker:
        """Expose the evolution breaker for inspection (ζ.5b).

        Operators / ``forge evolve status`` read ``state`` /
        ``failure_count`` here. The breaker is always present — it simply
        stays CLOSED when auto-evolution is healthy.
        """
        return self._evolution_breaker

    def resume_evolution(self) -> None:
        """Reset the evolution breaker so auto-trigger re-arms (ζ.5b).

        Call this after a human has investigated why step() was failing
        and either fixed the mutator / env or confirmed the failures were
        transient.
        """
        self._evolution_breaker.reset()
        logger.info("orchestrator.evolution_resumed")

    # ------------------------------------------------------------------
    # Memory injection wiring  (δ.1)
    # ------------------------------------------------------------------

    def attach_memory(self, memory: Any) -> None:
        """Bind a memory backend (typically :class:`HybridMemory`) for pre-run injection.

        The orchestrator constructs a :class:`MemoryContextInjector`
        lazily to avoid forcing a hard dependency on ``forge-memory`` at
        import time. Callers that need finer control may pass a
        pre-built injector via :attr:`memory_injector` directly.
        """
        self._memory = memory
        try:
            from forge_memory.injection import MemoryContextInjector
        except ImportError:  # pragma: no cover — tested via monkeypatch in CI
            logger.warning("orchestrator.attach_memory.forge_memory_missing")
            return
        self._memory_injector = MemoryContextInjector(
            memory=memory,
            config=self._config.memory if hasattr(self._config, "memory") else None,  # type: ignore[arg-type]
        )
        logger.info("orchestrator.memory_attached")

    @property
    def memory(self) -> Any:
        """Currently attached memory backend, if any."""
        return self._memory

    @property
    def memory_injector(self) -> Any:
        """Currently attached memory injector, if any. Writable for tests."""
        return self._memory_injector

    @memory_injector.setter
    def memory_injector(self, injector: Any) -> None:
        self._memory_injector = injector

    @property
    def pending_evolution_tasks(self) -> set[asyncio.Task[Any]]:
        """Background evolution tasks still in flight.

        Tests await these to make the auto-trigger deterministic:
        ``await asyncio.gather(*orchestrator.pending_evolution_tasks)``.
        """
        return self._pending_evolution_tasks

    def _should_trigger_evolution(self, envelope: TaskEnvelope) -> bool:
        """Return True iff a background ``evolution.step()`` should fire.

        Gates, in order:

        1. **Loop attached** — ``attach_evolution_loop`` was called.
        2. **Flag on** — ``config.evolution.auto_trigger_enabled`` or the
           ``FORGE_ENABLE_EVOLUTION_AUTO`` env var.
        3. **Not a reentrant eval** — envelopes carrying
           ``metadata["forge_internal_eval"] = True`` (emitted by
           :class:`EvolutionLoop._eval_run`) are skipped to prevent the
           evolution loop from re-triggering itself.
        4. **Enough history** — at least ``min_history_for_evolution``
           runs accumulated.
        5. **Modulo cadence** — history length divisible by
           ``trigger_every_n_runs`` (cheap quasi-random thinning).
        6. **Interval debouncer** — ``min_interval_seconds`` elapsed since
           the last auto-trigger. Prevents stampedes when runs cluster in
           time (bulk eval, replay).

        The function mutates ``_last_evolution_at`` when it returns True
        — this "claim" semantics is what makes the debouncer correct under
        concurrent ``run()`` calls.
        """
        cfg = self._config.evolution
        if self._evolution_loop is None:
            return False
        if not cfg.auto_trigger_enabled:
            return False
        if envelope.metadata.get("forge_internal_eval") is True:
            return False
        n = len(self._run_history)
        if n < cfg.min_history_for_evolution:
            return False
        if cfg.trigger_every_n_runs > 0 and n % cfg.trigger_every_n_runs != 0:
            return False
        now = datetime.now(UTC)
        if self._last_evolution_at is not None:
            elapsed = (now - self._last_evolution_at).total_seconds()
            if elapsed < cfg.min_interval_seconds:
                return False
        self._last_evolution_at = now
        return True

    def _schedule_evolution(self) -> asyncio.Task[Any] | None:
        """Fire-and-forget the evolution loop's next step.

        The task is registered in ``_pending_evolution_tasks`` so tests
        (and graceful shutdown) can await it. A ``done_callback`` removes
        the reference after completion and swallows exceptions, logging
        them — a crashing mutator must never escalate into the user's
        run() call path.
        """
        assert self._evolution_loop is not None  # guarded by _should_trigger_evolution

        # ζ.5b — skip the step if the breaker is OPEN. The breaker trips
        # after three consecutive failed ``step()`` calls and requires an
        # explicit ``resume_evolution`` to re-arm.
        if self._evolution_breaker.state.value == "open":
            logger.warning(
                "orchestrator.evolution_suspended",
                reason="breaker_open",
                failures=self._evolution_breaker.failure_count,
            )
            return None

        try:
            task = asyncio.create_task(
                self._evolution_loop.step(),
                name=f"forge-evolution-step-{uuid4().hex[:8]}",
            )
        except RuntimeError:
            # No running loop (e.g., called from a sync context). Skip.
            logger.warning("orchestrator.evolution_no_event_loop")
            return None

        self._pending_evolution_tasks.add(task)

        def _on_done(t: asyncio.Task[Any]) -> None:
            self._pending_evolution_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                # ζ.5b — count the failure and, if the breaker trips,
                # surface a single clear log so the operator sees WHY
                # auto-evolution stopped without grepping error traces.
                self._evolution_breaker.record_failure()
                logger.error(
                    "orchestrator.evolution_step_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    breaker_state=self._evolution_breaker.state.value,
                    failure_count=self._evolution_breaker.failure_count,
                )
                if self._evolution_breaker.state.value == "open":
                    logger.error(
                        "orchestrator.evolution_suspended",
                        reason="consecutive_failures",
                        threshold=3,
                    )
            else:
                self._evolution_breaker.record_success()

        task.add_done_callback(_on_done)
        logger.info(
            "orchestrator.evolution_scheduled",
            task_name=task.get_name(),
            pending=len(self._pending_evolution_tasks),
        )
        return task

    # ------------------------------------------------------------------
    # Event collection (internal)
    # ------------------------------------------------------------------

    async def _collect_event(self, event: RunEvent) -> None:
        """Subscriber that accumulates every event from the bus into the current run.

        Deliberately tolerates being called outside a run — events
        emitted in between runs are simply dropped on the floor.
        """
        if self._current_run_events is not None:
            self._current_run_events.append(event)

    # ------------------------------------------------------------------
    # Core run path
    # ------------------------------------------------------------------

    async def run(self, envelope: TaskEnvelope | None = None, **kwargs: object) -> RunResult:
        """Execute a run through the active adapter.

        The run is wrapped in a ``run_scope`` so interceptors called deep
        inside the adapter (via LangChain callbacks, CrewAI monkey-patches,
        etc.) can read :func:`forge_core.context.current_run` to discover
        which run they're inside.
        """
        if self._adapter is None:
            raise RuntimeError("No adapter loaded. Call load() or set_adapter() first.")

        if envelope is None:
            envelope = TaskEnvelope(
                input=dict(kwargs),
                config=RunConfig(
                    max_steps=self._config.default_max_steps,
                    timeout_seconds=self._config.default_timeout_seconds,
                    cost_ceiling=self._config.default_cost_ceiling,
                ),
            )

        run_id = uuid4().hex[:16]
        ctx = RunContext(
            run_id=run_id,
            task_id=envelope.task_id,
            span_id=uuid4().hex[:16],
            envelope_task_id=envelope.task_id,
        )

        logger.info(
            "orchestrator.run.start",
            run_id=run_id,
            task_id=envelope.task_id,
            adapter=self._adapter.name,
        )

        # Reset the per-run event buffer *before* entering the scope so
        # the very first event (RUN_STARTED) lands in the right bucket.
        self._current_run_events = []

        start_time = time.perf_counter()
        start_dt = datetime.now(UTC)

        # Publish RUN_STARTED so subscribers (dashboard SSE, metrics) see
        # the run boundary even if the adapter never emits anything itself.
        await self._bus.publish(
            RunEvent(
                kind=RunEventKind.RUN_STARTED,
                run_id=run_id,
                span_id=ctx.span_id,
                data={"task_id": envelope.task_id, "adapter": self._adapter.name},
            )
        )

        # δ.1 — enrich the envelope with relevant memories *before* the
        # adapter sees it. The injector itself honours both the per-run
        # config flag and the global env kill-switch; we only skip the
        # call if no injector was attached at all.
        if self._memory_injector is not None:
            try:
                envelope = await self._memory_injector.inject(envelope)
            except Exception as exc:  # defensive — never fail a run on memory hiccup
                logger.warning("orchestrator.memory_injection_failed", error=str(exc))

        result: RunResult
        try:
            with run_scope(ctx):
                try:
                    # Fase γ.4: wrap the adapter run in cost + timeout guards.
                    # We pass a factory (not a coroutine) so ``run_with_guards``
                    # can control task creation/cancellation without the
                    # caller having to hand off an active coroutine.
                    assert self._adapter is not None  # narrow for type checker
                    adapter = self._adapter

                    def _run_factory() -> Any:
                        return adapter.run(envelope)

                    result = await run_with_guards(
                        _run_factory,
                        bus=self._bus,
                        cost_ceiling=envelope.config.cost_ceiling,
                        timeout_seconds=envelope.config.timeout_seconds,
                    )
                except (BudgetExceeded, TimeoutExceeded) as exc:
                    elapsed = (time.perf_counter() - start_time) * 1000
                    result = RunResult(
                        task_id=envelope.task_id,
                        run_id=run_id,
                        status=RunStatus.FAILED,
                        duration_ms=elapsed,
                        errors=[str(exc)],
                        started_at=start_dt,
                        completed_at=datetime.now(UTC),
                        topology=self._get_topology(),
                    )
                    logger.warning(
                        "orchestrator.run.guard_tripped",
                        run_id=run_id,
                        task_id=envelope.task_id,
                        kind=type(exc).__name__,
                        error=str(exc),
                    )
                except Exception as exc:
                    elapsed = (time.perf_counter() - start_time) * 1000
                    result = RunResult(
                        task_id=envelope.task_id,
                        run_id=run_id,
                        status=RunStatus.FAILED,
                        duration_ms=elapsed,
                        errors=[str(exc)],
                        started_at=start_dt,
                        completed_at=datetime.now(UTC),
                        topology=self._get_topology(),
                    )
                    logger.error(
                        "orchestrator.run.failed",
                        run_id=run_id,
                        task_id=envelope.task_id,
                        error=str(exc),
                    )
        finally:
            # Close the run regardless of outcome.
            elapsed = (time.perf_counter() - start_time) * 1000
            # Fill in bookkeeping fields if the happy path built a result.
            if "result" in locals() and result is not None:
                result.duration_ms = elapsed
                result.completed_at = datetime.now(UTC)
                result.run_id = run_id
                result.topology = self._get_topology()

                # Merge: adapter-reported events + bus-collected events.
                # Deduplicate by event.id — LangChain callbacks publish to
                # the bus AND some adapters also put the same event in
                # their return list. Bus wins if we see a collision.
                bus_events = list(self._current_run_events or [])
                bus_event_ids = {e.id for e in bus_events}
                adapter_events = [e for e in result.events if e.id not in bus_event_ids]
                result.events = bus_events + adapter_events

                # Aggregate from the merged events.
                result.cost = self._aggregate_cost(result.events)

            # Always publish RUN_COMPLETED.
            await self._bus.publish(
                RunEvent(
                    kind=RunEventKind.RUN_COMPLETED,
                    run_id=run_id,
                    span_id=ctx.span_id,
                    latency_ms=elapsed,
                    data={
                        "status": getattr(result, "status", RunStatus.FAILED).value
                        if "result" in locals()
                        else RunStatus.FAILED.value,
                        "total_cost": str(
                            getattr(getattr(result, "cost", None), "total_cost", Decimal("0"))
                            if "result" in locals()
                            else Decimal("0")
                        ),
                    },
                )
            )

            # Drop the buffer reference so later off-run events aren't
            # appended to this run's history.
            self._current_run_events = None

        # Cost ceiling soft-warn (hard enforcement lives in γ.4 CostBudgetGuard).
        if result.cost.total_cost > envelope.config.cost_ceiling:
            logger.warning(
                "orchestrator.cost_ceiling_exceeded",
                run_id=run_id,
                cost=str(result.cost.total_cost),
                ceiling=str(envelope.config.cost_ceiling),
            )

        self._run_history.append(result)

        logger.info(
            "orchestrator.run.complete",
            run_id=run_id,
            task_id=envelope.task_id,
            status=result.status,
            duration_ms=f"{result.duration_ms:.1f}",
            cost=str(result.cost.total_cost),
            events=len(result.events),
        )

        # β.4 — Auto-trigger evolution.step() as a background task if
        # every gate passes (loop attached, flag on, non-reentrant,
        # enough history, modulo cadence, interval elapsed). The trigger
        # is deliberately AFTER appending to run_history so the loop's
        # _hypothesize() sees the run that just finished.
        if self._should_trigger_evolution(envelope):
            self._schedule_evolution()

        return result

    async def stream(self, envelope: TaskEnvelope) -> AsyncIterator[RunEvent]:
        """Execute a run and yield events as they happen."""
        if self._adapter is None:
            raise RuntimeError("No adapter loaded. Call load() or set_adapter() first.")

        async for event in self._adapter.stream(envelope):
            yield event

    def topology(self) -> list[AgentCard]:
        """Return the current agent topology."""
        return self._get_topology()

    @property
    def run_history(self) -> list[RunResult]:
        """Access the run history (for evolution loop consumption)."""
        return self._run_history

    def _get_topology(self) -> list[AgentCard]:
        """Get topology from active adapter, or empty list."""
        if self._adapter is None:
            return []
        try:
            return self._adapter.topology()
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Evolution: snapshot / swap / restore  (fase β.1)
    # ------------------------------------------------------------------

    @property
    def topology_version(self) -> int:
        """Monotonic version of the current topology.

        Incremented every time :meth:`swap_topology` applies a new set
        of agents. Rollbacks do *not* decrement it — the evolution
        journal tracks which version was in effect when.
        """
        return self._topology_version

    def current_state(self) -> TopologyState:
        """Return a :class:`TopologyState` snapshot of the live topology.

        Convenience for mutators that take ``TopologyState``; the
        returned object is safe to mutate without corrupting the live
        topology (deep-copied via :meth:`TopologySnapshot.to_state`).
        """
        snap = self.capture_snapshot()
        return snap.to_state()

    def capture_snapshot(self) -> TopologySnapshot:
        """Capture an immutable snapshot of the current topology.

        Called by the evolution loop immediately before
        :meth:`swap_topology`, and stored in the journal so a failed
        mutation can be rolled back by replaying the snapshot.
        """
        return TopologySnapshot.capture(
            agents=self._get_topology(),
            config=self._config_to_run_config(),
            version=self._topology_version,
            prompts_map=self._prompts_map,
        )

    def swap_topology(
        self,
        new_topology: list[AgentCard] | TopologyState,
        *,
        prompts_map: dict[str, str] | None = None,
    ) -> TopologySnapshot:
        """Atomically replace the active topology with ``new_topology``.

        Accepts either a bare ``list[AgentCard]`` (for mutators that
        only touch the agent graph) or a full :class:`TopologyState`
        (for mutators that also tune :class:`RunConfig` or the prompts
        map — ``ParameterTuneMutator``, ``PromptRewriteMutator``).

        Returns the *new* snapshot (post-swap) so callers can compare
        against the pre-swap snapshot captured moments before.

        The swap updates the adapter's internal ``_agents`` list via a
        best-effort protocol: if the adapter defines ``set_topology``,
        we call it; otherwise we monkey-patch ``_agents`` directly.
        This mirrors how built-in adapters already expose their agent
        list (see ``BaseAdapter._agents``).
        """
        if isinstance(new_topology, TopologyState):
            agents = list(new_topology.agents)
            if prompts_map is None:
                prompts_map = dict(new_topology.prompts_map)
            # If the state carries a config, apply it. We only override
            # known fields on the orchestrator's ForgeConfig to avoid
            # clobbering operator-level settings that shouldn't be
            # evolution-controlled.
            self._apply_state_config(new_topology.config)
        else:
            agents = list(new_topology)

        self._install_agents(agents)
        if prompts_map is not None:
            self._prompts_map = dict(prompts_map)

        self._topology_version += 1
        logger.info(
            "orchestrator.topology_swapped",
            version=self._topology_version,
            agents=len(agents),
        )
        return self.capture_snapshot()

    def restore_snapshot(self, snapshot: TopologySnapshot) -> TopologySnapshot:
        """Restore the orchestrator's topology from a captured snapshot.

        This is the atomic rollback primitive for the evolution loop.
        After this call, ``topology()`` returns the exact agents
        (deep-copied from the snapshot), ``config`` reflects the
        snapshot's config, and ``_prompts_map`` is reset.

        The topology version advances rather than rewinds — restore is
        a *forward* operation that installs an older state as the new
        current state, which is the only behavior that keeps the
        journal's version sequence monotonic and auditable.
        """
        agents = snapshot.agents_list()
        self._install_agents(agents)
        self._apply_state_config(snapshot.config)
        self._prompts_map = dict(snapshot.prompts_map)
        self._topology_version += 1
        logger.info(
            "orchestrator.snapshot_restored",
            restored_from_version=snapshot.version,
            new_version=self._topology_version,
            agents=len(agents),
        )
        return self.capture_snapshot()

    # ------------------------------------------------------------------
    # Evolution helpers (internal)
    # ------------------------------------------------------------------

    def _install_agents(self, agents: list[AgentCard]) -> None:
        """Push a new agent list into the active adapter.

        Uses ``set_topology`` when the adapter provides it; otherwise
        falls back to mutating the ``_agents`` attribute directly (the
        pattern used by :class:`BaseAdapter`). If no adapter is loaded
        we raise — swapping topology with no adapter is a programming
        error, not a recoverable state.
        """
        if self._adapter is None:
            raise RuntimeError(
                "Cannot swap topology: no adapter loaded. Call load() or set_adapter() first."
            )
        setter = getattr(self._adapter, "set_topology", None)
        if callable(setter):
            setter(agents)
            return
        # Fallback: BaseAdapter exposes `_agents` as its topology store.
        if hasattr(self._adapter, "_agents"):
            self._adapter._agents = list(agents)
            return
        raise RuntimeError(
            f"Adapter '{self._adapter.name}' does not support topology swap: "
            "neither set_topology() nor _agents attribute present."
        )

    def _apply_state_config(self, run_config: RunConfig) -> None:
        """Project a mutator-produced :class:`RunConfig` onto the ForgeConfig.

        Mutators operate on a :class:`RunConfig` (per-run knobs); the
        orchestrator carries a :class:`ForgeConfig` (process-level
        defaults). We project the relevant fields — ``max_steps``,
        ``timeout_seconds``, ``cost_ceiling`` — so the next run picks
        up the tuned values automatically.
        """
        self._config.default_max_steps = run_config.max_steps
        self._config.default_timeout_seconds = run_config.timeout_seconds
        self._config.default_cost_ceiling = run_config.cost_ceiling

    def _config_to_run_config(self) -> RunConfig:
        """Render the orchestrator's :class:`ForgeConfig` as a :class:`RunConfig`.

        Used by :meth:`capture_snapshot` so snapshots carry the
        per-run-facing view of configuration. The reverse projection is
        :meth:`_apply_state_config`.
        """
        return RunConfig(
            max_steps=self._config.default_max_steps,
            timeout_seconds=self._config.default_timeout_seconds,
            cost_ceiling=self._config.default_cost_ceiling,
        )

    @staticmethod
    def _aggregate_cost(events: list[RunEvent]) -> CostSummary:
        """Aggregate cost metrics from run events.

        Counts an ``LLM_CALL`` when the event carries token data — that's
        what makes the aggregate insensitive to the redundant legacy
        ``LLM_RESPONSE`` events some older code paths still emit.
        """
        summary = CostSummary()

        seen_spans: set[str] = set()
        for event in events:
            if event.kind == RunEventKind.LLM_CALL:
                # Guard against double-counting if both interceptor and
                # adapter publish the same call (shouldn't happen, but the
                # span_id dedup makes the aggregation safe either way).
                if event.span_id and event.span_id in seen_spans:
                    continue
                if event.span_id:
                    seen_spans.add(event.span_id)
                summary.total_input_tokens += event.input_tokens
                summary.total_output_tokens += event.output_tokens
                summary.total_cost += event.cost
                summary.llm_calls += 1

                if event.model:
                    summary.cost_by_model.setdefault(event.model, Decimal("0"))
                    summary.cost_by_model[event.model] += event.cost

                if event.agent_id:
                    summary.cost_by_agent.setdefault(event.agent_id, Decimal("0"))
                    summary.cost_by_agent[event.agent_id] += event.cost

            elif event.kind in (RunEventKind.TOOL_CALL, RunEventKind.TOOL_RESULT):
                summary.tool_calls += 1

        return summary


def _default_interceptor(bus: EventBus) -> LLMCallInterceptor:
    """Construct the default ``ForgeLLMInterceptor``, imported lazily.

    Kept out of the module-level imports because ``forge-core`` must be
    installable without ``forge-observe`` (e.g., minimal deployments that
    ship their own observability stack). The import is cheap once amortized
    across a process lifetime.
    """
    try:
        from forge_observe.interceptor import ForgeLLMInterceptor
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "MetaOrchestrator requires forge-observe for its default LLM "
            "interceptor. Install with: pip install forge-os"
        ) from exc
    return ForgeLLMInterceptor(bus)
