"""Shared intent classification engine for forge-core.

Provides a lightweight cosine-similarity classifier over labeled example texts
using sentence-transformers. The engine is lazy-initialized — the model loads
on the first call to classify() or register_intent(), not on import.

Exposed as a process-wide singleton via get_intent_classifier(). forge-rules
and forge-skills call get_intent_classifier() during their initialization and
register their domain intents. forge-core ships the engine; the catalog is
owned by the callers.

Usage::

    from forge_core.intent import get_intent_classifier

    clf = get_intent_classifier()
    clf.register_intent("run_tests", ["run the tests", "execute test suite"])
    label, confidence = await clf.classify("please run pytest")
    # label == "run_tests", confidence > 0.35

Requirements:
    pip install forge-core[intent]
    # installs sentence-transformers>=3.0 and numpy>=1.26
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import numpy as np

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Internal data model
# ---------------------------------------------------------------------------


@dataclass
class _IntentEntry:
    """A registered intent with pre-computed, L2-normalized example embeddings."""

    label: str
    examples: list[str]
    embeddings: list[Any] = field(default_factory=list)  # list[np.ndarray]


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class IntentClassifier:
    """Cosine-similarity intent classifier backed by sentence-transformers.

    Do not instantiate directly — use get_intent_classifier() to share the
    process-wide singleton. Direct instantiation is supported for testing.

    Thread / async safety
    ---------------------
    - Model loading is protected by threading.Lock (double-checked locking).
    - The catalog dict is written only by register_intent() under the same
      lock. Read access in _classify_sync() is lock-free; Python's GIL
      protects dict iteration for the current catalog snapshot.
    """

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        confidence_threshold: float = 0.35,
    ) -> None:
        self._model_name = model_name
        self._threshold = confidence_threshold
        self._model: Any = None  # SentenceTransformer, loaded lazily
        self._lock = threading.Lock()
        self._intents: dict[str, _IntentEntry] = {}

    # ------------------------------------------------------------------
    # Lazy model initialization (thread-safe)
    # ------------------------------------------------------------------

    def _ensure_model(self) -> Any:
        """Return the loaded SentenceTransformer, loading it on first call."""
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:  # double-checked locking
                return self._model
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise ImportError(
                    "sentence-transformers is required for IntentClassifier. "
                    "Install with: pip install forge-core[intent]"
                ) from exc
            logger.info("intent_classifier.loading_model", model=self._model_name)
            self._model = SentenceTransformer(self._model_name)
            logger.info("intent_classifier.model_ready", model=self._model_name)
        return self._model

    # ------------------------------------------------------------------
    # Catalog management
    # ------------------------------------------------------------------

    def register_intent(self, label: str, examples: list[str]) -> None:
        """Register a labeled intent with example utterances.

        Pre-computes L2-normalized embeddings for all examples immediately.
        Calling this again with the same label replaces the existing entry.

        Args:
            label: Intent label, e.g. ``"run_tests"``.
            examples: Representative utterances. Must be non-empty.
        """
        if not examples:
            raise ValueError(f"Intent '{label}' must have at least one example.")

        import numpy as np

        model = self._ensure_model()
        raw: np.ndarray = model.encode(
            examples,
            convert_to_numpy=True,
            normalize_embeddings=True,  # dot product == cosine sim
        )
        embeddings = [raw[i] for i in range(len(examples))]

        with self._lock:
            self._intents[label] = _IntentEntry(
                label=label, examples=examples, embeddings=embeddings
            )

        logger.debug(
            "intent_classifier.registered",
            label=label,
            n_examples=len(examples),
        )

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    async def classify(self, text: str) -> tuple[str, float]:
        """Classify text into (intent_label, confidence).

        Runs embedding + similarity in a thread pool to avoid blocking
        the asyncio event loop (sentence-transformers is sync).

        Returns:
            ``("unknown", 0.0)`` when no intent exceeds the threshold.
        """
        return await asyncio.to_thread(self._classify_sync, text)

    def _classify_sync(self, text: str) -> tuple[str, float]:
        """Blocking classification — called via asyncio.to_thread."""
        if not self._intents:
            return ("unknown", 0.0)

        import numpy as np

        model = self._ensure_model()
        query: np.ndarray = model.encode(
            text,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )

        best_label = "unknown"
        best_score = 0.0

        for entry in self._intents.values():
            for emb in entry.embeddings:
                # Both vectors are L2-normalized → dot product == cosine similarity
                score = float(np.dot(query, emb))
                if score > best_score:
                    best_score = score
                    best_label = entry.label

        if best_score < self._threshold:
            return ("unknown", 0.0)
        return (best_label, best_score)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_classifier_instance: IntentClassifier | None = None
_singleton_lock = threading.Lock()


def get_intent_classifier(
    *,
    model_name: str | None = None,
    confidence_threshold: float | None = None,
) -> IntentClassifier:
    """Return the process-wide IntentClassifier singleton.

    On first call, creates the instance using the provided parameters (or
    reads from ForgeConfig defaults). The model itself is NOT loaded until
    the first call to classify() or register_intent().

    Subsequent calls ignore parameters — the singleton is already configured.

    Args:
        model_name: Override the sentence-transformers model. When None,
            reads from ForgeConfig (env var FORGE_INTENT_MODEL).
        confidence_threshold: Override the confidence threshold. When None,
            reads from ForgeConfig (env var FORGE_INTENT_CONFIDENCE_THRESHOLD).
    """
    global _classifier_instance
    if _classifier_instance is not None:
        return _classifier_instance

    with _singleton_lock:
        if _classifier_instance is not None:  # double-checked locking
            return _classifier_instance

        if model_name is None or confidence_threshold is None:
            from forge_core.config import ForgeConfig

            cfg = ForgeConfig()
            model_name = model_name or cfg.intent_model
            confidence_threshold = confidence_threshold or cfg.intent_confidence_threshold

        _classifier_instance = IntentClassifier(
            model_name=model_name,
            confidence_threshold=confidence_threshold,
        )
        logger.debug(
            "intent_classifier.singleton_created",
            model=model_name,
            threshold=confidence_threshold,
        )

    return _classifier_instance
