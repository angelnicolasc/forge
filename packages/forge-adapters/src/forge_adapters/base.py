"""Base adapter with shared instrumentation logic.

All framework-specific adapters inherit from BaseAdapter to get
automatic event collection, cost tracking, and topology management.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from forge_core.types import (
    AgentCard,
    RunEvent,
    RunEventKind,
    RunResult,
    RunStatus,
    TaskEnvelope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

logger = structlog.get_logger()


class BaseAdapter(ABC):
    """Abstract base class for Forge adapters.

    Provides shared infrastructure: event collection, timing, error handling.
    Subclasses implement the framework-specific logic in _execute().
    """

    def __init__(self) -> None:
        self._agents: list[AgentCard] = []
        self._source: str | Path | None = None
        self._loaded = False

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable adapter name."""
        ...

    @abstractmethod
    def detect(self, source: str | Path) -> bool:
        """Return True if this adapter can handle the given source."""
        ...

    @abstractmethod
    async def _load_impl(self, source: str | Path) -> list[AgentCard]:
        """Framework-specific loading logic. Return discovered agents."""
        ...

    @abstractmethod
    async def _execute(self, envelope: TaskEnvelope) -> tuple[object, list[RunEvent]]:
        """Framework-specific execution. Return (output, events)."""
        ...

    async def load(self, source: str | Path) -> None:
        """Load and prepare the agent flow from source."""
        self._source = source
        self._agents = await self._load_impl(source)
        self._loaded = True
        logger.info(
            "adapter.loaded",
            adapter=self.name,
            source=str(source),
            agents=len(self._agents),
        )

    async def run(self, envelope: TaskEnvelope) -> RunResult:
        """Execute the flow and return a complete result."""
        if not self._loaded:
            raise RuntimeError(f"Adapter '{self.name}' not loaded. Call load() first.")

        start = time.perf_counter()
        start_dt = datetime.now(UTC)

        try:
            output, events = await self._execute(envelope)
            elapsed = (time.perf_counter() - start) * 1000

            return RunResult(
                task_id=envelope.task_id,
                status=RunStatus.COMPLETED,
                output=output,
                duration_ms=elapsed,
                events=events,
                topology=self._agents,
                started_at=start_dt,
                completed_at=datetime.now(UTC),
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            return RunResult(
                task_id=envelope.task_id,
                status=RunStatus.FAILED,
                duration_ms=elapsed,
                events=[
                    RunEvent(kind=RunEventKind.ERROR, error=str(exc)),
                ],
                errors=[str(exc)],
                topology=self._agents,
                started_at=start_dt,
                completed_at=datetime.now(UTC),
            )

    async def stream(self, envelope: TaskEnvelope) -> AsyncIterator[RunEvent]:
        """Default streaming: run and yield events after completion."""
        result = await self.run(envelope)
        for event in result.events:
            yield event

    def topology(self) -> list[AgentCard]:
        """Return the current agent topology."""
        return list(self._agents)
