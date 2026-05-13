"""forge_skills.executor — SkillExecutor: run a skill in a nested RunContext scope.

Isolation strategy: nested run_scope / child_scope (TaskEnvelope-nesting approach).
Zero serialization overhead. Each skill invocation gets a child RunContext that:
  - inherits the parent run_id (tracing continuity)
  - receives a new agent_id = "skill:<name>" (span isolation)
  - restores the parent context on exit, even under exception
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

import structlog

from forge_core.context import child_scope, current_run, run_scope
from forge_core.types import RunContext, RunEvent, RunEventKind
from forge_skills.types import (
    SkillBudgetSnapshot,
    SkillDef,
    SkillHandlerFn,
    SkillInvocation,
    SkillResult,
)

logger = structlog.get_logger(__name__)


class SkillBudgetError(RuntimeError):
    """Raised when a skill invocation exceeds its per-skill call budget."""

    def __init__(self, skill_name: str, limit: int) -> None:
        self.skill_name = skill_name
        self.limit = limit
        super().__init__(f"Skill '{skill_name}' exceeded its call budget of {limit} per run.")


class ArgumentError(ValueError):
    """Raised when required skill arguments are missing."""


class SkillExecutor:
    """Executes skill invocations within a nested RunContext scope.

    Pipeline per invocation:
      1. Validate required arguments against SkillDef.parameters.
      2. Check per-skill call budget (max_calls).
      3. Require a registered handler.
      4. Open child_scope (if inside a run_scope) or a fresh run_scope.
      5. Call handler with asyncio.wait_for (timeout = skill.timeout_seconds).
      6. Record call in budget counter.
      7. Emit SKILL_CALL RunEvent on the bus (if attached).
      8. Return SkillResult.

    Recoverable errors (missing handler, budget, timeout, handler exception)
    are encoded in SkillResult.error, not raised.
    """

    def __init__(self) -> None:
        self._call_counts: dict[str, int] = {}
        self._bus: Any = None

    def set_event_bus(self, bus: Any) -> None:
        self._bus = bus

    def reset(self) -> None:
        """Clear all per-run call counters (call at the start of each new run)."""
        self._call_counts.clear()

    def snapshot(self) -> SkillBudgetSnapshot:
        """Return an immutable copy of the current call-count state."""
        counts = dict(self._call_counts)
        return SkillBudgetSnapshot(calls_by_skill=counts, total_calls=sum(counts.values()))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_args(self, skill: SkillDef, arguments: dict[str, Any]) -> None:
        for param in skill.parameters:
            if param.required and param.name not in arguments and param.default is None:
                raise ArgumentError(
                    f"Skill '{skill.name}' requires argument '{param.name}' "
                    f"(type: {param.type.value}"
                    + (f", {param.description}" if param.description else "")
                    + ")"
                )

    def _check_budget(self, skill: SkillDef) -> None:
        count = self._call_counts.get(skill.name, 0)
        if count >= skill.max_calls:
            raise SkillBudgetError(skill.name, skill.max_calls)

    def _record(self, skill_name: str) -> None:
        self._call_counts[skill_name] = self._call_counts.get(skill_name, 0) + 1

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        skill: SkillDef,
        handler: SkillHandlerFn | None,
        invocation: SkillInvocation,
    ) -> SkillResult:
        """Run *skill* through the full pipeline. Never raises for handled errors."""
        t0 = time.monotonic()

        # 1–2. Validate + budget
        try:
            self._validate_args(skill, invocation.arguments)
            self._check_budget(skill)
        except (ArgumentError, SkillBudgetError) as exc:
            return SkillResult(skill_name=skill.name, error=str(exc))

        # 3. Handler must be registered
        if handler is None:
            return SkillResult(
                skill_name=skill.name,
                error=f"Skill '{skill.name}' has no registered handler.",
            )

        # 4–5. Execute inside a nested run scope
        active_run = current_run()
        try:
            if active_run is not None:
                with child_scope(agent_id=f"skill:{skill.name}"):
                    output = await asyncio.wait_for(
                        handler(invocation.arguments),
                        timeout=skill.timeout_seconds,
                    )
            else:
                ctx = RunContext(
                    run_id=invocation.run_id or uuid4().hex[:16],
                    task_id=invocation.skill_name,
                    agent_id=f"skill:{skill.name}",
                )
                with run_scope(ctx):
                    output = await asyncio.wait_for(
                        handler(invocation.arguments),
                        timeout=skill.timeout_seconds,
                    )
        except TimeoutError:
            return SkillResult(
                skill_name=skill.name,
                error=f"Skill '{skill.name}' timed out after {skill.timeout_seconds}s.",
            )
        except Exception as exc:
            logger.error("skills.executor.error", skill=skill.name, error=repr(exc))
            return SkillResult(skill_name=skill.name, error=str(exc))

        # 6. Record usage
        self._record(skill.name)
        duration_ms = (time.monotonic() - t0) * 1000

        # 7. Emit event
        if self._bus is not None:
            await self._bus.publish(
                RunEvent(
                    kind=RunEventKind.SKILL_CALL,
                    run_id=invocation.run_id,
                    agent_id=f"skill:{skill.name}",
                    data={
                        "skill": skill.name,
                        "duration_ms": round(duration_ms, 1),
                    },
                )
            )

        logger.info(
            "skills.executor.completed",
            skill=skill.name,
            duration_ms=round(duration_ms, 1),
        )

        return SkillResult(skill_name=skill.name, output=output, duration_ms=duration_ms)
