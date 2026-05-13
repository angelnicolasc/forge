"""Tests for the shared IntentClassifier engine (forge_core.intent).

These tests load the actual sentence-transformers model and are therefore
slow in CI. They are marked with ``requires_sentence_transformers`` so
they can be skipped in fast-check runs:

    pytest -m "not requires_sentence_transformers"
"""

from __future__ import annotations

import asyncio

import pytest

from forge_core.intent import IntentClassifier, get_intent_classifier
from forge_core.protocols import Classifier

pytestmark = pytest.mark.requires_sentence_transformers


@pytest.fixture
def clf() -> IntentClassifier:
    """Fresh classifier instance per test — not the global singleton."""
    return IntentClassifier(model_name="all-MiniLM-L6-v2", confidence_threshold=0.35)


# ---------------------------------------------------------------------------
# Catalog management
# ---------------------------------------------------------------------------


class TestIntentRegistration:
    def test_register_and_classify_known_intent(self, clf: IntentClassifier) -> None:
        clf.register_intent("greet", ["hello", "hi there", "good morning"])
        label, score = asyncio.run(clf.classify("hey!"))
        assert label == "greet"
        assert score > 0.35

    def test_empty_examples_raises(self, clf: IntentClassifier) -> None:
        with pytest.raises(ValueError, match="at least one example"):
            clf.register_intent("empty_intent", [])

    def test_unknown_below_threshold(self, clf: IntentClassifier) -> None:
        clf.register_intent("book_flight", ["I want to book a flight", "reserve a seat"])
        label, score = asyncio.run(clf.classify("quantum physics lecture notes"))
        assert label == "unknown"
        assert score == 0.0

    def test_no_intents_returns_unknown(self, clf: IntentClassifier) -> None:
        label, score = asyncio.run(clf.classify("anything goes"))
        assert label == "unknown"
        assert score == 0.0

    def test_re_register_replaces_entry(self, clf: IntentClassifier) -> None:
        clf.register_intent("cmd", ["run the process"])
        clf.register_intent("cmd", ["start the server", "launch the application"])
        # Should not raise and should still classify
        label, _ = asyncio.run(clf.classify("launch the app"))
        assert label == "cmd"

    def test_multiple_intents_picks_best(self, clf: IntentClassifier) -> None:
        clf.register_intent("greet", ["hello", "hi", "hey"])
        clf.register_intent("farewell", ["goodbye", "bye", "see you later"])
        label, score = asyncio.run(clf.classify("goodbye friend"))
        assert label == "farewell"
        assert score > 0.35


# ---------------------------------------------------------------------------
# Singleton behaviour
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_intent_classifier_returns_same_instance(self) -> None:
        import forge_core.intent as intent_mod

        original = intent_mod._classifier_instance
        intent_mod._classifier_instance = None
        try:
            a = get_intent_classifier()
            b = get_intent_classifier()
            assert a is b
        finally:
            intent_mod._classifier_instance = original

    def test_classifier_satisfies_protocol(self) -> None:
        assert isinstance(IntentClassifier(), Classifier)
