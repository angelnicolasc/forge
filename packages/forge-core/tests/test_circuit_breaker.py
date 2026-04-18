"""Tests for CircuitBreaker (fase ε.4)."""

from __future__ import annotations

import asyncio

import pytest

from forge_core.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


async def _ok() -> str:
    return "ok"


async def _boom() -> None:
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_starts_closed_and_passes_calls_through() -> None:
    cb = CircuitBreaker("llm", failure_threshold=3)
    assert cb.state is CircuitState.CLOSED
    assert await cb.call(_ok) == "ok"
    assert cb.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_failures_below_threshold_keep_closed() -> None:
    cb = CircuitBreaker("llm", failure_threshold=3)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(_boom)
    assert cb.state is CircuitState.CLOSED
    assert cb.failure_count == 2


@pytest.mark.asyncio
async def test_trips_to_open_at_threshold() -> None:
    cb = CircuitBreaker("llm", failure_threshold=3)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(_boom)
    assert cb.state is CircuitState.OPEN


@pytest.mark.asyncio
async def test_open_rejects_calls_with_circuit_open_error() -> None:
    cb = CircuitBreaker("llm", failure_threshold=1, recovery_timeout_seconds=60)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert cb.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError) as exc_info:
        await cb.call(_ok)
    assert exc_info.value.retry_after > 0


@pytest.mark.asyncio
async def test_recovers_through_half_open_on_successful_probe() -> None:
    cb = CircuitBreaker("llm", failure_threshold=1, recovery_timeout_seconds=0.05)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert cb.state is CircuitState.OPEN
    await asyncio.sleep(0.1)
    # Next call enters HALF_OPEN and succeeds → CLOSED.
    assert await cb.call(_ok) == "ok"
    assert cb.state is CircuitState.CLOSED
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_half_open_probe_failure_reopens() -> None:
    cb = CircuitBreaker("llm", failure_threshold=1, recovery_timeout_seconds=0.05)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    await asyncio.sleep(0.1)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert cb.state is CircuitState.OPEN


@pytest.mark.asyncio
async def test_success_resets_failure_count() -> None:
    cb = CircuitBreaker("llm", failure_threshold=5)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(_boom)
    assert cb.failure_count == 3
    await cb.call(_ok)
    assert cb.failure_count == 0
    assert cb.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_reset_forces_closed() -> None:
    cb = CircuitBreaker("e", failure_threshold=1, recovery_timeout_seconds=3600)
    with pytest.raises(RuntimeError):
        await cb.call(_boom)
    assert cb.state is CircuitState.OPEN
    cb.reset()
    assert cb.state is CircuitState.CLOSED
    assert await cb.call(_ok) == "ok"


def test_invalid_thresholds_rejected() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker("x", failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker("x", recovery_timeout_seconds=0)


@pytest.mark.asyncio
async def test_sync_record_helpers_track_state() -> None:
    cb = CircuitBreaker("sync", failure_threshold=2)
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    cb.record_success()  # from within half_open it would reset; from open just resets counters
    # record_success always returns to CLOSED per impl; acceptable for sync fast-path callers.
    assert cb.state is CircuitState.CLOSED
