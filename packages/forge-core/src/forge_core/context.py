"""Context propagation via :mod:`contextvars`.

The ``RunContext`` travels with an in-flight Forge run through arbitrarily
deep async call stacks. Any downstream component (LLM interceptors, tracer,
memory ingester, mutators) reads ``current_run()`` to know which run it is
inside *without* the adapter having to plumb parameters through every layer.

This is the architectural foundation for fixing Gap #3 ("ContextPropagation
no existe — spans no se anidan") in the L7/L8 remediation plan. It mirrors
how OpenTelemetry's ``context`` module propagates the active span.

Usage (inside ``MetaOrchestrator.run``)::

    ctx = RunContext(run_id=..., task_id=...)
    with run_scope(ctx):
        # Any code here — and any async task it awaits — can call current_run()
        # to retrieve the same ``ctx``. On exit, the previous context is restored.
        result = await self._adapter.run(envelope)

In an LLM interceptor::

    from forge_core.context import current_run

    async def on_llm_start(...):
        ctx = current_run()
        if ctx is None:
            # Instrumentation was enabled outside a run scope — emit a
            # warning but don't fail (the LLM call itself must still succeed).
            return
        span_id = uuid4().hex[:16]
        ...
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

from forge_core.types import RunContext  # re-exported at runtime via __all__

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "RunContext",
    "bind_run",
    "child_scope",
    "current_run",
    "require_run",
    "run_scope",
    "unbind_run",
]


# The single ContextVar instance. Default is None so that code running
# outside a run scope (e.g. cold-start instrumentation) observes the absence
# of a run rather than blowing up with a LookupError.
_CURRENT_RUN: ContextVar[RunContext | None] = ContextVar("forge_run_context", default=None)


def current_run() -> RunContext | None:
    """Return the ``RunContext`` bound to the current async task, or ``None``.

    Returns ``None`` when called outside of a ``run_scope``. Callers that
    require a run context (and should error otherwise) should use
    ``require_run()`` instead.
    """
    return _CURRENT_RUN.get()


def require_run() -> RunContext:
    """Return the current ``RunContext``; raise ``RuntimeError`` if absent.

    Use this at boundaries where a missing context represents a bug
    (e.g., inside ``MetaOrchestrator`` once the run has started).
    """
    ctx = _CURRENT_RUN.get()
    if ctx is None:
        raise RuntimeError("No active RunContext. Call require_run() only inside a run_scope.")
    return ctx


def bind_run(ctx: RunContext) -> Token[RunContext | None]:
    """Bind ``ctx`` as the current run and return a reset ``Token``.

    The caller is responsible for calling ``unbind_run(token)`` (or using
    ``run_scope`` which does it automatically). Prefer ``run_scope`` unless
    you explicitly need manual control.
    """
    return _CURRENT_RUN.set(ctx)


def unbind_run(token: Token[RunContext | None]) -> None:
    """Reset the ContextVar to the value it had before :func:`bind_run`."""
    _CURRENT_RUN.reset(token)


@contextmanager
def run_scope(ctx: RunContext) -> Iterator[RunContext]:
    """Context manager: bind ``ctx`` for the duration of the ``with`` block.

    On exit, the previous context is restored — whether the block exited
    normally or via an exception. This is the recommended way to establish
    a run context because it guarantees symmetric bind/unbind even under
    error paths.

    Example::

        with run_scope(RunContext(run_id="r1", task_id="t1")) as ctx:
            assert current_run() is ctx
            await do_work()
        assert current_run() is None  # restored
    """
    token = _CURRENT_RUN.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT_RUN.reset(token)


@contextmanager
def child_scope(
    *,
    span_id: str | None = None,
    agent_id: str | None = None,
) -> Iterator[RunContext]:
    """Bind a child context derived from the current run for a nested scope.

    Fails loudly with ``RuntimeError`` if called outside of an active
    ``run_scope``, because a child without a parent is always a bug.

    Example::

        with run_scope(parent_ctx):
            ...
            with child_scope(span_id="llm-call-1", agent_id="planner") as child:
                # child.parent_span_id == parent_ctx.span_id
                await llm_call()
    """
    parent = require_run()
    child = parent.child(span_id=span_id, agent_id=agent_id)
    token = _CURRENT_RUN.set(child)
    try:
        yield child
    finally:
        _CURRENT_RUN.reset(token)
