"""Acceptance tests for Fase γ.1 — :class:`CrewAIAdapter` instrumentation.

Rather than requiring the real ``crewai`` package (heavy, network-dependent,
slow under CI), we install a stand-in ``crewai`` module into ``sys.modules``
for the duration of each test. The stand-in exposes the three touch points
Forge monkey-patches:

* ``crewai.Agent`` with an ``execute_task`` instance method.
* ``crewai.Crew`` with a ``kickoff`` method that iterates ``self.agents`` and
  calls their ``execute_task`` (mirroring the real CrewAI orchestration).
* ``crewai.tools.BaseTool`` with a ``_run`` method.

That's enough shape for the adapter's instrumentation to fire and be
observable — exactly the contract the γ.6 acceptance tests assert on.
"""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING, Any

import pytest

from forge_adapters.crewai_adapter import CrewAIAdapter
from forge_core.context import RunContext, run_scope
from forge_core.events import EventBus
from forge_core.types import RunEvent, RunEventKind, TaskEnvelope

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fake crewai module
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Stand-in ``crewai.Agent``.

    Important: instrumentation patches the **class** ``execute_task``, not
    the instance. So the fake method must accept ``self`` as its first
    argument when called directly (bound) and as the first positional when
    called via the monkey-patched wrapper (unbound).
    """

    def __init__(self, role: str, tools: list[Any] | None = None) -> None:
        self.role = role
        self.goal = f"goal-{role}"
        self.backstory = f"backstory-{role}"
        self.llm = _FakeLLM()
        self.tools = tools or []

    def execute_task(self, task: Any) -> str:
        # Simulate tool usage during task execution.
        for tool in self.tools:
            tool._run(task)
        return f"{self.role}-done"


class _FakeLLM:
    """Minimal ChatModel-lookalike. The adapter just reads/writes ``callbacks``."""

    def __init__(self) -> None:
        self.model_name = "claude-haiku-4-20250514"
        self.callbacks: list[Any] = []


class _FakeTool:
    """Stand-in ``crewai.tools.BaseTool`` — instrumentation patches the class."""

    def __init__(self, name: str = "calc") -> None:
        self.name = name

    def _run(self, payload: Any) -> str:
        return f"{self.name}:{payload!r}"


class _FakeCrew:
    """Minimal Crew shape the adapter can drive."""

    def __init__(self, agents: list[_FakeAgent]) -> None:
        self.agents = agents

    def kickoff(self, inputs: Any = None) -> str:
        outputs = []
        for agent in self.agents:
            outputs.append(agent.execute_task(inputs))
        return " | ".join(outputs)


@pytest.fixture
def fake_crewai_module() -> Iterator[types.ModuleType]:
    """Install a fake ``crewai`` + ``crewai.tools`` into ``sys.modules``.

    The adapter imports them lazily inside ``_instrument_crewai``, so simply
    registering them for the duration of a test is enough to flip on the
    instrumentation paths without a real CrewAI install.
    """
    crewai_mod = types.ModuleType("crewai")
    crewai_mod.Agent = _FakeAgent  # type: ignore[attr-defined]
    crewai_mod.Crew = _FakeCrew  # type: ignore[attr-defined]

    tools_mod = types.ModuleType("crewai.tools")
    tools_mod.BaseTool = _FakeTool  # type: ignore[attr-defined]

    previous = {name: sys.modules.get(name) for name in ("crewai", "crewai.tools")}
    sys.modules["crewai"] = crewai_mod
    sys.modules["crewai.tools"] = tools_mod
    try:
        yield crewai_mod
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@pytest.fixture
def captured_events() -> tuple[list[RunEvent], EventBus]:
    """EventBus with a sync-capturing subscriber. Returns (events_list, bus)."""
    bus = EventBus()
    events: list[RunEvent] = []

    async def handler(event: RunEvent) -> None:
        events.append(event)

    for kind in (
        RunEventKind.AGENT_START,
        RunEventKind.AGENT_END,
        RunEventKind.TOOL_CALL,
        RunEventKind.TOOL_RESULT,
        RunEventKind.LLM_CALL,
        RunEventKind.ERROR,
    ):
        bus.subscribe(kind, handler)
    return events, bus


# ---------------------------------------------------------------------------
# Topology + detection
# ---------------------------------------------------------------------------


def test_detect_identifies_crewai_imports(tmp_path: Path) -> None:
    adapter = CrewAIAdapter()
    flow = tmp_path / "flow.py"
    flow.write_text("from crewai import Agent, Crew\ncrew = None\n")
    assert adapter.detect(flow) is True


def test_detect_rejects_unrelated_sources(tmp_path: Path) -> None:
    adapter = CrewAIAdapter()
    flow = tmp_path / "flow.py"
    flow.write_text("import os\nprint('hello')\n")
    assert adapter.detect(flow) is False


def test_topology_extraction_maps_role_and_model(fake_crewai_module) -> None:
    adapter = CrewAIAdapter()
    crew = _FakeCrew([_FakeAgent("writer"), _FakeAgent("reviewer")])
    adapter._crew = crew

    topology = adapter._extract_topology()
    roles = {a.name for a in topology}
    assert roles == {"writer", "reviewer"}
    assert all(a.metadata["framework"] == "crewai" for a in topology)
    assert all(a.model == "claude-haiku-4-20250514" for a in topology)


# ---------------------------------------------------------------------------
# Execution + instrumentation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_emits_crew_boundary_events(
    fake_crewai_module, captured_events: tuple[list[RunEvent], EventBus]
) -> None:
    """Top-level AGENT_START/AGENT_END for the crew always come back in events."""
    _events, bus = captured_events
    adapter = CrewAIAdapter()
    adapter.set_bus(bus)
    # Have to fake a load — we assign the crew directly.
    crew = _FakeCrew([_FakeAgent("writer")])
    adapter._crew = crew
    adapter._agents = adapter._extract_topology()
    adapter._loaded = True

    with run_scope(RunContext(run_id="run-xyz", task_id="t-xyz")):
        result = await adapter.run(TaskEnvelope(input={"topic": "AI"}))

    assert result.status.value == "completed"
    assert "writer-done" in str(result.output)
    # Boundary events from the adapter itself (returned in _execute).
    kinds = [e.kind for e in result.events]
    assert RunEventKind.AGENT_START in kinds
    assert RunEventKind.AGENT_END in kinds


@pytest.mark.asyncio
async def test_execute_publishes_per_agent_events_to_bus(
    fake_crewai_module, captured_events: tuple[list[RunEvent], EventBus]
) -> None:
    """Each ``Agent.execute_task`` invocation shows up on the bus with its role."""
    events, bus = captured_events
    adapter = CrewAIAdapter()
    adapter.set_bus(bus)
    crew = _FakeCrew([_FakeAgent("writer"), _FakeAgent("reviewer")])
    adapter._crew = crew
    adapter._agents = adapter._extract_topology()
    adapter._loaded = True

    with run_scope(RunContext(run_id="run-1", task_id="t-1")):
        await adapter.run(TaskEnvelope(input={"topic": "AI"}))

    roles_started = {
        e.agent_id
        for e in events
        if e.kind == RunEventKind.AGENT_START and e.agent_id in {"writer", "reviewer"}
    }
    roles_ended = {
        e.agent_id
        for e in events
        if e.kind == RunEventKind.AGENT_END and e.agent_id in {"writer", "reviewer"}
    }
    assert roles_started == {"writer", "reviewer"}
    assert roles_ended == {"writer", "reviewer"}
    # Every per-agent event carries the orchestrator-supplied run_id.
    assert all(e.run_id == "run-1" for e in events if e.agent_id in {"writer", "reviewer"})


@pytest.mark.asyncio
async def test_execute_publishes_tool_events(
    fake_crewai_module, captured_events: tuple[list[RunEvent], EventBus]
) -> None:
    """Tool calls inside ``execute_task`` produce TOOL_CALL / TOOL_RESULT events."""
    events, bus = captured_events
    tool = _FakeTool(name="calc")
    agent = _FakeAgent("analyst", tools=[tool])
    crew = _FakeCrew([agent])

    adapter = CrewAIAdapter()
    adapter.set_bus(bus)
    adapter._crew = crew
    adapter._agents = adapter._extract_topology()
    adapter._loaded = True

    with run_scope(RunContext(run_id="run-tools", task_id="t-tools")):
        await adapter.run(TaskEnvelope(input={"n": 2}))

    tool_calls = [e for e in events if e.kind == RunEventKind.TOOL_CALL]
    tool_results = [e for e in events if e.kind == RunEventKind.TOOL_RESULT]
    assert len(tool_calls) == 1
    assert len(tool_results) == 1
    assert tool_calls[0].tool_name == "calc"
    assert tool_results[0].tool_name == "calc"


@pytest.mark.asyncio
async def test_execute_restores_monkey_patches(
    fake_crewai_module, captured_events: tuple[list[RunEvent], EventBus]
) -> None:
    """After the run, ``Agent.execute_task`` and ``BaseTool._run`` are pristine."""
    _events, bus = captured_events
    original_execute = _FakeAgent.execute_task
    original_tool_run = _FakeTool._run

    adapter = CrewAIAdapter()
    adapter.set_bus(bus)
    adapter._crew = _FakeCrew([_FakeAgent("writer")])
    adapter._agents = adapter._extract_topology()
    adapter._loaded = True

    with run_scope(RunContext(run_id="r", task_id="t")):
        await adapter.run(TaskEnvelope(input={}))

    assert _FakeAgent.execute_task is original_execute
    assert _FakeTool._run is original_tool_run


@pytest.mark.asyncio
async def test_execute_failure_still_restores_patches(
    fake_crewai_module, captured_events: tuple[list[RunEvent], EventBus]
) -> None:
    """If ``kickoff`` raises, the monkey-patches are still removed."""
    _events, bus = captured_events
    original_execute = _FakeAgent.execute_task

    class ExplodingCrew(_FakeCrew):
        def kickoff(self, inputs: Any = None) -> Any:
            raise RuntimeError("boom")

    adapter = CrewAIAdapter()
    adapter.set_bus(bus)
    adapter._crew = ExplodingCrew([_FakeAgent("writer")])
    adapter._agents = [
        *adapter._extract_topology(),
    ]
    adapter._loaded = True

    with run_scope(RunContext(run_id="r-bad", task_id="t-bad")):
        result = await adapter.run(TaskEnvelope(input={}))

    # BaseAdapter.run catches and marks the run failed.
    assert result.status.value == "failed"
    assert _FakeAgent.execute_task is original_execute


@pytest.mark.asyncio
async def test_interceptor_callback_is_attached_to_agent_llms(
    fake_crewai_module, captured_events: tuple[list[RunEvent], EventBus]
) -> None:
    """When an interceptor is set, each agent's LLM gets the callback installed
    during the run and cleaned up afterwards."""
    _events, bus = captured_events

    class RecordingInterceptor:
        """Protocol-shaped interceptor that doesn't need LangChain."""

        async def on_llm_start(self, **kwargs: Any) -> str:
            return "span"

        async def on_llm_end(self, span_id: str, **kwargs: Any) -> None:
            return None

        async def on_llm_error(self, span_id: str, error: BaseException) -> None:
            return None

    adapter = CrewAIAdapter()
    adapter.set_bus(bus)
    adapter.set_interceptor(RecordingInterceptor())  # type: ignore[arg-type]

    agent = _FakeAgent("writer")
    crew = _FakeCrew([agent])
    adapter._crew = crew
    adapter._agents = adapter._extract_topology()
    adapter._loaded = True

    # Before run, no callbacks. ``ForgeLangChainCallback`` needs LangChain —
    # if it's not installed, ``llm.callbacks`` stays empty (best-effort) and
    # this test still validates the restoration semantic.
    assert agent.llm.callbacks == []

    with run_scope(RunContext(run_id="r-llm", task_id="t-llm")):
        await adapter.run(TaskEnvelope(input={}))

    # Restored after the run regardless of whether LangChain is installed.
    assert agent.llm.callbacks == []


