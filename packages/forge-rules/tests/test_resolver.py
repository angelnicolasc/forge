"""Tests for forge_rules.resolver — conflict resolution lattice."""

from __future__ import annotations

from forge_rules.resolver import _path_specificity, _pick_winner, resolve
from forge_rules.types import IntraStrategy, Rule, RuleAction


def _rule(
    id: str,
    action: RuleAction = RuleAction.SUGGEST,
    scope: list[str] | None = None,
    priority: int = 0,
) -> Rule:
    return Rule(
        id=id,
        action=action,
        description=f"Rule {id}",
        content=f"content of {id}",
        scope=scope or [],
        priority=priority,
    )


class TestPathSpecificity:
    def test_no_scope_is_zero(self) -> None:
        assert _path_specificity(_rule("r", scope=[])) == 0

    def test_longer_pattern_wins(self) -> None:
        short = _rule("a", scope=["*.py"])
        long_ = _rule("b", scope=["src/core/*.py"])
        assert _path_specificity(long_) > _path_specificity(short)

    def test_multiple_patterns_uses_max(self) -> None:
        r = _rule("x", scope=["*.py", "src/core/models/*.py"])
        assert _path_specificity(r) == len("src/core/models/*.py")


class TestPickWinner:
    def test_single_rule_always_wins(self) -> None:
        r = _rule("only")
        assert _pick_winner([r], IntraStrategy.MOST_SPECIFIC_WINS) is r

    def test_most_specific_wins_picks_longer_scope(self) -> None:
        broad = _rule("broad", scope=["*.py"])
        narrow = _rule("narrow", scope=["src/core/*.py"])
        winner = _pick_winner([broad, narrow], IntraStrategy.MOST_SPECIFIC_WINS)
        assert winner is narrow

    def test_priority_first_match_picks_higher_priority(self) -> None:
        low = _rule("low", priority=1)
        high = _rule("high", priority=10)
        winner = _pick_winner([low, high], IntraStrategy.PRIORITY_FIRST_MATCH)
        assert winner is high

    def test_priority_first_match_tie_uses_list_order(self) -> None:
        first = _rule("first", priority=5)
        second = _rule("second", priority=5)
        winner = _pick_winner([first, second], IntraStrategy.PRIORITY_FIRST_MATCH)
        assert winner is first


class TestResolve:
    def test_empty_returns_empty(self) -> None:
        assert resolve([]) == ([], [])

    def test_single_suggest_passes_through(self) -> None:
        r = _rule("s", action=RuleAction.SUGGEST)
        winners, denied = resolve([r])
        assert winners == [r]
        assert denied == []

    def test_deny_suppresses_require_and_suggest(self) -> None:
        deny_r = _rule("d", action=RuleAction.DENY)
        require_r = _rule("req", action=RuleAction.REQUIRE)
        suggest_r = _rule("sug", action=RuleAction.SUGGEST)
        winners, denied = resolve([deny_r, require_r, suggest_r])
        assert len(winners) == 1
        assert winners[0].id == "d"
        assert set(denied) == {"req", "sug"}

    def test_require_and_suggest_coexist_no_deny(self) -> None:
        req = _rule("req", action=RuleAction.REQUIRE)
        sug = _rule("sug", action=RuleAction.SUGGEST)
        winners, denied = resolve([req, sug])
        winner_ids = {r.id for r in winners}
        assert "req" in winner_ids
        assert "sug" in winner_ids
        assert denied == []

    def test_intra_category_most_specific(self) -> None:
        broad = _rule("broad", action=RuleAction.SUGGEST, scope=["*.py"])
        narrow = _rule("narrow", action=RuleAction.SUGGEST, scope=["src/core/*.py"])
        winners, _ = resolve([broad, narrow], IntraStrategy.MOST_SPECIFIC_WINS)
        assert len(winners) == 1
        assert winners[0].id == "narrow"

    def test_intra_category_priority(self) -> None:
        low = _rule("low", action=RuleAction.REQUIRE, priority=1)
        high = _rule("high", action=RuleAction.REQUIRE, priority=99)
        winners, _ = resolve([low, high], IntraStrategy.PRIORITY_FIRST_MATCH)
        assert len(winners) == 1
        assert winners[0].id == "high"

    def test_multiple_deny_resolves_one_winner(self) -> None:
        d1 = _rule("d1", action=RuleAction.DENY, scope=["*.py"])
        d2 = _rule("d2", action=RuleAction.DENY, scope=["src/core/*.py"])
        winners, _ = resolve([d1, d2], IntraStrategy.MOST_SPECIFIC_WINS)
        assert len(winners) == 1
        assert winners[0].id == "d2"
