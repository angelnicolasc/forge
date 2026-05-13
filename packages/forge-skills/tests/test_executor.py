"""Tests for forge_skills.executor — SkillExecutor pipeline."""

from __future__ import annotations

import asyncio

from forge_core.context import run_scope
from forge_core.types import RunContext, RunEvent, RunEventKind
from forge_skills.executor import SkillExecutor
from forge_skills.types import SkillDef, SkillInvocation, SkillParam


def _skill(
    name: str = "ping",
    params: list[SkillParam] | None = None,
    max_calls: int = 10,
    timeout: float = 5.0,
) -> SkillDef:
    return SkillDef(
        name=name,
        parameters=params or [],
        max_calls=max_calls,
        timeout_seconds=timeout,
    )


def _inv(tool: str = "ping", args: dict | None = None, run_id: str = "") -> SkillInvocation:
    return SkillInvocation(skill_name=tool, arguments=args or {}, run_id=run_id)


async def _ok_handler(args: dict) -> str:
    return f"ok:{args}"


async def _fail_handler(args: dict) -> str:
    raise RuntimeError("upstream error")


async def _slow_handler(args: dict) -> str:
    await asyncio.sleep(10)
    return "never"


class TestSkillExecutorHappyPath:
    def test_successful_execution(self) -> None:
        ex = SkillExecutor()
        result = asyncio.run(ex.execute(_skill(), _ok_handler, _inv()))
        assert result.ok
        assert "ok:" in str(result.output)
        assert result.skill_name == "ping"

    def test_duration_ms_populated(self) -> None:
        ex = SkillExecutor()
        result = asyncio.run(ex.execute(_skill(), _ok_handler, _inv()))
        assert result.duration_ms >= 0.0

    def test_executes_inside_existing_run_scope(self) -> None:
        ctx = RunContext(run_id="r1", task_id="t1")
        ex = SkillExecutor()

        async def _run() -> str:
            with run_scope(ctx):
                result = await ex.execute(_skill(), _ok_handler, _inv())
            return result.output

        output = asyncio.run(_run())
        assert "ok:" in output

    def test_executes_without_active_run_scope(self) -> None:
        ex = SkillExecutor()
        result = asyncio.run(ex.execute(_skill(), _ok_handler, _inv(run_id="r42")))
        assert result.ok


class TestSkillExecutorErrors:
    def test_missing_handler_returns_error(self) -> None:
        ex = SkillExecutor()
        result = asyncio.run(ex.execute(_skill(), None, _inv()))
        assert not result.ok
        assert "no registered handler" in result.error.lower()

    def test_handler_exception_returns_error(self) -> None:
        ex = SkillExecutor()
        result = asyncio.run(ex.execute(_skill(), _fail_handler, _inv()))
        assert not result.ok
        assert "upstream error" in result.error

    def test_timeout_returns_error(self) -> None:
        ex = SkillExecutor()
        skill = _skill(timeout=0.05)
        result = asyncio.run(ex.execute(skill, _slow_handler, _inv()))
        assert not result.ok
        assert "timed out" in result.error.lower()


class TestSkillExecutorArgumentValidation:
    def test_missing_required_arg_returns_error(self) -> None:
        skill = _skill(params=[SkillParam(name="text", required=True)])
        ex = SkillExecutor()
        result = asyncio.run(ex.execute(skill, _ok_handler, _inv(args={})))
        assert not result.ok
        assert "text" in result.error

    def test_optional_arg_not_required(self) -> None:
        skill = _skill(params=[SkillParam(name="limit", required=False, default=10)])
        ex = SkillExecutor()
        result = asyncio.run(ex.execute(skill, _ok_handler, _inv(args={})))
        assert result.ok

    def test_required_arg_provided_succeeds(self) -> None:
        skill = _skill(params=[SkillParam(name="text", required=True)])
        ex = SkillExecutor()
        result = asyncio.run(ex.execute(skill, _ok_handler, _inv(args={"text": "hello"})))
        assert result.ok


class TestSkillExecutorBudget:
    def test_budget_exceeded_returns_error(self) -> None:
        skill = _skill(max_calls=2)
        ex = SkillExecutor()
        asyncio.run(ex.execute(skill, _ok_handler, _inv()))
        asyncio.run(ex.execute(skill, _ok_handler, _inv()))
        result = asyncio.run(ex.execute(skill, _ok_handler, _inv()))
        assert not result.ok
        assert "budget" in result.error.lower() or "limit" in result.error.lower()

    def test_budget_snapshot_counts_calls(self) -> None:
        skill = _skill(max_calls=10)
        ex = SkillExecutor()
        asyncio.run(ex.execute(skill, _ok_handler, _inv()))
        asyncio.run(ex.execute(skill, _ok_handler, _inv()))
        snap = ex.snapshot()
        assert snap.calls_by_skill.get("ping") == 2
        assert snap.total_calls == 2

    def test_reset_clears_budget(self) -> None:
        skill = _skill(max_calls=1)
        ex = SkillExecutor()
        asyncio.run(ex.execute(skill, _ok_handler, _inv()))
        ex.reset()
        result = asyncio.run(ex.execute(skill, _ok_handler, _inv()))
        assert result.ok

    def test_different_skills_independent_budgets(self) -> None:
        skill_a = _skill("skill-a", max_calls=1)
        skill_b = _skill("skill-b", max_calls=10)
        ex = SkillExecutor()
        asyncio.run(ex.execute(skill_a, _ok_handler, _inv("skill-a")))
        over = asyncio.run(ex.execute(skill_a, _ok_handler, _inv("skill-a")))
        ok = asyncio.run(ex.execute(skill_b, _ok_handler, _inv("skill-b")))
        assert not over.ok
        assert ok.ok


class TestSkillExecutorEventBus:
    def test_skill_call_event_emitted(self) -> None:
        published: list[RunEvent] = []

        class _Bus:
            async def publish(self, event: RunEvent) -> None:
                published.append(event)

        ex = SkillExecutor()
        ex.set_event_bus(_Bus())
        asyncio.run(ex.execute(_skill(), _ok_handler, _inv(run_id="run-99")))

        assert len(published) == 1
        assert published[0].kind == RunEventKind.SKILL_CALL
        assert published[0].data.get("skill") == "ping"

    def test_no_event_on_error(self) -> None:
        published: list[RunEvent] = []

        class _Bus:
            async def publish(self, event: RunEvent) -> None:
                published.append(event)

        ex = SkillExecutor()
        ex.set_event_bus(_Bus())
        asyncio.run(ex.execute(_skill(), _fail_handler, _inv()))
        assert len(published) == 0

    def test_no_event_on_budget_exceeded(self) -> None:
        published: list[RunEvent] = []

        class _Bus:
            async def publish(self, event: RunEvent) -> None:
                published.append(event)

        skill = _skill(max_calls=1)
        ex = SkillExecutor()
        ex.set_event_bus(_Bus())
        asyncio.run(ex.execute(skill, _ok_handler, _inv()))  # success → event
        asyncio.run(ex.execute(skill, _ok_handler, _inv()))  # budget → no event
        assert len(published) == 1
