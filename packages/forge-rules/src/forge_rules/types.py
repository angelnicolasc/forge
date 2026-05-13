"""forge_rules.types — Core type system for the Rules Engine.

Conflict resolution semantics:
  Inter-category: deny > require > suggest  (fixed lattice, non-configurable)
  Intra-category: MOST_SPECIFIC_WINS (default) | PRIORITY_FIRST_MATCH (opt-in)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class RuleAction(StrEnum):
    DENY = "deny"
    REQUIRE = "require"
    SUGGEST = "suggest"


class IntraStrategy(StrEnum):
    MOST_SPECIFIC_WINS = "most_specific_wins"
    PRIORITY_FIRST_MATCH = "priority_first_match"


class Rule(BaseModel):
    """A single governance rule within a RulePack."""

    id: str
    action: RuleAction
    description: str
    content: str
    scope: list[str] = Field(
        default_factory=list,
        description="Glob patterns. Empty = applies everywhere.",
    )
    intent_tags: list[str] = Field(
        default_factory=list,
        description="Intent labels that trigger this rule (empty = always apply).",
    )
    priority: int = Field(
        default=0,
        description="Used only when IntraStrategy.PRIORITY_FIRST_MATCH is active.",
    )
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_id_no_spaces(self) -> "Rule":
        if " " in self.id:
            raise ValueError(f"Rule id must not contain spaces: {self.id!r}")
        return self


class RulePack(BaseModel):
    """A named, versioned collection of rules."""

    name: str
    version: str = "0.1.0"
    description: str = ""
    rules: list[Rule] = Field(default_factory=list)
    intra_strategy: IntraStrategy = IntraStrategy.MOST_SPECIFIC_WINS
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> "RulePack":
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"Duplicate rule id in pack {self.name!r}: {rule.id!r}")
            seen.add(rule.id)
        return self


class ScopeIntersection(BaseModel):
    """Two rules whose scopes overlap — detected at lint time."""

    rule_a: str
    rule_b: str
    pattern_a: str
    pattern_b: str
    example_path: str = ""


class RuleSelection(BaseModel):
    """Output of RulesEngine.select() — the resolved rule set for a given context."""

    rules: list[Rule] = Field(default_factory=list)
    denied_ids: list[str] = Field(default_factory=list)
    conflicts_resolved: int = 0
    injected_tokens: int = 0

    def context_text(self) -> str:
        """Render the selected rules as an injected context block."""
        if not self.rules:
            return ""
        lines = ["<forge:rules>"]
        for rule in self.rules:
            prefix = f"[{rule.action.upper()}]"
            lines.append(f"{prefix} {rule.description}")
            if rule.content.strip():
                lines.append(rule.content.strip())
        lines.append("</forge:rules>")
        return "\n".join(lines)
