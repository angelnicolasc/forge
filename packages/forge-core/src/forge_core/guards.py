"""Runtime guards for cost and time budgets.

A run without enforced limits is a loaded gun pointed at the operator's
credit card — the adapters are happy to grind through a $40 API bill if
the user's callable recurses. Two guards make this safe:

* :class:`CostBudgetGuard` subscribes to the event bus's ``LLM_CALL``
  events and tallies total cost. When the tally meets or exceeds the
  per-run ceiling, it signals an :class:`asyncio.Event` that
  :func:`run_with_guards` is racing against the adapter's ``run`` task.
  The adapter task gets cancelled; :exc:`BudgetExceeded` is raised.

* A plain ``asyncio.wait_for(task, timeout_seconds)`` enforces the wall
  clock. If the adapter hangs, ``wait_for`` cancels it and we raise
  :exc:`TimeoutExceeded`.

These guards are orthogonal: either can fire independently. Both always
leave the bus in a well-defined state — the guard cleans up its
subscription in ``__aexit__`` / ``dispose``.

Why not just let the orchestrator check cost after the run?
-----------------------------------------------------------

Because some frameworks (a recursive LangGraph agent, an infinite-loop
coding agent) can issue thousands of LLM calls in a single invocation.
Post-hoc enforcement is "close the barn door after the horse is gone."
The guard fires mid-flight, *before* the next LLM call fees get added.
That's the only honest form of enforcement.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from forge_core.types import RunEvent, RunEventKind

if TYPE_CHECKING:
    from forge_core.events import EventBus, SubscriptionHandle

logger = structlog.get_logger()


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class BudgetExceeded(RuntimeError):  # noqa: N818 -- `BudgetExceeded` is public API since α; renaming breaks downstream `except BudgetExceeded` clauses.
    """Raised when a run's accumulated LLM cost reaches the ceiling.

    The error message includes the ceiling and the cost that triggered
    the trip — both useful for operator dashboards. The orchestrator
    catches this and marks the run as :attr:`RunStatus.FAILED` with the
    full message in ``RunResult.errors``.
    """

    def __init__(self, *, ceiling: Decimal, observed: Decimal) -> None:
        self.ceiling = ceiling
        self.observed = observed
        super().__init__(f"Run cost ceiling ${ceiling} exceeded (observed ${observed}).")


class TimeoutExceeded(RuntimeError):  # noqa: N818 -- public API, see above.
    """Raised when a run exceeds its wall-clock budget.

    Thin subclass of :class:`RuntimeError` rather than
    :class:`asyncio.TimeoutError` because we want callers to catch our
    specific type without accidentally catching unrelated timeout
    errors from deep inside the adapter.
    """

    def __init__(self, *, timeout_seconds: float, elapsed_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            f"Run wall-clock budget {timeout_seconds:.1f}s exceeded "
            f"(elapsed {elapsed_seconds:.1f}s)."
        )


# ----------------------------------------------------------------------
# CostBudgetGuard
# ----------------------------------------------------------------------


@dataclass
class _CostState:
    """Mutable accumulator kept separate so the guard's public API can
    expose a read-only view without copying on every LLM_CALL event.
    """

    total: Decimal = field(default_factory=lambda: Decimal("0"))
    calls: int = 0


class CostBudgetGuard:
    """Tracks per-run cost via the event bus and trips at the ceiling.

    The guard is one-shot: ``trip_event`` fires exactly once, the first
    time the tallied cost meets ``ceiling``. Subsequent LLM_CALL events
    keep updating ``observed`` for post-mortem visibility but do not
    re-trigger the event.

    Lifecycle
    ---------

    1. Construct with the ceiling and the bus.
    2. :meth:`attach` subscribes to ``LLM_CALL`` — idempotent, safe to
       call twice (second call is a warning).
    3. Race :meth:`wait_tripped` against the adapter's ``run`` task
       using :func:`asyncio.wait(return_when=FIRST_COMPLETED)`.
    4. Always call :meth:`dispose` in a ``finally`` — it unsubscribes
       from the bus so the guard doesn't outlive the run.
    """

    def __init__(
        self,
        *,
        ceiling: Decimal,
        bus: EventBus,
    ) -> None:
        self._ceiling = Decimal(ceiling)
        self._bus = bus
        self._state = _CostState()
        self._tripped = asyncio.Event()
        self._subscription: SubscriptionHandle | None = None

    @property
    def ceiling(self) -> Decimal:
        return self._ceiling

    @property
    def observed(self) -> Decimal:
        return self._state.total

    @property
    def tripped(self) -> bool:
        return self._tripped.is_set()

    @property
    def llm_calls(self) -> int:
        return self._state.calls

    def attach(self) -> None:
        """Subscribe to the bus's LLM_CALL events.

        Ceilings of zero or negative are treated as "no limit" — the
        subscription still attaches so :attr:`observed` is populated,
        but :attr:`tripped` will never fire. This preserves the
        invariant that the guard never interferes with runs that
        explicitly opt out.
        """
        if self._subscription is not None:
            logger.warning("cost_budget_guard.attach_called_twice")
            return
        self._subscription = self._bus.subscribe(RunEventKind.LLM_CALL, self._on_llm_call)

    async def _on_llm_call(self, event: RunEvent) -> None:
        # event.cost can be a Decimal or raw string depending on source;
        # coerce for safety. Missing cost is treated as zero so malformed
        # events don't crash the guard.
        try:
            cost = Decimal(event.cost) if event.cost is not None else Decimal("0")
        except Exception:
            cost = Decimal("0")
        self._state.total += cost
        self._state.calls += 1

        # Zero / negative ceiling means "no limit" — skip tripping.
        if self._ceiling <= 0:
            return
        if not self._tripped.is_set() and self._state.total >= self._ceiling:
            self._tripped.set()
            logger.info(
                "cost_budget_guard.tripped",
                ceiling=str(self._ceiling),
                observed=str(self._state.total),
                calls=self._state.calls,
            )

    async def wait_tripped(self) -> None:
        """Block until the budget is tripped.

        Useful as one leg of an :func:`asyncio.wait(...)` race.
        """
        await self._tripped.wait()

    def dispose(self) -> None:
        """Unsubscribe from the bus so the guard can be garbage-collected.

        Safe to call multiple times.
        """
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)
            self._subscription = None


# ----------------------------------------------------------------------
# Combined guard: cost race + timeout
# ----------------------------------------------------------------------


async def run_with_guards(
    run_coro_factory: Any,
    *,
    bus: EventBus,
    cost_ceiling: Decimal,
    timeout_seconds: float | None,
) -> Any:
    """Run a coroutine under both a cost budget and a wall-clock timeout.

    Parameters
    ----------
    run_coro_factory
        A zero-arg callable that returns a fresh coroutine to wrap.
        We take a factory (not a coroutine) because ``asyncio.wait_for``
        cancels its inner task; if we'd been handed the coroutine
        directly we couldn't retry / recover without surprising the
        caller who thinks they still own it. Factory semantics are
        explicit.
    bus
        Event bus to attach the :class:`CostBudgetGuard` to.
    cost_ceiling
        Ceiling in USD. ``Decimal(0)`` or negative disables cost
        enforcement.
    timeout_seconds
        Wall-clock budget. ``None``, ``0``, or negative disables
        timeout enforcement.

    Returns
    -------
    Whatever the inner coroutine returned. On cost or timeout trip,
    raises :exc:`BudgetExceeded` / :exc:`TimeoutExceeded`.

    Semantics when both guards trip
    -------------------------------
    Cost wins. The cost guard's event fires synchronously inside the
    bus handler, so by the time ``asyncio.wait`` notices, the cost
    signal is already set. Timeout is a race-to-first-completed: if
    the timeout hits first we raise timeout; otherwise cost.
    """
    cost_guard = CostBudgetGuard(ceiling=cost_ceiling, bus=bus)
    cost_guard.attach()
    # Timeout disabled?
    timeout = float(timeout_seconds) if timeout_seconds and float(timeout_seconds) > 0 else None

    loop = asyncio.get_running_loop()
    start = loop.time()

    try:
        run_task = asyncio.create_task(run_coro_factory())
        trip_task = asyncio.create_task(cost_guard.wait_tripped())

        # If there's no timeout, race cost vs run to completion. If
        # there IS a timeout, the outer ``wait_for`` handles it.
        async def _race() -> Any:
            done, _pending = await asyncio.wait(
                {run_task, trip_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Cost tripped before run finished?
            if trip_task in done and not run_task.done():
                run_task.cancel()
                # Let cancellation propagate but swallow the CancelledError.
                with contextlib.suppress(asyncio.CancelledError, BaseException):
                    await run_task
                raise BudgetExceeded(ceiling=cost_guard.ceiling, observed=cost_guard.observed)
            # Run finished first — cancel the (pending) trip task.
            if not trip_task.done():
                trip_task.cancel()
            return run_task.result()

        if timeout is None:
            return await _race()
        try:
            return await asyncio.wait_for(_race(), timeout=timeout)
        except TimeoutError as exc:
            elapsed = loop.time() - start
            # Make sure inner tasks are cleaned up.
            for t in (run_task, trip_task):
                if not t.done():
                    t.cancel()
            raise TimeoutExceeded(timeout_seconds=timeout, elapsed_seconds=elapsed) from exc
    finally:
        cost_guard.dispose()
