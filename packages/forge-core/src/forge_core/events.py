"""In-process event bus that decouples the orchestrator from observers.

The ``MetaOrchestrator`` emits :class:`~forge_core.types.RunEvent` instances
onto an :class:`EventBus`. Independent subscribers — the OTel tracer, cost
tracker, memory ingester, evolution auto-trigger, dashboard SSE pump —
consume those events without any direct coupling to the orchestrator.

Design goals (informed by Gap #2 of the remediation plan — ``ForgeTracer``
implementado pero nunca invocado):

* **Fan-out, not fan-in.** One publish reaches every relevant subscriber.
* **Failure isolation.** A buggy subscriber never breaks publishing. Handlers
  run under ``asyncio.gather(..., return_exceptions=True)`` and exceptions
  are logged but never re-raised.
* **Backpressure-aware.** Subscribers are awaited, so a slow handler slows
  the bus rather than queuing unbounded work.
* **Wildcard subscriptions.** Pass ``"*"`` (or :attr:`ALL_EVENTS`) to receive
  every event, which the tracer and dashboard backend both need.

The bus is intentionally in-process. Cross-process/host transport (OTLP,
SSE to the dashboard) happens in dedicated subscribers, not in the bus.

Usage::

    bus = EventBus()
    handle = bus.subscribe(RunEventKind.LLM_CALL, my_async_handler)
    await bus.publish(RunEvent(kind=RunEventKind.LLM_CALL, ...))
    bus.unsubscribe(handle)
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

import structlog

from forge_core.types import RunEvent, RunEventKind

logger = structlog.get_logger()

EventHandler = Callable[[RunEvent], Awaitable[None]]
EventFilter = RunEventKind | str

#: Sentinel that subscribes a handler to every event, regardless of kind.
ALL_EVENTS: str = "*"


@dataclass(frozen=True)
class SubscriptionHandle:
    """Opaque handle returned by ``EventBus.subscribe``.

    Pass it to ``EventBus.unsubscribe`` to detach the handler. The handle
    captures the filter used at subscription time so unsubscribe is O(1)
    rather than scanning every bucket.
    """

    id: str
    filter: EventFilter


class EventBus:
    """Asynchronous fan-out bus for :class:`RunEvent` instances.

    Thread-safety note: this bus is designed for a single asyncio event loop.
    If you need cross-thread publishing, route through
    ``asyncio.run_coroutine_threadsafe``.
    """

    def __init__(self) -> None:
        # filter -> {handle_id: handler}
        self._subscribers: dict[EventFilter, dict[str, EventHandler]] = defaultdict(dict)

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    def subscribe(
        self,
        kind: EventFilter,
        handler: EventHandler,
    ) -> SubscriptionHandle:
        """Register ``handler`` for events matching ``kind``.

        Args:
            kind: A :class:`RunEventKind` value (exact match), a raw string
                kind, or ``ALL_EVENTS`` to receive every event.
            handler: An ``async`` callable taking a single ``RunEvent``.

        Returns:
            A :class:`SubscriptionHandle` suitable for later
            :meth:`unsubscribe`.
        """
        if not asyncio.iscoroutinefunction(handler):
            raise TypeError(f"EventBus handlers must be async callables; got {handler!r}")
        handle = SubscriptionHandle(id=uuid4().hex[:12], filter=kind)
        self._subscribers[kind][handle.id] = handler
        logger.debug("event_bus.subscribed", kind=str(kind), handle=handle.id)
        return handle

    def unsubscribe(self, handle: SubscriptionHandle) -> bool:
        """Detach the handler referenced by ``handle``.

        Returns ``True`` if a handler was removed, ``False`` if the handle
        was already gone (idempotent).
        """
        bucket = self._subscribers.get(handle.filter)
        if not bucket or handle.id not in bucket:
            return False
        del bucket[handle.id]
        if not bucket:
            # Clean up empty bucket to keep introspection tidy.
            del self._subscribers[handle.filter]
        logger.debug("event_bus.unsubscribed", handle=handle.id)
        return True

    def clear(self) -> None:
        """Remove every subscriber. Intended for test teardown."""
        self._subscribers.clear()

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, event: RunEvent) -> None:
        """Deliver ``event`` to every matching handler.

        Handlers run concurrently via ``asyncio.gather``. Exceptions are
        logged with structured context but never propagate — a broken
        subscriber cannot break the orchestrator.
        """
        handlers: list[EventHandler] = []
        # Specific-kind subscribers first (preserves intuitive ordering
        # when humans read logs — but execution is concurrent anyway).
        kind_bucket = self._subscribers.get(event.kind)
        if kind_bucket:
            handlers.extend(kind_bucket.values())
        wildcard_bucket = self._subscribers.get(ALL_EVENTS)
        if wildcard_bucket:
            handlers.extend(wildcard_bucket.values())

        if not handlers:
            return

        results = await asyncio.gather(
            *(self._safe_invoke(h, event) for h in handlers),
            return_exceptions=True,
        )
        for h, res in zip(handlers, results, strict=False):
            if isinstance(res, BaseException):
                logger.error(
                    "event_bus.handler_error",
                    handler=getattr(h, "__qualname__", repr(h)),
                    kind=str(event.kind),
                    error=str(res),
                    error_type=type(res).__name__,
                )

    @staticmethod
    async def _safe_invoke(handler: EventHandler, event: RunEvent) -> None:
        """Invoke ``handler`` — called inside ``gather(return_exceptions=True)``.

        We keep this thin so exceptions surface at the gather layer and all
        error handling is centralized in :meth:`publish`.
        """
        await handler(event)

    # ------------------------------------------------------------------
    # Introspection (useful for tests and dashboards)
    # ------------------------------------------------------------------

    def subscriber_count(self, kind: EventFilter | None = None) -> int:
        """Return how many subscribers are attached.

        If ``kind`` is provided, counts only subscribers for that filter;
        otherwise sums across all filters.
        """
        if kind is None:
            return sum(len(b) for b in self._subscribers.values())
        return len(self._subscribers.get(kind, {}))

    def has_subscribers(self, kind: EventFilter) -> bool:
        """Return ``True`` if ``kind`` has at least one subscriber (including wildcard)."""
        if self._subscribers.get(kind):
            return True
        return bool(self._subscribers.get(ALL_EVENTS))
