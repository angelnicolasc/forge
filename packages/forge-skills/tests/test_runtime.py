"""Tests for forge_skills.runtime — SkillRuntime full integration."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from forge_core.types import RunEvent, RunEventKind
from forge_skills.runtime import SkillRuntime
from forge_skills.types import SkillDef, SkillParam

from .conftest import VALID_SKILL_MD

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fake classifier (same pattern as test_dispatcher.py)
# ---------------------------------------------------------------------------


class _FakeClassifier:
    def __init__(self, responses: dict[str, tuple[str, float]]) -> None:
        self._responses = responses

    def register_intent(self, label: str, examples: list[str]) -> None:
        pass

    async def classify(self, text: str) -> tuple[str, float]:
        return self._responses.get(text, ("unknown", 0.0))


def _inject_fake_classifier(rt: SkillRuntime, responses: dict[str, tuple[str, float]]) -> None:
    rt._dispatcher._classifier = _FakeClassifier(responses)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _greet_handler(args: dict) -> str:
    return f"Hello, {args.get('user', 'World')}!"


async def _add_handler(args: dict) -> int:
    return int(args["a"]) + int(args["b"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSkillRuntimeRegistration:
    def test_register_and_list(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="ping"))
        assert "ping" in rt.list_skills()

    def test_unregister_removes_skill(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="ping"))
        assert rt.unregister("ping") is True
        assert "ping" not in rt.list_skills()

    def test_get_skill_returns_none_when_missing(self) -> None:
        rt = SkillRuntime()
        assert rt.get_skill("ghost") is None

    def test_get_skill_returns_def(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="ping"))
        assert rt.get_skill("ping") is not None

    def test_load_md_registers_skill(self) -> None:
        rt = SkillRuntime()
        skill = rt.load_md(VALID_SKILL_MD)
        assert skill.name in rt.list_skills()

    def test_load_yaml_registers_skill(self) -> None:
        rt = SkillRuntime()
        skill = rt.load_yaml("name: quicktest\ndescription: A test skill\n")
        assert skill.name in rt.list_skills()

    def test_load_dir_registers_skills(self, tmp_path: Path) -> None:
        for name in ("alpha", "beta"):
            (tmp_path / f"{name}.md").write_text(
                f"---\nname: {name}\ndescription: skill {name}\n---\n"
            )
        rt = SkillRuntime()
        skills = rt.load_dir(tmp_path)
        assert len(skills) == 2
        assert set(rt.list_skills()) == {"alpha", "beta"}

    def test_skills_with_tag(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="sum", tags=["nlp"]))
        rt.register(SkillDef(name="greet", tags=["social"]))
        nlp = [s.name for s in rt.skills_with_tag("nlp")]
        assert nlp == ["sum"]


class TestSkillRuntimeInvoke:
    def test_invoke_by_name(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="greet"), handler=_greet_handler)
        result = asyncio.run(rt.invoke("greet", arguments={"user": "Alice"}))
        assert result.ok
        assert result.output == "Hello, Alice!"

    def test_invoke_unknown_skill_returns_error(self) -> None:
        rt = SkillRuntime()
        result = asyncio.run(rt.invoke("ghost"))
        assert not result.ok
        assert "not registered" in result.error.lower()

    def test_invoke_no_handler_returns_error(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="bare"))
        result = asyncio.run(rt.invoke("bare"))
        assert not result.ok
        assert "no registered handler" in result.error.lower()

    def test_invoke_with_required_args(self) -> None:
        rt = SkillRuntime()
        rt.register(
            SkillDef(name="add", parameters=[SkillParam(name="a"), SkillParam(name="b")]),
            handler=_add_handler,
        )
        result = asyncio.run(rt.invoke("add", arguments={"a": 2, "b": 3}))
        assert result.ok
        assert result.output == 5

    def test_invoke_missing_required_arg_returns_error(self) -> None:
        rt = SkillRuntime()
        rt.register(
            SkillDef(name="add", parameters=[SkillParam(name="a")]),
            handler=_add_handler,
        )
        result = asyncio.run(rt.invoke("add", arguments={}))
        assert not result.ok


class TestSkillRuntimeDispatchText:
    def test_dispatch_to_known_skill(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="greet", examples=["greet user"]), handler=_greet_handler)
        _inject_fake_classifier(rt, {"greet user": ("greet", 0.9)})
        result = asyncio.run(rt.dispatch_text("greet user"))
        assert result.ok

    def test_dispatch_unknown_returns_error(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="greet"))
        _inject_fake_classifier(rt, {})  # all → ("unknown", 0.0)
        result = asyncio.run(rt.dispatch_text("random text"))
        assert not result.ok
        assert result.skill_name == "unknown"

    def test_dispatch_passes_arguments(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="greet", examples=["say hi"]), handler=_greet_handler)
        _inject_fake_classifier(rt, {"say hi": ("greet", 0.8)})
        result = asyncio.run(rt.dispatch_text("say hi", arguments={"user": "Bob"}))
        assert result.ok
        assert "Bob" in result.output


class TestSkillRuntimeBudget:
    def test_budget_snapshot_tracks_calls(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="ping", max_calls=10), handler=_greet_handler)
        asyncio.run(rt.invoke("ping"))
        asyncio.run(rt.invoke("ping"))
        snap = rt.budget_snapshot()
        assert snap.calls_by_skill.get("ping") == 2
        assert snap.total_calls == 2

    def test_reset_budget_clears_counts(self) -> None:
        rt = SkillRuntime()
        rt.register(SkillDef(name="ping", max_calls=1), handler=_greet_handler)
        asyncio.run(rt.invoke("ping"))
        rt.reset_budget()
        result = asyncio.run(rt.invoke("ping"))
        assert result.ok


class TestSkillRuntimeEventBus:
    def test_event_emitted_after_successful_invoke(self) -> None:
        published: list[RunEvent] = []

        class _Bus:
            async def publish(self, event: RunEvent) -> None:
                published.append(event)

        rt = SkillRuntime()
        rt.set_event_bus(_Bus())
        rt.register(SkillDef(name="ping"), handler=_greet_handler)
        asyncio.run(rt.invoke("ping", run_id="run-1"))

        assert len(published) == 1
        assert published[0].kind == RunEventKind.SKILL_CALL

    def test_no_event_on_unknown_skill(self) -> None:
        published: list[RunEvent] = []

        class _Bus:
            async def publish(self, event: RunEvent) -> None:
                published.append(event)

        rt = SkillRuntime()
        rt.set_event_bus(_Bus())
        asyncio.run(rt.invoke("ghost"))
        assert len(published) == 0
