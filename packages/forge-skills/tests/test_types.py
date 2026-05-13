"""Tests for forge_skills.types — SkillDef, SkillParam, SkillResult."""

from __future__ import annotations

import pytest

from forge_skills.types import (
    ParamType,
    SkillBudgetSnapshot,
    SkillDef,
    SkillParam,
    SkillResult,
)


class TestSkillParam:
    def test_defaults(self) -> None:
        p = SkillParam(name="x")
        assert p.type == ParamType.STRING
        assert p.required is True
        assert p.default is None
        assert p.description == ""

    def test_optional_param(self) -> None:
        p = SkillParam(name="limit", type=ParamType.INT, required=False, default=10)
        assert p.required is False
        assert p.default == 10

    def test_all_param_types(self) -> None:
        for pt in ParamType:
            p = SkillParam(name="x", type=pt)
            assert p.type == pt


class TestSkillDef:
    def test_defaults(self) -> None:
        s = SkillDef(name="my-skill")
        assert s.description == ""
        assert s.examples == []
        assert s.parameters == []
        assert s.tools == []
        assert s.timeout_seconds == 30.0
        assert s.max_calls == 20
        assert s.tags == []
        assert s.body == ""
        assert s.source == ""

    def test_name_with_spaces_raises(self) -> None:
        with pytest.raises(ValueError, match="spaces"):
            SkillDef(name="my skill")

    def test_name_with_hyphens_ok(self) -> None:
        s = SkillDef(name="my-skill")
        assert s.name == "my-skill"

    def test_name_with_underscores_ok(self) -> None:
        s = SkillDef(name="my_skill")
        assert s.name == "my_skill"

    def test_full_construction(self) -> None:
        s = SkillDef(
            name="summarize",
            description="Summarize text",
            examples=["summarize this"],
            parameters=[SkillParam(name="text")],
            timeout_seconds=60.0,
            max_calls=10,
            tags=["nlp"],
            body="Summarize: {text}",
            source="skills/summarize.md",
        )
        assert s.name == "summarize"
        assert s.timeout_seconds == 60.0
        assert len(s.parameters) == 1

    def test_roundtrip_model_dump(self) -> None:
        s = SkillDef(name="ping", tags=["util"])
        d = s.model_dump()
        s2 = SkillDef(**d)
        assert s2.name == s.name
        assert s2.tags == s.tags


class TestSkillResult:
    def test_ok_true_when_no_error(self) -> None:
        r = SkillResult(skill_name="ping", output="pong")
        assert r.ok is True

    def test_ok_false_when_error(self) -> None:
        r = SkillResult(skill_name="ping", error="timeout")
        assert r.ok is False
        assert r.output is None

    def test_defaults(self) -> None:
        r = SkillResult(skill_name="x")
        assert r.duration_ms == 0.0
        assert r.tokens_used == 0


class TestSkillBudgetSnapshot:
    def test_empty_snapshot(self) -> None:
        snap = SkillBudgetSnapshot()
        assert snap.total_calls == 0
        assert snap.calls_by_skill == {}

    def test_populated_snapshot(self) -> None:
        snap = SkillBudgetSnapshot(calls_by_skill={"ping": 3, "greet": 1}, total_calls=4)
        assert snap.calls_by_skill["ping"] == 3
        assert snap.total_calls == 4
