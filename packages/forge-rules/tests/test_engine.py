"""Tests for forge_rules.engine — RulesEngine top-level API."""

from __future__ import annotations

import asyncio
import textwrap

import pytest

from forge_rules.engine import RulesEngine
from forge_rules.types import IntraStrategy, Rule, RuleAction, RulePack


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pack(
    name: str = "test",
    rules: list[Rule] | None = None,
    strategy: IntraStrategy = IntraStrategy.MOST_SPECIFIC_WINS,
) -> RulePack:
    return RulePack(name=name, rules=rules or [], intra_strategy=strategy)


def _rule(
    id: str,
    action: RuleAction = RuleAction.SUGGEST,
    content: str = "Follow this rule.",
    scope: list[str] | None = None,
    intent_tags: list[str] | None = None,
    enabled: bool = True,
    priority: int = 0,
) -> Rule:
    return Rule(
        id=id,
        action=action,
        description=f"Rule {id}",
        content=content,
        scope=scope or [],
        intent_tags=intent_tags or [],
        enabled=enabled,
        priority=priority,
    )


SIMPLE_YAML = textwrap.dedent("""
    name: yaml-pack
    version: "0.1.0"
    rules:
      - id: no-print
        action: deny
        description: "Prohibit print() in production code"
        content: "Use structured logging instead of print()."
        scope:
          - "src/**/*.py"
        intent_tags: []
        priority: 0
        enabled: true
      - id: add-types
        action: suggest
        description: "Add type hints"
        content: "All public functions should have type annotations."
        scope: []
        intent_tags: []
        priority: 0
        enabled: true
""").strip()


# ---------------------------------------------------------------------------
# Pack management
# ---------------------------------------------------------------------------


class TestPackManagement:
    def test_load_pack_registers(self) -> None:
        engine = RulesEngine()
        pack = _pack("alpha")
        engine.load_pack(pack)
        assert len(engine.packs) == 1
        assert engine.packs[0].name == "alpha"

    def test_load_pack_replaces_same_name(self) -> None:
        engine = RulesEngine()
        engine.load_pack(_pack("alpha", rules=[_rule("r1")]))
        engine.load_pack(_pack("alpha", rules=[_rule("r2")]))
        assert len(engine.packs) == 1
        assert engine.packs[0].rules[0].id == "r2"

    def test_load_yaml_string(self) -> None:
        engine = RulesEngine()
        pack = engine.load_yaml(SIMPLE_YAML)
        assert pack.name == "yaml-pack"
        assert len(pack.rules) == 2
        assert len(engine.packs) == 1

    def test_unload_pack(self) -> None:
        engine = RulesEngine()
        engine.load_pack(_pack("alpha"))
        assert engine.unload_pack("alpha")
        assert engine.packs == []

    def test_unload_nonexistent_returns_false(self) -> None:
        engine = RulesEngine()
        assert not engine.unload_pack("ghost")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class TestSelect:
    def test_empty_engine_returns_empty_selection(self) -> None:
        engine = RulesEngine()
        sel = engine.select(path="src/main.py")
        assert sel.rules == []
        assert sel.denied_ids == []

    def test_wildcard_rule_always_selected(self) -> None:
        engine = RulesEngine()
        engine.load_pack(_pack("p", rules=[_rule("always")]))
        sel = engine.select(path="anything.py")
        assert any(r.id == "always" for r in sel.rules)

    def test_scope_filter(self) -> None:
        engine = RulesEngine()
        py_rule = _rule("py", scope=["**/*.py"])
        js_rule = _rule("js", scope=["**/*.js"])
        engine.load_pack(_pack("p", rules=[py_rule, js_rule]))
        sel = engine.select(path="src/main.py")
        ids = {r.id for r in sel.rules}
        assert "py" in ids
        assert "js" not in ids

    def test_deny_rule_creates_denied_ids(self) -> None:
        engine = RulesEngine()
        deny = _rule("block", action=RuleAction.DENY)
        suggest = _rule("tip", action=RuleAction.SUGGEST)
        engine.load_pack(_pack("p", rules=[deny, suggest]))
        sel = engine.select()
        assert "tip" in sel.denied_ids
        assert any(r.id == "block" for r in sel.rules)

    def test_pack_name_filter(self) -> None:
        engine = RulesEngine()
        engine.load_pack(_pack("alpha", rules=[_rule("ra")]))
        engine.load_pack(_pack("beta", rules=[_rule("rb")]))
        sel = engine.select(pack_name="alpha")
        ids = {r.id for r in sel.rules}
        assert "ra" in ids
        assert "rb" not in ids

    def test_intent_filter(self) -> None:
        engine = RulesEngine()
        review_r = _rule("review", intent_tags=["code_review"])
        engine.load_pack(_pack("p", rules=[review_r]))
        sel = engine.select(intent="code_review")
        assert any(r.id == "review" for r in sel.rules)
        sel2 = engine.select(intent="other")
        assert not any(r.id == "review" for r in sel2.rules)


