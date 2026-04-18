"""Symbolic rule engine for Forge Memory.

A lightweight forward-chaining rule engine (~200 lines) that lets users
express deterministic business constraints over memory entries and
agent behaviour.

Rules fire after vector and graph retrieval and can:
  - VETO: remove an entry from results (hard constraint)
  - BOOST: increase an entry's score (soft preference)
  - DEMOTE: decrease an entry's score (soft penalty)
  - REQUIRE: assert that a result must be present (raises if not)
  - TAG: annotate an entry with metadata

Rules are evaluated in priority order (higher = first). First matching
rule in a conflict group wins.

Example DSL (Python):
    engine = RuleEngine()

    @engine.rule(priority=100, description="Never use GPT-4o for summarization")
    def no_gpt4o_summarize(entry: MemoryEntry, context: dict) -> RuleAction | None:
        if context.get("task_type") == "summarize" and "gpt-4o" in entry.content:
            return RuleAction.veto("GPT-4o blocked for summarization tasks")
        return None

    @engine.rule(priority=50, description="Boost compliance-reviewed entries")
    def boost_compliance(entry: MemoryEntry, context: dict) -> RuleAction | None:
        if entry.tags.get("compliance_reviewed") == "true":
            return RuleAction.boost(0.3, "Compliance-reviewed entry gets priority boost")
        return None
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from enum import StrEnum
from typing import Any

import structlog

from forge_core.types import MemoryEntry, MemoryQuery

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Rule action types
# ---------------------------------------------------------------------------


class RuleActionKind(StrEnum):
    VETO = "veto"
    BOOST = "boost"
    DEMOTE = "demote"
    TAG = "tag"


@dataclass(frozen=True)
class RuleAction:
    """The outcome of a fired rule."""

    kind: RuleActionKind
    reason: str = ""
    score_delta: float = 0.0
    tags: dict[str, str] = field(default_factory=dict)

    @classmethod
    def veto(cls, reason: str = "") -> RuleAction:
        return cls(kind=RuleActionKind.VETO, reason=reason)

    @classmethod
    def boost(cls, delta: float, reason: str = "") -> RuleAction:
        return cls(kind=RuleActionKind.BOOST, score_delta=abs(delta), reason=reason)

    @classmethod
    def demote(cls, delta: float, reason: str = "") -> RuleAction:
        return cls(kind=RuleActionKind.DEMOTE, score_delta=-abs(delta), reason=reason)

    @classmethod
    def tag(cls, **tags: str) -> RuleAction:
        return cls(kind=RuleActionKind.TAG, tags=tags)


# ---------------------------------------------------------------------------
# Rule definition
# ---------------------------------------------------------------------------

RuleFn = Callable[[MemoryEntry, dict[str, Any]], RuleAction | None]


@dataclass
class Rule:
    """A single symbolic rule."""

    fn: RuleFn
    priority: int = 0
    description: str = ""
    enabled: bool = True
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.fn.__name__

    def evaluate(self, entry: MemoryEntry, context: dict[str, Any]) -> RuleAction | None:
        """Evaluate this rule against an entry. Returns action or None."""
        if not self.enabled:
            return None
        try:
            return self.fn(entry, context)
        except Exception as exc:
            logger.warning("rule.evaluation_error", rule=self.name, error=str(exc))
            return None


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------


class RuleEngine:
    """Forward-chaining rule engine for Forge Memory.

    Applies registered rules to a list of (MemoryEntry, score) pairs
    and returns the filtered, reranked list.
    """

    backend_type = "symbolic"

    def __init__(self) -> None:
        self._rules: list[Rule] = []

    def rule(
        self,
        priority: int = 0,
        description: str = "",
        enabled: bool = True,
    ) -> Callable[[RuleFn], RuleFn]:
        """Decorator to register a rule function."""

        def decorator(fn: RuleFn) -> RuleFn:
            self._rules.append(
                Rule(fn=fn, priority=priority, description=description, enabled=enabled)
            )
            # Keep sorted by priority (highest first)
            self._rules.sort(key=lambda r: r.priority, reverse=True)
            logger.debug("rule.registered", name=fn.__name__, priority=priority)

            @functools.wraps(fn)
            def wrapper(entry: MemoryEntry, context: dict[str, Any]) -> RuleAction | None:
                return fn(entry, context)

            return wrapper

        return decorator

    def add_rule(self, rule: Rule) -> None:
        """Register a pre-built Rule instance."""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority, reverse=True)

    def apply(
        self,
        entries: list[tuple[MemoryEntry, float]],
        context: dict[str, Any] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        """Apply all rules to a list of (entry, score) pairs.

        Returns filtered and reranked list. Vetoed entries are removed.
        Score deltas from BOOST/DEMOTE are applied additively.
        """
        ctx = context or {}
        results: list[tuple[MemoryEntry, float]] = []

        for entry, score in entries:
            new_score = score
            vetoed = False
            modified_entry = entry

            for rule in self._rules:
                action = rule.evaluate(entry, ctx)
                if action is None:
                    continue

                if action.kind == RuleActionKind.VETO:
                    logger.debug(
                        "rule.veto", entry_id=entry.id, rule=rule.name, reason=action.reason
                    )
                    vetoed = True
                    break
                elif action.kind in (RuleActionKind.BOOST, RuleActionKind.DEMOTE):
                    new_score = max(0.0, min(1.0, new_score + action.score_delta))
                    logger.debug(
                        "rule.score_adjust",
                        entry_id=entry.id,
                        rule=rule.name,
                        delta=action.score_delta,
                        new_score=new_score,
                    )
                elif action.kind == RuleActionKind.TAG:
                    # Create a new entry with merged tags (entries are Pydantic models)
                    merged_tags = {**entry.tags, **action.tags}
                    modified_entry = entry.model_copy(update={"tags": merged_tags})

            if not vetoed:
                results.append((modified_entry, new_score))

        # Re-sort after score adjustments
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    async def store(self, entry: MemoryEntry) -> str:
        """No-op store (rules are stateless). Returns entry ID."""
        return entry.id

    async def query(self, q: MemoryQuery) -> list[tuple[MemoryEntry, float]]:
        """Rules don't actively retrieve — they filter. Returns empty list."""
        return []

    async def delete(self, entry_id: str) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    @property
    def rules(self) -> list[Rule]:
        """List of registered rules (sorted by priority)."""
        return list(self._rules)

    def disable_rule(self, name: str) -> bool:
        """Disable a rule by name. Returns True if found."""
        for rule in self._rules:
            if rule.name == name:
                rule.enabled = False
                return True
        return False

    def enable_rule(self, name: str) -> bool:
        """Re-enable a disabled rule. Returns True if found."""
        for rule in self._rules:
            if rule.name == name:
                rule.enabled = True
                return True
        return False


# ---------------------------------------------------------------------------
# Built-in rules (always registered by default)
# ---------------------------------------------------------------------------


def _build_default_engine() -> RuleEngine:
    engine = RuleEngine()

    @engine.rule(priority=1000, description="Block expired entries unless explicitly requested")
    def block_expired(entry: MemoryEntry, context: dict[str, Any]) -> RuleAction | None:
        from datetime import datetime

        if (
            entry.valid_until
            and entry.valid_until < datetime.now(UTC)
            and not context.get("include_expired", False)
        ):
            return RuleAction.veto(f"Entry expired at {entry.valid_until}")
        return None

    @engine.rule(priority=100, description="Boost entries tagged as verified")
    def boost_verified(entry: MemoryEntry, context: dict[str, Any]) -> RuleAction | None:
        if entry.tags.get("verified") == "true":
            return RuleAction.boost(0.1, "Verified entry boost")
        return None

    return engine


default_rule_engine = _build_default_engine()
