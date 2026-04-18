"""Acceptance tests for Fase γ.2 — :class:`AutoGenAdapter` instrumentation.

AutoGen's structured logging is well-behaved — it emits ``logging.LogRecord``
instances with an ``event`` attribute holding a structured event object. That
makes it trivial to simulate here: we construct records by hand and push them
through the attached handler, asserting that Forge's interceptor/bus are
notified with the right payload.

No real AutoGen install is required; this isolates our tests from the real
framework's churn while still validating every branch of the handler.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

from forge_adapters.autogen_adapter import (
    _AUTOGEN_TRACE_LOGGER_NAME,
    AutoGenAdapter,
    _ForgeAutoGenLogHandler,
)
from forge_core.context import RunContext, run_scope
from forge_core.events import EventBus
from forge_core.types import RunEvent, RunEventKind, TaskEnvelope

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers: fake AutoGen event payloads
# ---------------------------------------------------------------------------


class _FakeEvent:
    """Stand-in for AutoGen's ``LLMCallEvent`` / ``AgentStartEvent`` / etc.

    Attribute-access only — matches AutoGen 0.4+ event class shape.
    """

    def __init__(self, event_type: str, **kwargs: Any) -> None:
        self.event_type = event_type
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_record(event: _FakeEvent) -> logging.LogRecord:
    record = logging.LogRecord(
        name=_AUTOGEN_TRACE_LOGGER_NAME,
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=event.event_type,
        args=None,
        exc_info=None,
    )
    record.event = event  # type: ignore[attr-defined]
    return record


class _RecordingInterceptor:
    """Minimal LLMCallInterceptor implementation for assertions."""

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.ends: list[dict[str, Any]] = []
        self._next_span_id = 0

    async def on_llm_start(
        self,
        *,
        model: str,
        agent_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self._next_span_id += 1
        span_id = f"span-{self._next_span_id}"
        self.starts.append(
            {
                "span_id": span_id,
                "model": model,
                "agent_id": agent_id,
                "metadata": metadata,
            }
        )
        return span_id

    async def on_llm_end(
        self,
        span_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        output: Any = None,
    ) -> None:
        self.ends.append(
            {
                "span_id": span_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "output": output,
            }
        )

    async def on_llm_error(self, span_id: str, error: BaseException) -> None:
        return None


# ---------------------------------------------------------------------------
# Handler-level tests (the interesting logic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_paired_llm_start_and_end_publishes_llm_call_event() -> None:
    """A start event followed by an end event produces one LLM_CALL on the bus."""
    bus = EventBus()
    llm_events: list[RunEvent] = []

    async def record(ev: RunEvent) -> None:
        llm_events.append(ev)

    bus.subscribe(RunEventKind.LLM_CALL, record)

    handler = _ForgeAutoGenLogHandler(bus=bus, interceptor=None, run_id="r-1")

    start = _FakeEvent(
        "LLMCallEvent",
        call_id="call-1",
        model="claude-haiku-4-20250514",
        agent_id="researcher",
    )
    end = _FakeEvent(
        "LLMCallEndEvent",
        call_id="call-1",
        prompt_tokens=120,
        completion_tokens=45,
        response="hello world",
    )

    handler.handle(_make_record(start))
    handler.handle(_make_record(end))
    await handler.drain()

    assert len(llm_events) == 1
    event = llm_events[0]
    assert event.model == "claude-haiku-4-20250514"
    assert event.agent_id == "researcher"
    assert event.input_tokens == 120
    assert event.output_tokens == 45
    assert event.run_id == "r-1"
    # Cost was computed locally — non-zero for a known model / non-zero tokens.
    assert event.cost > 0


@pytest.mark.asyncio
async def test_handler_tolerates_alt_schema_field_names() -> None:
    """AutoGen's token fields changed names across versions — we accept both."""
    bus = EventBus()
    llm_events: list[RunEvent] = []

    async def record(ev: RunEvent) -> None:
        llm_events.append(ev)

    bus.subscribe(RunEventKind.LLM_CALL, record)

    handler = _ForgeAutoGenLogHandler(bus=bus, interceptor=None, run_id="r")
    start = _FakeEvent("LLMCallEvent", call_id="c", model_name="gpt-4o-mini")
    end = _FakeEvent(
        "LLMCallEndEvent",
        call_id="c",
        input_tokens=10,  # newer field name
        output_tokens=20,
    )
    handler.handle(_make_record(start))
    handler.handle(_make_record(end))
    await handler.drain()

    assert len(llm_events) == 1
    assert llm_events[0].model == "gpt-4o-mini"
    assert llm_events[0].input_tokens == 10
    assert llm_events[0].output_tokens == 20