# ---------------------------------------------------------------------------
# Context text rendering
# ---------------------------------------------------------------------------


class TestContextText:
    def test_empty_selection_empty_string(self) -> None:
        engine = RulesEngine()
        sel = engine.select()
        assert sel.context_text() == ""

    def test_context_block_contains_rule_content(self) -> None:
        engine = RulesEngine()
        engine.load_pack(_pack("p", rules=[_rule("r1", content="Do the thing.")]))
        sel = engine.select()
        ctx = sel.context_text()
        assert "<forge:rules>" in ctx
        assert "Do the thing." in ctx
        assert "</forge:rules>" in ctx

    def test_action_prefix_in_context(self) -> None:
        engine = RulesEngine()
        engine.load_pack(_pack("p", rules=[_rule("d", action=RuleAction.DENY, content="No.")]))
        sel = engine.select()
        ctx = sel.context_text()
        assert "[DENY]" in ctx


# ---------------------------------------------------------------------------
# Inject (async — emits CONTEXT_INJECTED)
# ---------------------------------------------------------------------------


class TestInject:
    def test_inject_returns_context_string(self) -> None:
        engine = RulesEngine()
        engine.load_pack(_pack("p", rules=[_rule("r", content="Follow this.")]))
        sel = engine.select()
        result = asyncio.run(engine.inject(sel, run_id="run-001"))
        assert "Follow this." in result

    def test_inject_emits_event_on_bus(self) -> None:
        """CONTEXT_INJECTED is emitted on the EventBus when rules are injected."""
        from forge_core.types import RunEvent, RunEventKind

        published: list[RunEvent] = []

        class _FakeBus:
            async def publish(self, event: RunEvent) -> None:
                published.append(event)

        engine = RulesEngine()
        engine.set_event_bus(_FakeBus())
        engine.load_pack(_pack("p", rules=[_rule("r", content="Some content.")]))
        sel = engine.select()
        asyncio.run(engine.inject(sel, run_id="run-evt"))

        assert len(published) == 1
        assert published[0].kind == RunEventKind.CONTEXT_INJECTED
        assert published[0].context_tokens > 0
        assert published[0].data["source"] == "rules"

    def test_inject_no_bus_no_error(self) -> None:
        engine = RulesEngine()
        engine.load_pack(_pack("p", rules=[_rule("r", content="x")]))
        sel = engine.select()
        result = asyncio.run(engine.inject(sel))
        assert isinstance(result, str)

    def test_inject_empty_selection_returns_empty(self) -> None:
        engine = RulesEngine()
        sel = engine.select()
        result = asyncio.run(engine.inject(sel))
        assert result == ""


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------


class TestLint:
    def test_no_intersections_clean(self) -> None:
        engine = RulesEngine()
        py_rule = _rule("py", scope=["**/*.py"])
        js_rule = _rule("js", scope=["**/*.js"])
        engine.load_pack(_pack("p", rules=[py_rule, js_rule]))
        assert engine.lint() == []

    def test_intersections_detected(self) -> None:
        engine = RulesEngine()
        broad = _rule("broad", scope=["src/**/*.py"])
        narrow = _rule("narrow", scope=["src/core/*.py"])
        engine.load_pack(_pack("p", rules=[broad, narrow]))
        intersections = engine.lint()
        assert len(intersections) >= 1
        ids = {(i.rule_a, i.rule_b) for i in intersections}
        assert ("broad", "narrow") in ids

    def test_lint_by_pack_name(self) -> None:
        engine = RulesEngine()
        broad = _rule("broad", scope=["**/*.py"])
        narrow = _rule("narrow", scope=["src/*.py"])
        engine.load_pack(_pack("alpha", rules=[broad, narrow]))
        engine.load_pack(_pack("beta", rules=[_rule("clean", scope=["docs/*.md"])]))
        # lint only alpha
        result = engine.lint(pack_name="alpha")
        assert any(i.rule_a in ("broad", "narrow") for i in result)


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_pack_no_errors(self) -> None:
        engine = RulesEngine()
        pack = _pack("good", rules=[_rule("r1")])
        assert engine.validate(pack) == []

    def test_empty_pack_returns_error(self) -> None:
        engine = RulesEngine()
        pack = _pack("empty")
        errors = engine.validate(pack)
        assert any("no rules" in e.lower() for e in errors)

    def test_disabled_deny_rule_warning(self) -> None:
        engine = RulesEngine()
        pack = _pack("p", rules=[_rule("d", action=RuleAction.DENY, enabled=False)])
        errors = engine.validate(pack)
        assert any("disabled" in e.lower() or "deny" in e.lower() for e in errors)
