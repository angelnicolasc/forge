"""LangGraph adapter for Forge.

Wraps LangGraph's CompiledGraph into Forge's uniform Adapter interface.
Intercepts invoke()/stream() calls to capture events, costs, and topology.

Cost/token instrumentation is achieved by installing a
:class:`~forge_adapters.langchain_callback.ForgeLangChainCallback` into the
graph's runtime ``config`` on every invocation. That callback routes LLM
lifecycle events through an :class:`~forge_core.protocols.LLMCallInterceptor`
— the canonical one lives in ``forge_observe.interceptor`` and is passed
in by the ``MetaOrchestrator``. The upshot: after L7/L8 fase α, a LangGraph
flow wrapped by Forge produces populated ``LLM_CALL`` events with real
token counts, real costs, and OpenTelemetry spans nested under the active
run span — regardless of whether the user calls ``ainvoke`` or ``astream``.
"""

from __future__ import annotations

import ast
import importlib.util
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

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

    from forge_core.protocols import LLMCallInterceptor

logger = structlog.get_logger()


def _has_langgraph_import(source: str | Path) -> bool:
    """Check if a Python file imports langgraph."""
    try:
        code = Path(source).read_text(encoding="utf-8")
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "langgraph" in alias.name:
                        return True
            elif isinstance(node, ast.ImportFrom) and node.module and "langgraph" in node.module:
                return True
    except Exception:
        return False
    return False


