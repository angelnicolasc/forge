"""Tests for :class:`~forge_adapters.generic.GenericCallableAdapter`.

The generic adapter is the "wrap anything" escape hatch in Forge: the user
points it at any async or sync callable, and we still extract cost / token
telemetry by monkey-patching known LLM SDKs (``anthropic`` / ``openai`` /
``google.generativeai``) for the duration of the run.

These tests exercise that contract end-to-end:

* A wired adapter publishes ``LLM_CALL`` events on the bus when the user's
  callable reaches for ``anthropic.Anthropic().messages.create`` — with
  tokens populated from the fake SDK's response shape.
* An unwired adapter (no interceptor, no bus) still runs the callable
  without raising, just without telemetry.
* Both async callables and sync callables work — the sync path must cross
  the ``asyncio.to_thread`` worker boundary.
* On exit, the SDK's real method is restored, regardless of whether the
  callable succeeded or raised.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest

from forge_adapters.generic import GenericCallableAdapter
from forge_core.events import EventBus
from forge_core.types import RunEventKind, TaskEnvelope
from forge_observe.interceptor import ForgeLLMInterceptor

# ---------------------------------------------------------------------------
# Fake anthropic SDK install — same shape as the llm_clients tests but
# duplicated here so the adapter test suite is self-contained.
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeResponse:
    usage: _FakeUsage
    content: str = "response text"


def _install_fake_anthropic() -> type:
    anthropic_pkg = types.ModuleType("anthropic")
    anthropic_pkg.__path__ = []  # type: ignore[attr-defined]
    resources_pkg = types.ModuleType("anthropic.resources")
    resources_pkg.__path__ = []  # type: ignore[attr-defined]
    messages_mod = types.ModuleType("anthropic.resources.messages")

    class Messages:
        def create(self, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(usage=_FakeUsage(input_tokens=50, output_tokens=75))

    class AsyncMessages:
        async def create(self, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(usage=_FakeUsage(input_tokens=60, output_tokens=90))

    messages_mod.Messages = Messages  # type: ignore[attr-defined]
    messages_mod.AsyncMessages = AsyncMessages  # type: ignore[attr-defined]

    sys.modules["anthropic"] = anthropic_pkg
    sys.modules["anthropic.resources"] = resources_pkg
    sys.modules["anthropic.resources.messages"] = messages_mod
    return Messages


@pytest.fixture(autouse=True)
def _clean_modules() -> Any:
    names = [
        "anthropic",
        "anthropic.resources",
        "anthropic.resources.messages",
    ]
    pre = {n: sys.modules.get(n) for n in names}
    yield
    for n in names:
        if pre.get(n) is not None:
            sys.modules[n] = pre[n]  # type: ignore[assignment]
        else:
            sys.modules.pop(n, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_adapter_async_callable_captures_anthropic_call() -> None:
    """End-to-end: async user callable calls Anthropic, event reaches the bus.

    Verifies the Fase α / Fase γ wiring: set_bus + set_interceptor, then
    ``run(envelope)`` runs the user function with SDK instrumentation
    active, and an ``LLM_CALL`` event shows up on the bus with tokens.
    """
    _install_fake_anthropic()
    from anthropic.resources.messages import AsyncMessages

    bus = EventBus()
    llm_events = []

    async def record(ev: Any) -> None:
        llm_events.append(ev)

    bus.subscribe(RunEventKind.LLM_CALL, record)
    interceptor = ForgeLLMInterceptor(bus=bus)

    async def user_flow(payload: dict[str, Any]) -> dict[str, Any]:
        # User's code reaches for Anthropic directly — the kind of thing
        # you can't instrument without the generic adapter.
        inst = AsyncMessages()
        response = await inst.create(
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": payload["q"]}],
        )
        return {"content": response.content}

    adapter = GenericCallableAdapter(fn=user_flow, adapter_name="user_flow")
    adapter.set_interceptor(interceptor)
    adapter.set_bus(bus)
    await adapter.load("<inline>")

    result = await adapter.run(TaskEnvelope(input={"q": "hello"}))
    assert result.status.value == "completed"
    assert result.output == {"content": "response text"}

    # The LLM_CALL event landed — and has populated tokens.
    assert len(llm_events) == 1
    ev = llm_events[0]
    assert ev.model == "claude-sonnet-4-20250514"
    assert ev.input_tokens == 60
    assert ev.output_tokens == 90
    assert ev.cost > 0  # DefaultCostModel has claude-sonnet-4 in the table.


@pytest.mark.asyncio
async def test_generic_adapter_sync_callable_captures_via_to_thread() -> None:
    """Sync user callables get instrumented too.

    The wrapper runs on a worker thread (``asyncio.to_thread``) and must
    marshal its interceptor call back onto the main loop via
    ``run_coroutine_threadsafe``. Regressions here manifest as a
    deadlock or missing events.
    """
    Messages = _install_fake_anthropic()

    bus = EventBus()
    llm_events = []

    async def record(ev: Any) -> None:
        llm_events.append(ev)

    bus.subscribe(RunEventKind.LLM_CALL, record)
    interceptor = ForgeLLMInterceptor(bus=bus)

    def sync_user_flow(payload: dict[str, Any]) -> str:
        inst = Messages()
        response = inst.create(model="claude-haiku-4-20250514")
        return response.content

    adapter = GenericCallableAdapter(fn=sync_user_flow, adapter_name="sync_flow")
    adapter.set_interceptor(interceptor)
    adapter.set_bus(bus)
    await adapter.load("<inline>")

    result = await adapter.run(TaskEnvelope(input={}))
    assert result.status.value == "completed"
    # Give the bus publish scheduled from on_llm_end a tick to land.
    await asyncio.sleep(0)
    assert len(llm_events) == 1
    assert llm_events[0].input_tokens == 50
    assert llm_events[0].output_tokens == 75


@pytest.mark.asyncio
async def test_generic_adapter_without_wiring_runs_uninstrumented() -> None:
    """Without set_interceptor, the adapter degrades to pass-through.

    Useful for prototyping / embedded use — the adapter must not raise
    just because there's no orchestrator attached.
    """
    Messages = _install_fake_anthropic()

    async def user_flow(payload: dict[str, Any]) -> str:
        # Note: no adapter.set_interceptor, so no patching occurs.
        # Calling inst.create must still work; it just produces no events.
        inst = Messages()
        return inst.create(model="x").content

    adapter = GenericCallableAdapter(fn=user_flow, adapter_name="bare")
    await adapter.load("<inline>")
    result = await adapter.run(TaskEnvelope(input={}))
    assert result.status.value == "completed"
    assert result.output == "response text"


@pytest.mark.asyncio
async def test_generic_adapter_restores_sdk_after_run() -> None:
    """The adapter must un-patch the SDK even after a successful run.

    Otherwise subsequent uninstrumented code would still go through
    Forge's wrapper, which is both misleading and a memory leak (the
    wrapper closes over the interceptor).
    """
    _install_fake_anthropic()
    from anthropic.resources.messages import AsyncMessages

    pristine = AsyncMessages.create

    bus = EventBus()
    interceptor = ForgeLLMInterceptor(bus=bus)

    async def user_flow(payload: dict[str, Any]) -> str:
        inst = AsyncMessages()
        await inst.create(model="claude-sonnet-4-20250514")
        # Confirm the patch is live WHILE the callable runs.
        assert AsyncMessages.create is not pristine
        return "ok"

    adapter = GenericCallableAdapter(fn=user_flow, adapter_name="patch_test")
    adapter.set_interceptor(interceptor)
    adapter.set_bus(bus)
    await adapter.load("<inline>")
    await adapter.run(TaskEnvelope(input={}))

    # After the run finishes, the original is restored.
    assert AsyncMessages.create is pristine


@pytest.mark.asyncio
async def test_generic_adapter_restores_sdk_after_exception() -> None:
    """Same guarantee on the error path: revert-in-finally, always."""
    _install_fake_anthropic()
    from anthropic.resources.messages import AsyncMessages

    pristine = AsyncMessages.create
    bus = EventBus()
    interceptor = ForgeLLMInterceptor(bus=bus)

    class _UserError(RuntimeError):
        pass

    async def bad_flow(payload: dict[str, Any]) -> None:
        raise _UserError("intentional")

    adapter = GenericCallableAdapter(fn=bad_flow, adapter_name="bad")
    adapter.set_interceptor(interceptor)
    adapter.set_bus(bus)
    await adapter.load("<inline>")
    result = await adapter.run(TaskEnvelope(input={}))

    assert result.status.value == "failed"
    assert AsyncMessages.create is pristine


@pytest.mark.asyncio
async def test_detect_always_returns_false() -> None:
    """Generic adapter is explicitly opt-in — never auto-detected."""
    adapter = GenericCallableAdapter()
    assert adapter.detect("anything.py") is False


@pytest.mark.asyncio
async def test_agent_start_end_boundary_events_present() -> None:
    """Every run emits AGENT_START + AGENT_END for the callable itself.

    This is useful for the dashboard even when no LLM events fire,
    because it still gives the run a nonzero duration and a named
    topology node.
    """

    async def trivial(payload: dict[str, Any]) -> int:
        return 42

    adapter = GenericCallableAdapter(fn=trivial, adapter_name="trivial")
    await adapter.load("<inline>")
    result = await adapter.run(TaskEnvelope(input={}))
    kinds = [e.kind for e in result.events]
    assert RunEventKind.AGENT_START in kinds
    assert RunEventKind.AGENT_END in kinds
    assert result.output == 42
