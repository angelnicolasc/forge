"""forge_rules.resolver — Conflict resolution with a fixed action lattice.

Inter-category lattice (non-configurable):
    deny > require > suggest

Intra-category strategies:
    MOST_SPECIFIC_WINS  — the rule with the longest (most-specific) scope pattern wins
    PRIORITY_FIRST_MATCH — the rule with the highest priority integer wins

The resolver operates on already-filtered rules (rules that matched scope + intent).
It returns the winning rules and the IDs of rules suppressed by deny actions.
"""

from __future__ import annotations

from forge_rules.types import IntraStrategy, Rule, RuleAction


_ACTION_RANK: dict[RuleAction, int] = {
    RuleAction.DENY: 2,
    RuleAction.REQUIRE: 1,
    RuleAction.SUGGEST: 0,
}


def _path_specificity(rule: Rule) -> int:
    """Heuristic: longer scope patterns are more specific.

    Rules with no scope (wildcard) score 0; each extra character adds specificity.
    The heuristic is not perfect for all glob edge cases but is stable and simple.
    """
    if not rule.scope:
        return 0
    return max(len(p) for p in rule.scope)


def resolve(
    rules: list[Rule],
    strategy: IntraStrategy = IntraStrategy.MOST_SPECIFIC_WINS,
) -> tuple[list[Rule], list[str]]:
    """Apply conflict resolution to a pre-filtered rule list.

    Returns:
        (winners, denied_ids) where:
          - winners: rules that survive all lattice + intra-strategy resolution
          - denied_ids: rule ids suppressed by a DENY rule in the same category scope
    """
    if not rules:
        return [], []

    # Step 1: if any DENY rule is present, suppress all lower-rank rules with
    # overlapping scope. We collect deny winners first.
    deny_rules = [r for r in rules if r.action == RuleAction.DENY]
    require_rules = [r for r in rules if r.action == RuleAction.REQUIRE]
    suggest_rules = [r for r in rules if r.action == RuleAction.SUGGEST]

    denied_ids: list[str] = []

    # Deny rules beat require + suggest unconditionally (inter-category lattice).
    if deny_rules:
        deny_winner = _pick_winner(deny_rules, strategy)
        denied_ids = [r.id for r in require_rules + suggest_rules]
        return [deny_winner], denied_ids

    # No deny: resolve require and suggest independently.
    winners: list[Rule] = []
    if require_rules:
        winners.append(_pick_winner(require_rules, strategy))
    if suggest_rules:
        winners.append(_pick_winner(suggest_rules, strategy))

    return winners, denied_ids


def _pick_winner(rules: list[Rule], strategy: IntraStrategy) -> Rule:
    """Pick the single winning rule from a same-action group."""
    if len(rules) == 1:
        return rules[0]
    if strategy == IntraStrategy.MOST_SPECIFIC_WINS:
        return max(rules, key=_path_specificity)
    # PRIORITY_FIRST_MATCH: higher priority int wins; ties broken by list order
    return max(rules, key=lambda r: r.priority)