class LangGraphAdapter(BaseAdapter):
    """Adapter for LangGraph compiled graphs."""

    def __init__(self, interceptor: LLMCallInterceptor | None = None) -> None:
        super().__init__()
        self._graph: Any = None
        self._module: Any = None
        # The interceptor is optional at construction time so that the
        # adapter remains usable in tests and from ``registry.detect_adapter``
        # (which creates instances without knowing about the orchestrator).
        # The orchestrator injects it via ``set_interceptor`` before ``run``.
        self._interceptor = interceptor

    @property
    def name(self) -> str:
        return "langgraph"

    def set_interceptor(self, interceptor: LLMCallInterceptor) -> None:
        """Attach the LLM interceptor the orchestrator owns.

        Called by ``MetaOrchestrator.load`` / ``set_adapter`` so every
        LangGraph run inherits centralized cost/token tracking without the
        adapter ever constructing its own interceptor (that would fragment
        telemetry across adapters).
        """
        self._interceptor = interceptor

    def detect(self, source: str | Path) -> bool:
        return _has_langgraph_import(source)

    async def _load_impl(self, source: str | Path) -> list[AgentCard]:
        """Load a LangGraph graph from source file.

        Expects the module to expose a `graph` variable (CompiledGraph)
        or a `build_graph()` function that returns one.
        """
        path = Path(source).resolve()

        spec = importlib.util.spec_from_file_location("_forge_user_flow", str(path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load module from {path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module

        # Find the graph object
        graph = getattr(module, "graph", None)
        if graph is None:
            build_fn = getattr(module, "build_graph", None)
            if build_fn is not None:
                graph = build_fn()
            else:
                # Try to find any CompiledGraph in module namespace
                for attr_name in dir(module):
                    obj = getattr(module, attr_name)
                    if hasattr(obj, "invoke") and hasattr(obj, "get_graph"):
                        graph = obj
                        break

        if graph is None:
            raise RuntimeError(
                f"No LangGraph graph found in {path}. "
                "Expose a 'graph' variable or 'build_graph()' function."
            )

        self._graph = graph

        # Extract topology from the graph structure
        agents = self._extract_topology()
        return agents

    def _extract_topology(self) -> list[AgentCard]:
        """Extract agent topology from a LangGraph compiled graph."""
        agents: list[AgentCard] = []

        try:
            graph_data = self._graph.get_graph()
            nodes = getattr(graph_data, "nodes", {})

            for node_id, node_data in nodes.items():
                if node_id in ("__start__", "__end__"):
                    continue

                name = getattr(node_data, "name", node_id)
                agents.append(
                    AgentCard(
                        id=str(node_id),
                        name=str(name),
                        role=f"LangGraph node: {name}",
                        metadata={"framework": "langgraph", "node_type": "graph_node"},
                    )
                )
        except Exception as exc:
            logger.warning("langgraph.topology_extraction_failed", error=str(exc))
            agents.append(
                AgentCard(
                    name="langgraph_flow",
                    role="LangGraph compiled graph (topology extraction failed)",
                )
            )

        return agents

    async def _execute(self, envelope: TaskEnvelope) -> tuple[Any, list[RunEvent]]:
        """Execute the LangGraph graph with full LLM instrumentation.

        We install a ``ForgeLangChainCallback`` on every invocation so token
        usage for every ``ChatAnthropic`` / ``ChatOpenAI`` / etc. call
        inside the graph flows through Forge's interceptor → bus. This is
        the core L7/L8 α.4 fix.

        The callback publishes ``LLM_CALL`` events directly to the bus, so
        this method's returned ``events`` list only needs to track adapter-
        level boundaries (``AGENT_START`` / ``AGENT_END``). Cost/token data
        is aggregated by the ``MetaOrchestrator`` from the bus, not from
        this return value.
        """
        events: list[RunEvent] = []
        ctx = current_run()
        run_id = ctx.run_id if ctx is not None else None

        # Start event
        events.append(
            RunEvent(
                kind=RunEventKind.AGENT_START,
                run_id=run_id,
                agent_id="langgraph_orchestrator",
                data={"input": envelope.input},
            )
        )

        # Build the callback + metadata config that LangChain consumes.
        # Missing interceptor is a soft-fail: we log once and proceed with
        # the old (uninstrumented) behavior so `load` still works in tests
        # that construct a bare LangGraphAdapter().
        config: dict[str, Any] | None = None
        if self._interceptor is not None:
            try:
                from forge_adapters.langchain_callback import ForgeLangChainCallback

                callback = ForgeLangChainCallback(self._interceptor)
                config = {
                    "callbacks": [callback],
                    "metadata": {
                        "forge_run_id": run_id or "",
                        "forge_task_id": envelope.task_id,
                    },
                }
            except ImportError:
                # LangChain missing — LangGraph should've required it, but
                # if for some reason it isn't available, degrade instead of
                # crashing the user's run.
                logger.warning("langgraph.callback_unavailable_langchain_missing")
        else:
            logger.warning("langgraph.no_interceptor_running_uninstrumented")

        start = time.perf_counter()

        # Try async invoke first, fall back to sync
        try:
            if hasattr(self._graph, "ainvoke"):
                if config is not None:
                    output = await self._graph.ainvoke(envelope.input, config=config)
                else:
                    output = await self._graph.ainvoke(envelope.input)
            else:
                if config is not None:
                    output = self._graph.invoke(envelope.input, config=config)
                else:
                    output = self._graph.invoke(envelope.input)
        except Exception as exc:
            events.append(
                RunEvent(
                    kind=RunEventKind.ERROR,
                    run_id=run_id,
                    agent_id="langgraph_orchestrator",
                    error=str(exc),
                )
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000

        # End event
        events.append(
            RunEvent(
                kind=RunEventKind.AGENT_END,
                run_id=run_id,
                agent_id="langgraph_orchestrator",
                latency_ms=elapsed_ms,
                data={"output_type": type(output).__name__},
            )
        )

        return output, events

    async def stream(self, envelope: TaskEnvelope) -> AsyncIterator[RunEvent]:
        """Stream events from LangGraph execution."""
        yield RunEvent(
            kind=RunEventKind.AGENT_START,
            agent_id="langgraph_orchestrator",
            data={"input": envelope.input},
        )

        try:
            if hasattr(self._graph, "astream_events"):
                async for event in self._graph.astream_events(envelope.input, version="v2"):
                    yield self._convert_langgraph_event(event)
            elif hasattr(self._graph, "astream"):
                async for chunk in self._graph.astream(envelope.input):
                    yield RunEvent(
                        kind=RunEventKind.AGENT_END,
                        agent_id="langgraph_orchestrator",
                        data={"chunk": str(chunk)[:500]},
                    )
            else:
                # Fallback: run synchronously
                output = self._graph.invoke(envelope.input)
                yield RunEvent(
                    kind=RunEventKind.AGENT_END,
                    agent_id="langgraph_orchestrator",
                    data={"output_type": type(output).__name__},
                )
        except Exception as exc:
            yield RunEvent(
                kind=RunEventKind.ERROR,
                agent_id="langgraph_orchestrator",
                error=str(exc),
            )

    @staticmethod
    def _convert_langgraph_event(event: dict[str, Any]) -> RunEvent:
        """Convert a LangGraph stream event to a Forge RunEvent."""
        event_type = event.get("event", "unknown")
        name = event.get("name", "unknown")
        data = event.get("data", {})

        kind_map: dict[str, RunEventKind] = {
            "on_chat_model_start": RunEventKind.LLM_CALL,
            "on_chat_model_end": RunEventKind.LLM_RESPONSE,
            "on_tool_start": RunEventKind.TOOL_CALL,
            "on_tool_end": RunEventKind.TOOL_RESULT,
            "on_chain_start": RunEventKind.AGENT_START,
            "on_chain_end": RunEventKind.AGENT_END,
        }

        kind = kind_map.get(event_type, RunEventKind.AGENT_END)

        # Extract token usage if available
        input_tokens = 0
        output_tokens = 0
        if "usage_metadata" in data:
            usage = data["usage_metadata"]
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)

        return RunEvent(
            kind=kind,
            agent_id=name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            data={"event_type": event_type, "name": name},
        )


# Expose as 'Adapter' for discovery
Adapter = LangGraphAdapter
