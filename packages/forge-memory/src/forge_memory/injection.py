"""Memory context injection — the hook that closes the loop.

The Living Collaborative Memory stores knowledge across runs, but until
something *reads* from it before a run, the memory is a museum. This
module implements the pre-run hook that turns it into a working memory:

    envelope -> query HybridMemory -> format top-k -> enrich envelope

The injector is deliberately small and pure — it takes an envelope in,
returns an envelope out. The orchestrator decides whether to call it
(gated by :attr:`MemoryConfig.enable_context_injection` and the
``FORGE_ENABLE_MEMORY_INJECTION`` feature flag).

Design decisions worth defending
--------------------------------

*   **Best-effort, never-raises.** A memory backend hiccup is not a
    reason to fail the user's run. All failures are logged and the
    original envelope passes through untouched.
*   **Query-text heuristic lives here**, not in the orchestrator. The
    orchestrator shouldn't know that memory is free-text-searched; it
    hands over the envelope and gets an envelope back.
*   **Two injection targets**. The formatted block lands in
    :attr:`TaskEnvelope.memory_context` (list of strings — adapters
    surface it to prompts) *and* in
    :attr:`TaskEnvelope.context["forge_memory"]` (structured dict —
    adapters that want richer access can read it). Belt and suspenders;
    the plan text covers both.
*   **Feature flag layered on top of config flag.** The env flag acts
    as a global kill-switch regardless of per-run config — useful when
    an operator needs to disable injection fleet-wide without pushing a
    config change.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from forge_core.types import MemoryConfig, MemoryEntry, MemoryQuery, TaskEnvelope

logger = structlog.get_logger()


_FEATURE_FLAG = "FORGE_ENABLE_MEMORY_INJECTION"


def _flag_enabled() -> bool:
    """Read the global kill-switch at call time (not import time).

    Tests flip env vars between cases; caching the result at import
    time would make behavior stick to whichever value happened to be
    set first. Read it fresh — this is not hot-path code.
    """
    val = os.environ.get(_FEATURE_FLAG, "").strip().lower()
    return val in {"1", "true", "yes", "on"}


class MemoryContextInjector:
    """Enrich a :class:`TaskEnvelope` with relevant memories before run.

    Parameters
    ----------
    memory
        Anything with an async ``query(MemoryQuery) -> list[(entry, score)]``
        method — in practice :class:`forge_memory.HybridMemory`, but
        structurally typed so tests can substitute a stub.
    config
        Per-run memory config. ``enable_context_injection=False`` makes
        :meth:`inject` a no-op pass-through.
    """

    def __init__(
        self,
        memory: Any,
        config: MemoryConfig | None = None,
    ) -> None:
        self._memory = memory
        self._config = config or MemoryConfig()

    async def inject(self, envelope: TaskEnvelope) -> TaskEnvelope:
        """Return an envelope enriched with memory context, or the
        original if injection is disabled / produced nothing useful.

        The method is deliberately tolerant of backend failures: if the
        query raises, we log and pass the envelope through unchanged.
        The user's run does not get blocked on an ailing memory.
        """
        if not self._config.enable_context_injection:
            return envelope
        if not _flag_enabled():
            # Respect the global kill-switch even if the per-run config
            # asked for injection.
            return envelope

        query_text = self._extract_query_text(envelope.input)
        if not query_text:
            return envelope

        top_k = max(1, int(self._config.top_k))
        query = MemoryQuery(
            text=query_text,
            tags=envelope.tags or None,
            top_k=top_k,
            limit=top_k,
        )

        try:
            results = await self._memory.query(query)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("memory_injector.query_failed", error=str(exc))
            return envelope

        relevant = [
            (entry, score)
            for entry, score in (results or [])
            if score >= self._config.min_relevance
        ]
        if not relevant:
            logger.debug(
                "memory_injector.no_relevant_hits",
                considered=len(results or []),
                min_relevance=self._config.min_relevance,
            )
            return envelope

        formatted_lines = self._format_memories(relevant)
        structured = self._structured_block(relevant)

        logger.info(
            "memory_injector.injected",
            hits=len(relevant),
            top_score=float(relevant[0][1]),
        )

        return envelope.model_copy(
            update={
                "memory_context": [*envelope.memory_context, *formatted_lines],
                "context": {**envelope.context, "forge_memory": structured},
                "metadata": {**envelope.metadata, "memory_hits": len(relevant)},
            }
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_query_text(payload: Any) -> str:
        """Pull a natural-language query out of the envelope input.

        We check a short allowlist of common field names before giving
        up. If nothing matches, we stringify the whole payload — ugly
        but functional, and better than silently skipping injection.
        """
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, dict):
            for key in ("query", "question", "prompt", "input", "text", "task"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            # Fallback: concatenate string values, capped so a huge
            # dict doesn't produce a multi-MB embedding query.
            joined = " ".join(
                str(v) for v in payload.values() if isinstance(v, (str, int, float))
            ).strip()
            return joined[:2000]
        return str(payload)[:2000]

    @staticmethod
    def _format_memories(results: list[tuple[MemoryEntry, float]]) -> list[str]:
        """Render memories as short lines suitable for prompt injection."""
        lines: list[str] = []
        for entry, score in results:
            snippet = entry.content.strip().replace("\n", " ")
            if len(snippet) > 500:
                snippet = snippet[:497] + "..."
            lines.append(f"[memory score={score:.2f}] {snippet}")
        return lines

    @staticmethod
    def _structured_block(
        results: list[tuple[MemoryEntry, float]],
    ) -> dict[str, Any]:
        """Machine-readable companion to the prompt-formatted lines."""
        return {
            "hits": [
                {
                    "id": entry.id,
                    "score": float(score),
                    "content": entry.content,
                    "tags": dict(entry.tags),
                    "source_run_id": entry.source_run_id,
                    "version": entry.version,
                }
                for entry, score in results
            ],
            "count": len(results),
        }
