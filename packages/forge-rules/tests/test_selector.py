"""Tests for forge_rules.selector — path + intent selection."""

from __future__ import annotations

import pytest

from forge_rules.selector import _intent_matches, _scope_matches, group_by_action, select_rules
from forge_rules.types import Rule, RuleAction, RulePack


def _rule(
    id: str,
    scope: list[str] | None = None,
    intent_tags: list[str] | None = None,
    enabled: bool = True,
) -> Rule:
    return Rule(
        id=id,
        action=RuleAction.SUGGEST,
        description=f"Rule {id}",
        content=f"content of {id}",
        scope=scope or [],
        intent_tags=intent_tags or [],
        enabled=enabled,
    )


def _pack(rules: list[Rule]) -> RulePack:
    return RulePack(name="test-pack", rules=rules)


class TestScopeMatches:
    def test_empty_scope_always_matches(self) -> None:
        r = _rule("r", scope=[])
        assert _scope_matches(r, "src/any/path.py")
        assert _scope_matches(r, "")

    def test_glob_matches_correct_path(self) -> None:
        r = _rule("r", scope=["**/*.py"])
        assert _scope_matches(r, "src/main.py")
        assert not _scope_matches(r, "src/main.js")

    def test_multiple_patterns_or_semantics(self) -> None:
        r = _rule("r", scope=["**/*.py", "**/*.ts"])
        assert _scope_matches(r, "app/index.ts")
        assert _scope_matches(r, "app/index.py")
        assert not _scope_matches(r, "app/index.go")


class TestIntentMatches:
    def test_empty_intent_tags_always_matches(self) -> None:
        r = _rule("r", intent_tags=[])
        assert _intent_matches(r, "")
        assert _intent_matches(r, "code_review")

    def test_intent_in_tags_matches(self) -> None:
        r = _rule("r", intent_tags=["code_review", "security"])
        assert _intent_matches(r, "code_review")
        assert _intent_matches(r, "security")

    def test_intent_not_in_tags_no_match(self) -> None:
        r = _rule("r", intent_tags=["code_review"])
        assert not _intent_matches(r, "unknown")
        assert not _intent_matches(r, "")


class TestSelectRules:
    def test_disabled_rule_excluded(self) -> None:
        pack = _pack([_rule("disabled", enabled=False)])
        assert select_rules(pack) == []

    def test_wildcard_rule_always_selected(self) -> None:
        r = _rule("wildcard", scope=[], intent_tags=[])
        pack = _pack([r])
        assert select_rules(pack) == [r]

    def test_scope_filter(self) -> None:
        py_rule = _rule("py", scope=["**/*.py"])
        js_rule = _rule("js", scope=["**/*.js"])
        pack = _pack([py_rule, js_rule])
        result = select_rules(pack, path="src/main.py")
        assert py_rule in result
        assert js_rule not in result

    def test_intent_filter(self) -> None:
        review_rule = _rule("review", intent_tags=["code_review"])
        refactor_rule = _rule("refactor", intent_tags=["refactor"])
        pack = _pack([review_rule, refactor_rule])
        result = select_rules(pack, intent="code_review")
        assert review_rule in result
        assert refactor_rule not in result

    def test_scope_and_intent_combined(self) -> None:
        r = _rule("both", scope=["**/*.py"], intent_tags=["code_review"])
        pack = _pack([r])
        # both match
        assert select_rules(pack, path="src/main.py", intent="code_review") == [r]
        # scope mismatch
        assert select_rules(pack, path="src/main.js", intent="code_review") == []
        # intent mismatch
        assert select_rules(pack, path="src/main.py", intent="other") == []

    def test_empty_pack_returns_empty(self) -> None:
        pack = _pack([])
        assert select_rules(pack) == []


class TestGroupByAction:
    def test_groups_rules_by_action(self) -> None:
        r1 = _rule("a", enabled=True)
        r2 = Rule(id="b", action=RuleAction.DENY, description="b", content="deny it")
        result = group_by_action([r1, r2])
        assert r1 in result[RuleAction.SUGGEST]
        assert r2 in result[RuleAction.DENY]

    def test_empty_input_returns_all_actions_empty(self) -> None:
        result = group_by_action([])
        assert all(v == [] for v in result.values())
        assert set(result.keys()) == set(RuleAction)
