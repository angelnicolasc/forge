"""Tests for forge_skills.dispatcher — IntentDispatcher.

The IntentClassifier (sentence-transformers) is replaced with a synchronous
fake injected directly into dispatcher._classifier. This way all tests run
without the [intent] extra installed.
"""

from __future__ import annotations

import asyncio

import pytest

from forge_skills.dispatcher import DispatchError, IntentDispatcher
from forge_skills.registry import SkillRegistry
from forge_skills.types import SkillDef


# ---------------------------------------------------------------------------
# Fake classifier — no sentence-transformers required
# ---------------------------------------------------------------------------


class _FakeClassifier:
    """Deterministic classifier for tests: maps exact text → (label, confidence)."""

    def __init__(self, responses: dict[str, tuple[str, float]]) -> None:
        self._responses = responses
        self.registered: dict[str, list[str]] = {}

    def register_intent(self, label: str, examples: list[str]) -> None:
        self.registered[label] = examples

    async def classify(self, text: str) -> tuple[str, float]:
        return self._responses.get(text, ("unknown", 0.0))


def _reg_with_skills(*names: str) -> SkillRegistry:
    reg = SkillRegistry()
    for name in names:
        reg.register(SkillDef(name=name, examples=[f"please {name}"]))
    return reg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIntentDispatcher:
    def _disp(
        self,
        registry: SkillRegistry,
        responses: dict[str, tuple[str, float]],
        threshold: float = 0.35,
    ) -> IntentDispatcher:
        d = IntentDispatcher(registry, confidence_threshold=threshold)
        d._classifier = _FakeClassifier(responses)
        return d

    def test_resolve_known_skill(self) -> None:
        reg = _reg_with_skills("summarize")
        disp = self._disp(reg, {"summarize this": ("summarize", 0.9)})
        skill = asyncio.run(disp.resolve("summarize this"))
        assert skill.name == "summarize"

    def test_resolve_unknown_raises_dispatch_error(self) -> None:
        reg = _reg_with_skills("summarize")
        disp = self._disp(reg, {})  # all texts → ("unknown", 0.0)
        with pytest.raises(DispatchError):
            asyncio.run(disp.resolve("something completely random"))

    def test_resolve_below_threshold_raises(self) -> None:
        reg = _reg_with_skills("summarize")
        disp = self._disp(reg, {"text": ("summarize", 0.2)}, threshold=0.5)
        with pytest.raises(DispatchError, match="threshold"):
            asyncio.run(disp.resolve("text"))

    def test_resolve_at_threshold_succeeds(self) -> None:
        reg = _reg_with_skills("greet")
        disp = self._disp(reg, {"hi": ("greet", 0.5)}, threshold=0.5)
        skill = asyncio.run(disp.resolve("hi"))
        assert skill.name == "greet"

    def test_resolve_label_not_in_registry_raises(self) -> None:
        reg = _reg_with_skills("summarize")
        # Classifier says "ghost" but "ghost" is not registered
        disp = self._disp(reg, {"text": ("ghost", 0.95)})
        with pytest.raises(DispatchError):
            asyncio.run(disp.resolve("text"))

    def test_classify_text_returns_tuple(self) -> None:
        reg = _reg_with_skills("ping")
        disp = self._disp(reg, {"ping": ("ping", 0.8)})
        label, conf = asyncio.run(disp.classify_text("ping"))
        assert label == "ping"
        assert conf == 0.8

    def test_sync_intents_registers_examples(self) -> None:
        reg = _reg_with_skills("ping", "greet")
        disp = self._disp(reg, {})
        disp.sync_intents()
        fake: _FakeClassifier = disp._classifier  # type: ignore[assignment]
        assert "ping" in fake.registered
        assert "greet" in fake.registered

    def test_sync_intents_skips_skills_without_examples(self) -> None:
        reg = SkillRegistry()
        reg.register(SkillDef(name="bare"))  # no examples
        disp = self._disp(reg, {})
        disp.sync_intents()
        fake: _FakeClassifier = disp._classifier  # type: ignore[assignment]
        assert "bare" not in fake.registered

    def test_multi_skill_routing(self) -> None:
        reg = _reg_with_skills("summarize", "translate")
        disp = self._disp(
            reg,
            {
                "summarize text": ("summarize", 0.9),
                "translate to french": ("translate", 0.85),
            },
        )
        assert asyncio.run(disp.resolve("summarize text")).name == "summarize"
        assert asyncio.run(disp.resolve("translate to french")).name == "translate"

    def test_missing_intent_extra_raises_import_error(self) -> None:
        reg = _reg_with_skills("x")
        disp = IntentDispatcher(reg)  # no _classifier injected
        # Patch _get_classifier to simulate missing sentence-transformers
        original = disp._get_classifier

        def _broken() -> object:
            raise ImportError("sentence-transformers not installed")

        disp._get_classifier = _broken  # type: ignore[method-assign]
        with pytest.raises(ImportError):
            asyncio.run(disp.classify_text("anything"))
