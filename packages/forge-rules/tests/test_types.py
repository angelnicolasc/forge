"""Tests for forge_rules.types — core type system."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge_rules.types import (
    IntraStrategy,
    Rule,
    RuleAction,
    RulePack,
    RuleSelection,
    ScopeIntersection,
)


class TestRule:
    def test_defaults(self) -> None:
        r = Rule(id="r1", action=RuleAction.SUGGEST, description="desc", content="content")
        assert r.scope == []
        assert r.intent_tags == []
        assert r.priority == 0
        assert r.enabled is True

    def test_id_no_spaces(self) -> None:
        with pytest.raises(ValidationError):
            Rule(id="bad id", action=RuleAction.SUGGEST, description="d", content="c")

    def test_all_actions(self) -> None:
        for action in RuleAction:
            r = Rule(id="r", action=action, description="d", content="c")
            assert r.action == action


class TestRulePack:
    def test_defaults(self) -> None:
        pack = RulePack(name="test")
        assert pack.version == "0.1.0"
        assert pack.rules == []
        assert pack.intra_strategy == IntraStrategy.MOST_SPECIFIC_WINS

    def test_duplicate_ids_raise(self) -> None:
        rules = [
            Rule(id="dup", action=RuleAction.SUGGEST, description="a", content="a"),
            Rule(id="dup", action=RuleAction.DENY, description="b", content="b"),
        ]
        with pytest.raises(ValidationError):
            RulePack(name="test", rules=rules)

    def test_unique_ids_ok(self) -> None:
        rules = [
            Rule(id="r1", action=RuleAction.SUGGEST, description="a", content="a"),
            Rule(id="r2", action=RuleAction.DENY, description="b", content="b"),
        ]
        pack = RulePack(name="test", rules=rules)
        assert len(pack.rules) == 2


class TestRuleSelection:
    def test_empty_selection_empty_context(self) -> None:
        sel = RuleSelection()
        assert sel.context_text() == ""

    def test_context_text_wraps_content(self) -> None:
        r = Rule(id="r", action=RuleAction.REQUIRE, description="Use typing", content="Add types.")
        sel = RuleSelection(rules=[r])
        ctx = sel.context_text()
        assert "<forge:rules>" in ctx
        assert "</forge:rules>" in ctx
        assert "[REQUIRE]" in ctx
        assert "Add types." in ctx

    def test_context_text_multiple_rules(self) -> None:
        rules = [
            Rule(id="a", action=RuleAction.DENY, description="A", content="Content A"),
            Rule(id="b", action=RuleAction.SUGGEST, description="B", content="Content B"),
        ]
        sel = RuleSelection(rules=rules)
        ctx = sel.context_text()
        assert "Content A" in ctx
        assert "Content B" in ctx


class TestScopeIntersection:
    def test_basic_fields(self) -> None:
        ix = ScopeIntersection(
            rule_a="r1",
            rule_b="r2",
            pattern_a="src/**/*.py",
            pattern_b="src/core/*.py",
            example_path="src/core/main.py",
        )
        assert ix.rule_a == "r1"
        assert ix.example_path == "src/core/main.py"


class TestIntraStrategy:
    def test_strategy_values(self) -> None:
        assert IntraStrategy.MOST_SPECIFIC_WINS == "most_specific_wins"
        assert IntraStrategy.PRIORITY_FIRST_MATCH == "priority_first_match"