@pytest.mark.asyncio
async def test_handler_agent_start_end_publishes_to_bus() -> None:
    """AgentStartEvent / AgentStopEvent go to the bus with the run_id."""
    bus = EventBus()
    events: list[RunEvent] = []

    async def record(ev: RunEvent) -> None:
        events.append(ev)

    bus.subscribe(RunEventKind.AGENT_START, record)
    bus.subscribe(RunEventKind.AGENT_END, record)

    handler = _ForgeAutoGenLogHandler(bus=bus, interceptor=None, run_id="r-42")
    handler.handle(_make_record(_FakeEvent("AgentStartEvent", agent_id="planner")))
    handler.handle(_make_record(_FakeEvent("AgentStopEvent", agent_id="planner", duration_ms=17.5)))
    await handler.drain()

    kinds = [e.kind for e in events]
    assert RunEventKind.AGENT_START in kinds
    assert RunEventKind.AGENT_END in kinds
    assert all(e.run_id == "r-42" for e in events)
    end_event = next(e for e in events if e.kind == RunEventKind.AGENT_END)
    assert end_event.latency_ms == 17.5


@pytest.mark.asyncio
async def test_handler_ignores_unstructured_log_records() -> None:
    """Records without ``.event`` are harmless — AutoGen emits many of those."""
    interceptor = _RecordingInterceptor()
    handler = _ForgeAutoGenLogHandler(bus=None, interceptor=interceptor, run_id="r")

    raw = logging.LogRecord(
        name=_AUTOGEN_TRACE_LOGGER_NAME,
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="just a log line",
        args=None,
        exc_info=None,
    )
    # No `.event` attr — handler should do nothing.
    handler.handle(raw)
    assert interceptor.starts == []
    assert interceptor.ends == []


@pytest.mark.asyncio
async def test_handler_close_pending_spans_emits_abandoned_llm_call() -> None:
    """If only ``start`` arrived, ``close_pending_spans`` publishes an abandoned call."""
    bus = EventBus()
    events: list[RunEvent] = []

    async def record(ev: RunEvent) -> None:
        events.append(ev)

    bus.subscribe(RunEventKind.LLM_CALL, record)

    handler = _ForgeAutoGenLogHandler(bus=bus, interceptor=None, run_id="r")
    handler.handle(_make_record(_FakeEvent("LLMCallEvent", call_id="c", model="m")))

    orphaned = handler.close_pending_spans()
    await handler.drain()
    assert orphaned == 1
    assert len(events) == 1
    assert events[0].input_tokens == 0
    assert events[0].data.get("abandoned") is True


@pytest.mark.asyncio
async def test_handler_end_without_start_is_dropped_silently() -> None:
    """An orphaned end event doesn't crash or emit a spurious LLM_CALL."""
    bus = EventBus()
    events: list[RunEvent] = []

    async def record(ev: RunEvent) -> None:
        events.append(ev)

    bus.subscribe(RunEventKind.LLM_CALL, record)

    handler = _ForgeAutoGenLogHandler(bus=bus, interceptor=None, run_id="r")
    handler.handle(
        _make_record(
            _FakeEvent(
                "LLMCallEndEvent",
                call_id="ghost",
                prompt_tokens=1,
                completion_tokens=1,
            )
        )
    )
    await handler.drain()
    assert events == []


# ---------------------------------------------------------------------------
# Adapter-level tests
# ---------------------------------------------------------------------------


