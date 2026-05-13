"""forge_skills.dispatcher — IntentDispatcher: free text → SkillDef via IntentClassifier.

Requires forge-core[intent] (sentence-transformers) to be installed.
The classifier is loaded lazily on the first classify call.
"""

from __future__ import annotations

import structlog

from forge_skills.registry import SkillNotFoundError, SkillRegistry
from forge_skills.types import SkillDef

logger = structlog.get_logger(__name__)


class DispatchError(RuntimeError):
    """Raised when no skill can be matched for the given text."""


class IntentDispatcher:
    """Classifies free text to a registered skill via IntentClassifier.

    Lifecycle:
      1. Call sync_intents() after any registry modification so the
         classifier knows about all skills and their example phrases.
      2. Call classify_text(text) to get (label, confidence).
      3. Call resolve(text) to get the matching SkillDef, or raise DispatchError.

    The underlying IntentClassifier is loaded lazily on the first call.
    If forge-core[intent] is not installed, an ImportError is raised with
    a clear installation hint.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        confidence_threshold: float = 0.35,
        model_name: str = "all-MiniLM-L6-v2",
    ) -> None:
        self._registry = registry
        self._threshold = confidence_threshold
        self._model_name = model_name
        self._classifier: object | None = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_classifier(self) -> object:
        if self._classifier is None:
            try:
                from forge_core.intent import IntentClassifier  # type: ignore[attr-defined]
            except ImportError as exc:
                raise ImportError(
                    "IntentDispatcher requires forge-core[intent]. "
                    "Install with: pip install forge-core[intent]"
                ) from exc
            self._classifier = IntentClassifier(
                model_name=self._model_name,
                confidence_threshold=self._threshold,
            )
        return self._classifier

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_intents(self) -> None:
        """Register each skill's example phrases with the IntentClassifier.

        Must be called (at least once) before dispatch_text / resolve will work.
        """
        clf = self._get_classifier()
        count = 0
        for skill in self._registry.all_skills():
            if skill.examples:
                clf.register_intent(skill.name, skill.examples)  # type: ignore[attr-defined]
                count += 1
        logger.info("skills.dispatcher.synced", skills_with_examples=count)

    async def classify_text(self, text: str) -> tuple[str, float]:
        """Return (intent_label, confidence). "unknown" if below threshold."""
        clf = self._get_classifier()
        return await clf.classify(text)  # type: ignore[attr-defined,return-value]

    async def resolve(self, text: str) -> SkillDef:
        """Classify *text* and return the matching SkillDef.

        Raises DispatchError if confidence is below threshold or if the
        matched label is not registered in the registry.
        """
        label, confidence = await self.classify_text(text)
        if label == "unknown" or confidence < self._threshold:
            raise DispatchError(
                f"No skill matched text {text[:80]!r} "
                f"(best confidence {confidence:.2f} < threshold {self._threshold:.2f})."
            )
        try:
            skill = self._registry.get(label)
        except SkillNotFoundError as exc:
            raise DispatchError(
                f"Classifier matched label '{label}' but it is not in the registry."
            ) from exc
        logger.info("skills.dispatcher.resolved", skill=label, confidence=f"{confidence:.3f}")
        return skill
