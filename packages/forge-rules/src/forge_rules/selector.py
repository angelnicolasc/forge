"""forge_rules.selector — Rule selection by path and intent.

Given a RulePack, a target path, and an optional intent label, the selector
returns the subset of rules that apply to the context.

Selection criteria (all must pass):
  1. rule.enabled == True
  2. scope: empty scope = wildcard (always matches); non-empty = at least one
     glob pattern matches the target path.
  3. intent_tags: empty = always matches; non-empty = intent label must be in the list.
"""

from __future__ import annotations

import fnmatch

import structlog

from forge_rules.types import IntraStrategy, Rule, RulePack, RuleAction

logger = structlog.get_logger(__name__)


def _scope_matches(rule: Rule, path: str) -> bool:
    if not rule.scope:
        return True
    return any(fnmatch.fnmatch(path, pat) for pat in rule.scope)


def _intent_matches(rule: Rule, intent: str) -> bool:
    if not rule.intent_tags:
        return True
    return intent in rule.intent_tags


def select_rules(
    pack: RulePack,
    *,
    path: str = "",
    intent: str = "",
) -> list[Rule]:
    """Return enabled rules from *pack* that match *path* and *intent*.

    No conflict resolution is applied here — the raw matched set is returned.
    Pass the result to :func:`forge_rules.resolver.resolve` to get the winners.
    """
    matched: list[Rule] = []
    for rule in pack.rules:
        if not rule.enabled:
            continue
        if not _scope_matches(rule, path):
            continue
        if not _intent_matches(rule, intent):
            continue
        matched.append(rule)

    logger.debug(
        "rules.select",
        pack=pack.name,
        path=path,
        intent=intent,
        matched=[r.id for r in matched],
    )
    return matched


def group_by_action(rules: list[Rule]) -> dict[RuleAction, list[Rule]]:
    """Group rules by their action for per-action conflict resolution."""
    groups: dict[RuleAction, list[Rule]] = {a: [] for a in RuleAction}
    for rule in rules:
        groups[rule.action].append(rule)
    return groups