class _FakeTeam:
    """Stand-in for autogen_agentchat's Team / BaseGroupChat."""

    def __init__(self, script: list[_FakeEvent], participants: list[Any] | None = None) -> None:
        self._script = script
        self._participants = participants or []

    async def run(self, task: str) -> str:
        # Emit every scripted event through the live logger.
        trace_logger = logging.getLogger(_AUTOGEN_TRACE_LOGGER_NAME)
        for ev in self._script:
            trace_logger.handle(_make_record(ev))
        return f"team-output:{task}"


@pytest.mark.asyncio
async def test_adapter_attaches_handler_and_detaches_on_exit() -> None:
    """The autogen trace logger has no extra handler after the run returns."""
    trace_logger = logging.getLogger(_AUTOGEN_TRACE_LOGGER_NAME)
    before = list(trace_logger.handlers)

    bus = EventBus()
    adapter = AutoGenAdapter()
    adapter.set_bus(bus)
    adapter.set_interceptor(_RecordingInterceptor())  # type: ignore[arg-type]
    adapter._team = _FakeTeam(script=[])
    adapter._agents = []
    adapter._loaded = True

    with run_scope(RunContext(run_id="r", task_id="t")):
        await adapter.run(TaskEnvelope(input={"task": "hello"}))

    after = list(trace_logger.handlers)
    assert after == before  # pristine restoration


@pytest.mark.asyncio
async def test_adapter_run_captures_llm_events_through_logger() -> None:
    """End-to-end: the team logs events; the interceptor receives them."""
    interceptor = _RecordingInterceptor()
    bus = EventBus()
    agent_events: list[RunEvent] = []

    async def record(ev: RunEvent) -> None:
        agent_events.append(ev)

    bus.subscribe(RunEventKind.AGENT_START, record)
    bus.subscribe(RunEventKind.AGENT_END, record)

    adapter = AutoGenAdapter()
    adapter.set_bus(bus)
    adapter.set_interceptor(interceptor)  # type: ignore[arg-type]
    adapter._team = _FakeTeam(
        script=[
            _FakeEvent("AgentStartEvent", agent_id="planner"),
            _FakeEvent("LLMCallEvent", call_id="c1", model="gpt-4o-mini", agent_id="planner"),
            _FakeEvent("LLMCallEndEvent", call_id="c1", prompt_tokens=50, completion_tokens=10),
            _FakeEvent("AgentStopEvent", agent_id="planner"),
        ],
        participants=[],
    )
    adapter._agents = []
    adapter._loaded = True

    with run_scope(RunContext(run_id="r-999", task_id="t-999")):
        result = await adapter.run(TaskEnvelope(input={"task": "go"}))

    assert result.status.value == "completed"
    # The LLM call was round-tripped through the interceptor.
    assert len(interceptor.starts) == 1
    assert len(interceptor.ends) == 1
    assert interceptor.ends[0]["input_tokens"] == 50
    # And agent lifecycle events landed on the bus with the right run_id.
    starts = [e for e in agent_events if e.kind == RunEventKind.AGENT_START]
    ends = [e for e in agent_events if e.kind == RunEventKind.AGENT_END]
    assert any(e.agent_id == "planner" for e in starts)
    assert any(e.agent_id == "planner" for e in ends)


@pytest.mark.asyncio
async def test_adapter_execute_returns_crew_level_boundary_events() -> None:
    """Adapter-level AGENT_START/END for the team itself always come back."""
    adapter = AutoGenAdapter()
    adapter._team = _FakeTeam(script=[])
    adapter._agents = []
    adapter._loaded = True

    with run_scope(RunContext(run_id="r", task_id="t")):
        result = await adapter.run(TaskEnvelope(input={"task": "ping"}))

    kinds = [e.kind for e in result.events]
    assert RunEventKind.AGENT_START in kinds
    assert RunEventKind.AGENT_END in kinds


def test_detect_recognizes_autogen_imports(tmp_path: Path) -> None:
    adapter = AutoGenAdapter()
    src = tmp_path / "flow.py"
    src.write_text("from autogen_agentchat.teams import RoundRobinGroupChat\n")
    assert adapter.detect(src) is True
