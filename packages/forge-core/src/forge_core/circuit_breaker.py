"""Circuit breaker (fase ε.4).

Classic three-state breaker (CLOSED → OPEN → HALF_OPEN → CLOSED) with
async-friendly ergonomics. Forge uses one instance per dependency:

* LLM API — trips after N consecutive call failures, cooling down before
  the next attempt. Keeps a hung Anthropic endpoint from starving the
  event loop with timeouts.
* Memory backend — trips on ChromaDB / Neo4j errors and falls back to
  "degraded retrieval" (return empty list + warn) while waiting.
* Evolution loop — trips after M consecutive rollbacks so auto-evolution
  pauses itself when it's doing more harm than good; the operator has
  to manually ``reset()`` to resume.

State transitions
-----------------

* **CLOSED** — calls pass through. Each failure increments
  ``failure_count``; when it reaches ``failure_threshold`` the breaker
  trips to OPEN. A successful call resets the counter.
* **OPEN** — calls fail fast with :class:`CircuitOpenError`. After
  ``recovery_timeout_seconds`` the breaker moves to HALF_OPEN on the
  next call attempt.
* **HALF_OPEN** — the next single call is allowed through as a probe.
  Success → CLOSED and counters reset. Failure → back to OPEN with the
  clock restarted.

All time is via :func:`time.monotonic` so clock skew doesn't strand the
breaker in OPEN after a system resume.
"""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger()

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the breaker is OPEN."""

    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(f"Circuit '{name}' is OPEN; retry in {retry_after:.1f}s")
        self.name = name
        self.retry_after = retry_after


class CircuitBreaker:
    """Async-safe circuit breaker.

    A single instance protects a specific dependency. Share it across
    callers that hit the same backend so they trip together.

    The breaker is non-reentrant by design — ``call()`` serializes
    state transitions through an :class:`asyncio.Lock`. The protected
    function itself runs outside the lock, so parallel calls under a
    healthy breaker don't serialize on each other. Only the
    state-transition bookkeeping is locked.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be > 0")
        self._name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_seconds
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    # ---- introspection ---------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    # ---- public API ------------------------------------------------------

    async def call(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Invoke ``fn(*args, **kwargs)`` under the breaker."""
        await self._before_call()
        try:
            result = await fn(*args, **kwargs)
        except BaseException as exc:  # trip on any failure incl. cancellation? No — only Exception.
            if isinstance(exc, Exception):
                await self._record_failure(exc)
            raise
        await self._record_success()
        return result

    def record_failure(self) -> None:
        """Synchronous failure signal — for callers that already ran the op.

        Useful when the operation isn't a coroutine (e.g. a sync library
        call inside :func:`asyncio.to_thread`). Ignores the lock; state
        transitions are still monotonic and idempotent.
        """
        self._apply_failure_transition()

    def record_success(self) -> None:
        """Synchronous success signal."""
        self._apply_success_transition()

    def reset(self) -> None:
        """Force the breaker back to CLOSED and clear counters."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None
        logger.info("circuit.reset", name=self._name)

    # ---- internals -------------------------------------------------------

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state is CircuitState.OPEN:
                assert self._opened_at is not None
                elapsed = time.monotonic() - self._opened_at
                if elapsed < self._recovery_timeout:
                    raise CircuitOpenError(self._name, self._recovery_timeout - elapsed)
                self._state = CircuitState.HALF_OPEN
                logger.info("circuit.half_open", name=self._name)

    async def _record_failure(self, exc: Exception) -> None:
        async with self._lock:
            self._apply_failure_transition()
            logger.warning(
                "circuit.failure",
                name=self._name,
                state=self._state.value,
                failure_count=self._failure_count,
                error=repr(exc),
            )

    async def _record_success(self) -> None:
        async with self._lock:
            self._apply_success_transition()

    def _apply_failure_transition(self) -> None:
        self._failure_count += 1
        if self._state is CircuitState.HALF_OPEN:
            # Probe failed — reopen and restart the clock.
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            return
        if self._failure_count >= self._failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    def _apply_success_transition(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            logger.info("circuit.closed", name=self._name)
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None


__all__ = ["CircuitBreaker", "CircuitOpenError", "CircuitState"]
