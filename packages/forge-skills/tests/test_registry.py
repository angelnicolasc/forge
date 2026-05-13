"""Tests for forge_skills.registry — SkillRegistry."""

from __future__ import annotations

import pytest

from forge_skills.registry import SkillNotFoundError, SkillRegistry
from forge_skills.types import SkillDef, SkillParam


def _skill(name: str, tags: list[str] | None = None) -> SkillDef:
    return SkillDef(name=name, description=f"skill {name}", tags=tags or [])


async def _handler(args: dict) -> str:
    return "ok"


class TestSkillRegistry:
    def test_register_and_get(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("ping"))
        skill = reg.get("ping")
        assert skill.name == "ping"

    def test_get_missing_raises(self) -> None:
        reg = SkillRegistry()
        with pytest.raises(SkillNotFoundError):
            reg.get("ghost")

    def test_find_returns_none_when_missing(self) -> None:
        reg = SkillRegistry()
        assert reg.find("ghost") is None

    def test_find_returns_skill_when_present(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("ping"))
        assert reg.find("ping") is not None

    def test_register_with_handler(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("ping"), _handler)
        assert reg.get_handler("ping") is _handler

    def test_register_without_handler(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("ping"))
        assert reg.get_handler("ping") is None

    def test_overwrite_clears_old_handler(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("ping"), _handler)
        reg.register(_skill("ping"))          # re-register without handler
        assert reg.get_handler("ping") is None

    def test_overwrite_replaces_definition(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("ping"))
        updated = SkillDef(name="ping", description="updated", tags=["util"])
        reg.register(updated)
        assert reg.get("ping").description == "updated"

    def test_unregister_returns_true(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("ping"))
        assert reg.unregister("ping") is True

    def test_unregister_removes_skill(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("ping"))
        reg.unregister("ping")
        assert reg.find("ping") is None

    def test_unregister_removes_handler(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("ping"), _handler)
        reg.unregister("ping")
        assert reg.get_handler("ping") is None

    def test_unregister_missing_returns_false(self) -> None:
        reg = SkillRegistry()
        assert reg.unregister("ghost") is False

    def test_all_names_sorted(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("zebra"))
        reg.register(_skill("alpha"))
        reg.register(_skill("middle"))
        assert reg.all_names() == ["alpha", "middle", "zebra"]

    def test_all_skills_sorted(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("b"))
        reg.register(_skill("a"))
        names = [s.name for s in reg.all_skills()]
        assert names == ["a", "b"]

    def test_skills_with_tag(self) -> None:
        reg = SkillRegistry()
        reg.register(_skill("summarize", tags=["nlp", "text"]))
        reg.register(_skill("greet", tags=["social"]))
        reg.register(_skill("translate", tags=["nlp"]))
        nlp = {s.name for s in reg.skills_with_tag("nlp")}
        assert nlp == {"summarize", "translate"}
        assert reg.skills_with_tag("social") == [reg.find("greet")]

    def test_skill_count(self) -> None:
        reg = SkillRegistry()
        assert reg.skill_count() == 0
        reg.register(_skill("a"))
        reg.register(_skill("b"))
        assert reg.skill_count() == 2
        reg.unregister("a")
        assert reg.skill_count() == 1

    def test_multiple_handlers_independent(self) -> None:
        reg = SkillRegistry()
        async def h1(args: dict) -> str: return "h1"
        async def h2(args: dict) -> str: return "h2"
        reg.register(_skill("skill-a"), h1)
        reg.register(_skill("skill-b"), h2)
        assert reg.get_handler("skill-a") is h1
        assert reg.get_handler("skill-b") is h2
