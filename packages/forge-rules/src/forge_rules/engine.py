"""forge_rules.engine — RulesEngine: the public API for Fase 1.

Usage:

    engine = RulesEngine()
    engine.load_pack(pack)
    selection = engine.select(path="src/api/views.py", intent="code_review")
    ctx_block = engine.inject(selection, run_id="run-xyz")

The engine is async-safe: select() is synchronous (fast path), inject() emits
a CONTEXT_INJECTED RunEvent on the EventBus if one is registered.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog

from forge_core.types import RunEvent, RunEventKind
from forge_rules.glob_intersect import find_intersections
from forge_rules.loader import load_yaml
from forge_rules.resolver import resolve
from forge_rules.selector import select_rules
from forge_rules.types import IntraStrategy, Rule, RuleAction, RulePack, RuleSelection, ScopeIntersection

logger = structlog.get_logger(__name__)


class RulesEngine:
    """Central engine: manages multiple RulePacks and resolves conflicts."""

    def __init__(self) -> None:
        self._packs: list[RulePack] = []
        self._bus: Any = None  # EventBus from forge_core — injected at runtime

    # ------------------------------------------------------------------
    # Pack management
    # ------------------------------------------------------------------

    def load_pack(self, pack: RulePack) -> None:
        """Register a RulePack. Replaces any existing pack with the same name."""
        self._packs = [p for p in self._packs if p.name != pack.name]
        self._packs.append(pack)
        logger.info("rules.pack.loaded", pack=pack.name, rules=len(pack.rules))

    def load_yaml(self, source: str | Path) -> RulePack:
        """Load a YAML file (or YAML string) and register the pack. Returns the pack."""
        pack = load_yaml(source)
        self.load_pack(pack)
        return pack

    def unload_pack(self, name: str) -> bool:
        """Remove a pack by name. Returns True if it was present."""
        before = len(self._packs)
        self._packs = [p for p in self._packs if p.name != name]
        return len(self._packs) < before

    @property
    def packs(self) -> list[RulePack]:
        return list(self._packs)

    def set_event_bus(self, bus: Any) -> None:
        """Inject an EventBus for CONTEXT_INJECTED emissions."""
        self._bus = bus

    # ------------------------------------------------------------------
    # Selection + injection
    # ------------------------------------------------------------------

    def select(
        self,
        *,
        path: str = "",
        intent: str = "",
        pack_name: str | None = None,
    ) -> RuleSelection:
        """Select and resolve rules for the given context.

        Args:
            path: File path to match against rule scopes (glob patterns).
            intent: Intent label from IntentClassifier (or "" for wildcard).
            pack_name: Limit selection to a specific pack (default: all packs).

        Returns:
            RuleSelection with the winning rules, denied ids, and conflict count.
        """
        packs = (
            [p for p in self._packs if p.name == pack_name]
            if pack_name
            else self._packs
        )

        all_matched: list[Rule] = []
        strategy = IntraStrategy.MOST_SPECIFIC_WINS
        for pack in packs:
            matched = select_rules(pack, path=path, intent=intent)
            all_matched.extend(matched)
            # Use strategy of the first pack that has matches (packs are ordered)
            if matched:
                strategy = pack.intra_strategy

        winners, denied_ids = resolve(all_matched, strategy)
        resolved_count = len(all_matched) - len(winners) - len(denied_ids)

        selection = RuleSelection(
            rules=winners,
            denied_ids=denied_ids,
            conflicts_resolved=max(resolved_count, 0),
        )
        logger.debug(
            "rules.selected",
            path=path,
            intent=intent,
            winners=[r.id for r in winners],
            denied=denied_ids,
        )
        return selection

    async def inject(
        self,
        selection: RuleSelection,
        *,
        run_id: str = "",
    ) -> str:
        """Render the selection to a context block and emit CONTEXT_INJECTED.

        Returns the rendered context string (may be empty if no rules matched).
        """
        text = selection.context_text()
        token_count = len(text) // 4  # rough approximation

        if text and self._bus is not None:
            event = RunEvent(
                kind=RunEventKind.CONTEXT_INJECTED,
                run_id=run_id,
                context_tokens=token_count,
                data={
                    "source": "rules",
                    "rule_count": len(selection.rules),
                    "denied_count": len(selection.denied_ids),
                },
            )
            await self._bus.publish(event)

        return text

    # ------------------------------------------------------------------
    # Lint & validate
    # ------------------------------------------------------------------

    def lint(self, pack_name: str | None = None) -> list[ScopeIntersection]:
        """Detect scope intersections across rules at lint time.

        Returns a list of ScopeIntersection objects (empty = clean).
        Only considers rules from *pack_name* if specified.
        """
        packs = (
            [p for p in self._packs if p.name == pack_name]
            if pack_name
            else self._packs
        )
        scope_map: dict[str, list[str]] = {}
        for pack in packs:
            for rule in pack.rules:
                if rule.scope:
                    scope_map[rule.id] = rule.scope

        intersections: list[ScopeIntersection] = []
        for id_a, id_b, pat_a, pat_b, example in find_intersections(scope_map):
            intersections.append(
                ScopeIntersection(
                    rule_a=id_a,
                    rule_b=id_b,
                    pattern_a=pat_a,
                    pattern_b=pat_b,
                    example_path=example,
                )
            )
        return intersections

    def validate(self, pack: RulePack | None = None) -> list[str]:
        """Return a list of validation error strings.

        Validates all registered packs (or a single pack) without loading it.
        An empty list means the pack is valid.
        """
        errors: list[str] = []
        packs_to_check = [pack] if pack else self._packs

        for p in packs_to_check:
            if not p.rules:
                errors.append(f"Pack {p.name!r} has no rules.")
            deny_rules = [r for r in p.rules if r.action == RuleAction.DENY and not r.enabled]
            if deny_rules:
                errors.append(
                    f"Pack {p.name!r}: disabled DENY rules {[r.id for r in deny_rules]} "
                    "have no effect — consider removing them."
                )

        return errors
