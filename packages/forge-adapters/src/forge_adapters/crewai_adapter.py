"""CrewAI adapter for Forge.

Wraps CrewAI's :class:`Crew` into Forge's uniform :class:`Adapter` interface
with the same instrumentation contract as the LangGraph adapter (Fase γ.1):

* **Agent boundaries** are captured by monkey-patching ``crewai.Agent.execute_task``
  within the adapter's execution scope. Entry and exit emit
  :class:`RunEventKind.AGENT_START` / :class:`RunEventKind.AGENT_END` events,
  tagged with the active ``run_id`` from :func:`forge_core.context.current_run`.
* **Tool calls** are captured by monkey-patching ``crewai.tools.BaseTool._run``.
  Each call becomes a :class:`RunEventKind.TOOL_CALL` / :class:`RunEventKind.TOOL_RESULT`
  pair.
* **LLM cost/token telemetry** leverages the fact that CrewAI 0.5+ uses
  LangChain chat models under the hood. We inject the same
  :class:`forge_adapters.langchain_callback.ForgeLangChainCallback` into each
  ``Agent.llm`` before ``kickoff()``, so the cost aggregation path is literally
  identical to LangGraph's — a single interceptor produces populated
  :class:`RunEventKind.LLM_CALL` events regardless of framework.

Monkey-patches are scoped to the execution via ``try/finally`` so the global
CrewAI classes are restored once ``kickoff()`` returns. This keeps Forge's
instrumentation invisible to any other CrewAI user in the same process.

Because ``crew.kickoff()`` is synchronous and network-heavy, we execute it via
:func:`asyncio.to_thread`. That keeps the event loop responsive and allows the
orchestrator's budget/timeout guards to cancel the run if needed.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib.util
import time
from contextlib import contextmanager
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


class CrewAIAdapter(BaseAdapter):
    """Adapter for CrewAI crews with end-to-end Forge instrumentation."""

    def __init__(
        self,
        interceptor: LLMCallInterceptor | None = None,
        bus: EventBus | None = None,
    ) -> None:
        super().__init__()
        self._crew: Any = None
        # Orchestrator-owned. Injected via set_interceptor / set_bus.
        self._interceptor = interceptor
        self._bus = bus

    @property
    def name(self) -> str:
        return "crewai"

    # ------------------------------------------------------------------
    # Orchestrator wiring (Fase α contract)
    # ------------------------------------------------------------------

    def set_interceptor(self, interceptor: LLMCallInterceptor) -> None:
        """Accept the orchestrator-owned :class:`LLMCallInterceptor`.

        Called by :meth:`MetaOrchestrator._wire_adapter`. Mirrors the
        contract implemented by the LangGraph adapter so the orchestrator
        can treat every adapter identically.
        """
        self._interceptor = interceptor

    def set_bus(self, bus: EventBus) -> None:
        """Accept the orchestrator-owned :class:`EventBus`.

        Used by tool monkey-patching to publish ``TOOL_CALL`` /
        ``TOOL_RESULT`` events to observers without returning them through
        the ``_execute`` list (which is only used for adapter-boundary
        events, by Fase α convention).
        """
        self._bus = bus

    # ------------------------------------------------------------------
    # Detection + loading
    # ------------------------------------------------------------------

    def detect(self, source: str | Path) -> bool:
        try:
            code = Path(source).read_text(encoding="utf-8")
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "crewai" in node.module:
                    return True
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "crewai" in alias.name:
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

        crew = getattr(module, "crew", None)
        if crew is None:
            build_fn = getattr(module, "build_crew", None)
            if build_fn is not None:
                crew = build_fn()

        if crew is None:
            raise RuntimeError(
                f"No CrewAI crew found in {path}. "
                "Expose a 'crew' variable or 'build_crew()' function."
            )

        self._crew = crew
        return self._extract_topology()

    def _extract_topology(self) -> list[AgentCard]:
        agents: list[AgentCard] = []
        try:
            crew_agents = getattr(self._crew, "agents", [])
            for agent in crew_agents:
                role = getattr(agent, "role", "unknown")
                agents.append(
                    AgentCard(
                        name=str(role),
                        role=str(getattr(agent, "goal", "")),
                        description=str(getattr(agent, "backstory", "")),
                        model=_resolve_model_name(agent),
                        metadata={"framework": "crewai"},
                    )
                )
        except Exception as exc:
            logger.warning("crewai.topology_extraction_failed", error=str(exc))
        return agents

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _execute(self, envelope: TaskEnvelope) -> tuple[Any, list[RunEvent]]:
        """Run the crew with full agent/tool/LLM instrumentation.

        The ``events`` returned here only track the outermost adapter boundary.
        Per-agent and per-tool events are published to the bus directly by the
        monkey-patched wrappers — the ``MetaOrchestrator`` aggregates them from
        there, matching the Fase α convention established by the LangGraph
        adapter.
        """
        events: list[RunEvent] = []
        ctx = current_run()
        run_id = ctx.run_id if ctx is not None else None

        events.append(
            RunEvent(
                kind=RunEventKind.AGENT_START,
                run_id=run_id,
                agent_id="crewai_crew",
                data={"input": envelope.input},
            )
        )

        if self._interceptor is None:
            logger.warning("crewai.no_interceptor_running_uninstrumented")

        start = time.perf_counter()
        try:
            with self._instrument_crewai():
                # CrewAI's kickoff is blocking and may hit the network.
                # Offload to a thread so asyncio stays responsive and the
                # orchestrator's guards can cancel if needed.
                result = await asyncio.to_thread(self._invoke_crew, envelope.input)
        except Exception as exc:
            events.append(
                RunEvent(
                    kind=RunEventKind.ERROR,
                    run_id=run_id,
                    agent_id="crewai_crew",
                    error=str(exc),
                )
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000
        events.append(
            RunEvent(
                kind=RunEventKind.AGENT_END,
                run_id=run_id,
                agent_id="crewai_crew",
                latency_ms=elapsed_ms,
                data={"output_type": type(result).__name__},
            )
        )
        return result, events

    def _invoke_crew(self, payload: Any) -> Any:
        """Call ``crew.kickoff`` with the payload.

        Extracted so tests can monkey-patch the boundary cleanly, and so the
        ``asyncio.to_thread`` call has a tight, synchronous target.
        """
        kickoff = self._crew.kickoff
        try:
            return kickoff(inputs=payload)
        except TypeError:
            # Older CrewAI versions (<0.5) don't accept the ``inputs`` kw.
            return kickoff(payload)

    # ------------------------------------------------------------------
    # Instrumentation scope (monkey-patching)
    # ------------------------------------------------------------------

    @contextmanager
    def _instrument_crewai(self) -> Iterator[None]:
        """Install instrumentation on CrewAI classes for the duration of a run.

        Three independent concerns, each handled in a best-effort manner so a
        CrewAI version that renamed or removed one path doesn't break the
        whole adapter:

        1. ``Agent.execute_task`` — monkey-patched to emit AGENT_START/END.
        2. ``BaseTool._run`` — monkey-patched to emit TOOL_CALL/RESULT.
        3. Each ``Agent.llm`` receives a :class:`ForgeLangChainCallback` so
           CrewAI's internal LangChain calls produce populated ``LLM_CALL``
           events via the shared interceptor.

        All patches are reversed in the ``finally`` block so other consumers
        of CrewAI in the same process see pristine classes once the run ends.
        """
        patches: list[tuple[Any, str, Any]] = []
        restore_llm_callbacks: list[tuple[Any, Any]] = []

        try:
            try:
                import crewai
            except ImportError:
                logger.warning("crewai.not_installed_skipping_instrumentation")
                yield
                return

            # 1. Agent.execute_task boundary.
            agent_cls = getattr(crewai, "Agent", None)
            original_execute = getattr(agent_cls, "execute_task", None) if agent_cls else None
            if agent_cls is not None and original_execute is not None:
                patches.append((agent_cls, "execute_task", original_execute))
                agent_cls.execute_task = _build_agent_execute_wrapper(original_execute, self._bus)

            # 2. BaseTool._run instrumentation.
            base_tool = _import_base_tool()
            if base_tool is not None:
                original_tool_run = getattr(base_tool, "_run", None)
                if original_tool_run is not None:
                    patches.append((base_tool, "_run", original_tool_run))
                    base_tool._run = _build_tool_run_wrapper(original_tool_run, self._bus)

            # 3. Inject the LangChain callback into each agent's LLM.
            if self._interceptor is not None:
                restore_llm_callbacks.extend(self._inject_langchain_callbacks())

            yield
        finally:
            # Restore class methods in reverse.
            for owner, attr, original in reversed(patches):
                try:
                    setattr(owner, attr, original)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("crewai.restore_failed", attr=attr, error=str(exc))
            # Restore per-agent llm.callbacks.
            for llm, original in restore_llm_callbacks:
                with contextlib.suppress(Exception):  # pragma: no cover - defensive
                    llm.callbacks = original

    def _inject_langchain_callbacks(self) -> list[tuple[Any, Any]]:
        """Attach ``ForgeLangChainCallback`` to every ``Agent.llm`` in the crew.

        Returns a list of ``(llm, original_callbacks)`` tuples so the caller
        can restore the prior callback list when the run completes. If the
        LangChain package isn't installed (or the attribute model drifts in a
        future CrewAI version), this is a no-op with a warning — we don't want
        a cosmetic instrumentation gap to crash a real user's run.
        """
        restorations: list[tuple[Any, Any]] = []
        if self._interceptor is None:
            return restorations
        try:
            from forge_adapters.langchain_callback import ForgeLangChainCallback
        except ImportError:
            logger.warning("crewai.langchain_callback_unavailable")
            return restorations

        try:
            callback = ForgeLangChainCallback(self._interceptor)
        except ImportError:
            logger.warning("crewai.langchain_not_installed")
            return restorations

        for agent in getattr(self._crew, "agents", []) or []:
            llm = getattr(agent, "llm", None)
            if llm is None:
                continue
            current = getattr(llm, "callbacks", None)
            restorations.append((llm, current))
            # LangChain accepts either a list or a manager; we always write a
            # fresh list so the per-run callback is isolated.
            try:
                llm.callbacks = [*(current or []), callback]
            except Exception as exc:
                logger.warning("crewai.callback_attach_failed", error=str(exc))
        return restorations


Adapter = CrewAIAdapter


# ----------------------------------------------------------------------
# Module-level wrapper builders
# ----------------------------------------------------------------------


def _resolve_model_name(agent: Any) -> str | None:
    """Best-effort extraction of the model name from a CrewAI agent.

    CrewAI 0.1-0.5 stored the llm as a LangChain chat model; 0.5+ also
    accepts plain string model names via ``llm_config``. We try both and
    return ``None`` if we can't figure it out — Forge's cost model degrades
    gracefully with unknown models (logs a warning, costs as $0).
    """
    llm = getattr(agent, "llm", None)
    if llm is None:
        return None
    for attr in ("model", "model_name", "deployment_name"):
        value = getattr(llm, attr, None)
        if value:
            return str(value)
    if isinstance(llm, str):
        return llm
    return None


def _import_base_tool() -> Any | None:
    """Locate CrewAI's ``BaseTool`` class across version layouts.

    CrewAI has shuffled this class: ``crewai.tools.BaseTool`` in 0.1-0.5,
    ``crewai_tools.BaseTool`` in some 0.30+ splits. We try the common paths
    and return ``None`` if none resolve — instrumentation of tool calls is
    optional (agent-level events still fire).
    """
    candidates = (
        ("crewai.tools", "BaseTool"),
        ("crewai_tools", "BaseTool"),
    )
    for module_name, attr in candidates:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        cls = getattr(module, attr, None)
        if cls is not None:
            return cls
    return None


def _build_agent_execute_wrapper(original: Any, bus: EventBus | None) -> Any:
    """Wrap ``Agent.execute_task`` to emit Forge agent-boundary events."""

    def wrapped(self_agent: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = current_run()
        run_id = ctx.run_id if ctx is not None else None
        agent_id = str(getattr(self_agent, "role", None) or "crewai_agent")
        start = time.perf_counter()

        if bus is not None:
            _publish_sync(
                bus,
                RunEvent(
                    kind=RunEventKind.AGENT_START,
                    run_id=run_id,
                    agent_id=agent_id,
                ),
            )
        try:
            result = original(self_agent, *args, **kwargs)
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000.0
            if bus is not None:
                _publish_sync(
                    bus,
                    RunEvent(
                        kind=RunEventKind.ERROR,
                        run_id=run_id,
                        agent_id=agent_id,
                        latency_ms=latency_ms,
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                )
            raise
        latency_ms = (time.perf_counter() - start) * 1000.0
        if bus is not None:
            _publish_sync(
                bus,
                RunEvent(
                    kind=RunEventKind.AGENT_END,
                    run_id=run_id,
                    agent_id=agent_id,
                    latency_ms=latency_ms,
                    data={"output_type": type(result).__name__},
                ),
            )
        return result

    wrapped.__wrapped__ = original  # type: ignore[attr-defined]
    return wrapped


def _build_tool_run_wrapper(original: Any, bus: EventBus | None) -> Any:
    """Wrap ``BaseTool._run`` to emit TOOL_CALL / TOOL_RESULT events."""

    def wrapped(self_tool: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = current_run()
        run_id = ctx.run_id if ctx is not None else None
        tool_name = str(getattr(self_tool, "name", None) or type(self_tool).__name__)
        start = time.perf_counter()

        if bus is not None:
            _publish_sync(
                bus,
                RunEvent(
                    kind=RunEventKind.TOOL_CALL,
                    run_id=run_id,
                    tool_name=tool_name,
                    data={"args": _summarize(args), "kwargs": _summarize(kwargs)},
                ),
            )
        try:
            result = original(self_tool, *args, **kwargs)
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000.0
            if bus is not None:
                _publish_sync(
                    bus,
                    RunEvent(
                        kind=RunEventKind.ERROR,
                        run_id=run_id,
                        tool_name=tool_name,
                        latency_ms=latency_ms,
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                )
            raise
        latency_ms = (time.perf_counter() - start) * 1000.0
        if bus is not None:
            _publish_sync(
                bus,
                RunEvent(
                    kind=RunEventKind.TOOL_RESULT,
                    run_id=run_id,
                    tool_name=tool_name,
                    latency_ms=latency_ms,
                    data={"result_type": type(result).__name__},
                ),
            )
        return result

    wrapped.__wrapped__ = original  # type: ignore[attr-defined]
    return wrapped


def _publish_sync(bus: EventBus, event: RunEvent) -> None:
    """Publish an event from a sync context.

    CrewAI's ``execute_task`` / ``_run`` are synchronous methods that our
    monkey-patches wrap. The shared ``EventBus`` is async-first, so from a
    sync callsite we need to schedule the coroutine on the running loop.

    Resolution order:

    1. If there is a running loop (we're inside ``asyncio.to_thread``), use
       :func:`asyncio.run_coroutine_threadsafe` from the thread to dispatch
       the publish back to the main loop, fire-and-forget.
    2. If there is no loop at all (pure sync test), fall back to creating
       one transiently via :func:`asyncio.run`.
    """
    coro = bus.publish(event)
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
            return
        except RuntimeError:
            pass
    try:
        asyncio.run(coro)
    except RuntimeError as exc:
        logger.warning("crewai.publish_failed", error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("crewai.publish_error", error=str(exc))


def _summarize(value: Any, *, max_len: int = 200) -> str:
    """Produce a short, safe string for events.

    CrewAI tools frequently receive large dicts or JSON blobs as inputs;
    we trim to keep ``RunEvent.data`` payloads small for downstream
    serialization (journal, dashboard).
    """
    s = repr(value)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"