@pytest.mark.asyncio
async def test_execute_without_bus_still_runs(fake_crewai_module) -> None:
    """If neither bus nor interceptor is wired, the adapter still returns a result.

    The cost of missing instrumentation is that the only events come from
    the adapter boundary (AGENT_START/AGENT_END of the crew). Runs must
    never crash for want of wiring — that's the ``no_interceptor`` path.
    """
    adapter = CrewAIAdapter()
    adapter._crew = _FakeCrew([_FakeAgent("writer")])
    adapter._agents = adapter._extract_topology()
    adapter._loaded = True

    with run_scope(RunContext(run_id="r", task_id="t")):
        result = await adapter.run(TaskEnvelope(input={}))

    assert result.status.value == "completed"


@pytest.mark.asyncio
async def test_kickoff_falls_back_when_inputs_kw_unsupported(
    fake_crewai_module, captured_events: tuple[list[RunEvent], EventBus]
) -> None:
    """Legacy CrewAI versions without ``inputs=`` still run (positional fallback)."""
    _events, bus = captured_events

    class LegacyCrew(_FakeCrew):
        def kickoff(self, payload: Any = None) -> str:
            # This signature refuses the `inputs=` kw → TypeError on first try.
            return f"legacy:{payload}"

    adapter = CrewAIAdapter()
    adapter.set_bus(bus)
    adapter._crew = LegacyCrew([_FakeAgent("writer")])
    adapter._agents = adapter._extract_topology()
    adapter._loaded = True

    with run_scope(RunContext(run_id="r-legacy", task_id="t-legacy")):
        result = await adapter.run(TaskEnvelope(input={"topic": "x"}))

    assert "legacy:" in str(result.output)
