"""HybridMemory — The Living Collaborative Memory for Forge.

Orchestrates three complementary knowledge backends:

  1. Vector   (ChromaDB)   — semantic similarity search via embeddings
  2. Graph    (NetworkX)   — entity-relationship traversal with temporal validity
  3. Symbolic (RuleEngine) — deterministic business rules that filter/boost results

Results from vector and graph are fused via Reciprocal Rank Fusion (RRF),
then passed through the symbolic rule engine for final filtering and reranking.

This is the feature no one else has production-ready:
  - Cross-run persistence (agents remember what happened in past runs)
  - Collaborative writes (multiple agents can write to the same KB)
  - Full provenance (every entry has creator, version, and evidence chain)
  - Deterministic overrides (symbolic rules always win over statistical retrieval)

Usage:
    memory = HybridMemory()
    await memory.store(entry)
    results = await memory.query(MemoryQuery(text="RAG optimization techniques"))
    for entry, score in results:
        print(f"{score:.2f}: {entry.content[:100]}")
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from forge_core.circuit_breaker import CircuitBreaker, CircuitOpenError
from forge_core.types import MemoryEntry, MemoryQuery
from forge_memory.graph.networkx_backend import NetworkXBackend
from forge_memory.ingestion import IngestionPipeline
from forge_memory.symbolic.rules import RuleEngine, default_rule_engine
from forge_memory.vector.chromadb_backend import ChromaDBBackend
from forge_memory.versioning import MemoryVersionStore

logger = structlog.get_logger()

# RRF constant (k=60 is the standard Elasticsearch default)
_RRF_K = 60


class HybridMemory:
    """Living Collaborative Memory — fuses vector, graph, and symbolic retrieval.

    Thread-safe. All methods are async. Backends are lazily initialized.

    Swapping backends is trivial — any class satisfying the MemoryBackend
    Protocol (structural typing) works: Qdrant, Neo4j, custom stores.
    """

    def __init__(
        self,
        vector_backend: Any | None = None,
        graph_backend: Any | None = None,
        rule_engine: RuleEngine | None = None,
        version_store: MemoryVersionStore | None = None,
        ingestion_pipeline: IngestionPipeline | None = None,
        rrf_k: int = _RRF_K,
        vector_weight: float = 0.6,
        graph_weight: float = 0.4,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._vector = vector_backend or ChromaDBBackend()
        self._graph = graph_backend or NetworkXBackend()
        self._rules = rule_engine or default_rule_engine
        self._versions = version_store or MemoryVersionStore()
        self._ingestion = ingestion_pipeline or IngestionPipeline()
        self._rrf_k = rrf_k
        self._vector_weight = vector_weight
        self._graph_weight = graph_weight
        # ζ.5c — a persistently broken backend shouldn't kill every run;
        # after N repeated failures ``query`` short-circuits and returns
        # an empty list with a warning. Consistent with MemoryContextInjector's
        # "memory errors never abort a run" contract (δ.1).
        self._breaker = breaker or CircuitBreaker(
            "memory_query",
            failure_threshold=5,
            recovery_timeout_seconds=60.0,
        )

    # -----------------------------------------------------------------------
    # Core API
    # -----------------------------------------------------------------------

    async def store(self, entry: MemoryEntry) -> str:
        """Store a MemoryEntry across all backends concurrently.

        The version store records provenance. Both vector and graph
        backends receive the entry simultaneously for consistency.
        """
        # Version the entry
        entry = self._versions.next_version(entry)

        # Write to vector and graph backends concurrently
        vector_id: str | BaseException
        graph_id: str | BaseException
        vector_id, graph_id = await asyncio.gather(
            self._vector.store(entry),
            self._graph.store(entry),
            return_exceptions=True,
        )

        if isinstance(vector_id, Exception):
            logger.warning("memory.vector_store_failed", error=str(vector_id))
        if isinstance(graph_id, Exception):
            logger.warning("memory.graph_store_failed", error=str(graph_id))

        # Record provenance
        self._versions.record(entry, operation="store")

        logger.debug(
            "memory.stored",
            entry_id=entry.id,
            version=entry.version,
            run_id=entry.source_run_id,
        )
        return entry.id

    async def query(
        self,
        q: MemoryQuery,
        context: dict[str, Any] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        """Query memory using the three-layer hybrid strategy.

        Flow:
          1. Query vector backend (semantic) and graph backend (structural) concurrently
          2. Fuse results via Reciprocal Rank Fusion (RRF)
          3. Apply symbolic rules for filtering and reranking
          4. Return top-k results sorted by final score

        Args:
            q: MemoryQuery with text, embedding, entity_filter, etc.
            context: Optional runtime context passed to symbolic rules
                     (e.g., {"task_type": "summarize", "agent_id": "planner"})

        Returns:
            List of (MemoryEntry, score) sorted by descending relevance.
        """
        # ζ.5c — bounded retry with breaker. If the breaker is OPEN we
        # return [] immediately with a warning; if closed, we run the
        # gather and only credit the breaker a failure when BOTH backends
        # raise (a partial degradation is not a full outage).
        try:
            await self._breaker._before_call()
        except CircuitOpenError as exc:
            logger.warning(
                "memory.query.circuit_open",
                name=exc.name,
                retry_after=exc.retry_after,
            )
            return []

        # Step 1: Query both backends concurrently
        vector_raw: list[tuple[MemoryEntry, float]] | BaseException
        graph_raw: list[tuple[MemoryEntry, float]] | BaseException
        vector_raw, graph_raw = await asyncio.gather(
            self._vector.query(q),
            self._graph.query(q),
            return_exceptions=True,
        )

        vector_failed = isinstance(vector_raw, Exception)
        graph_failed = isinstance(graph_raw, Exception)
        vector_results: list[tuple[MemoryEntry, float]] = (
            [] if vector_failed else vector_raw  # type: ignore[assignment]
        )
        graph_results: list[tuple[MemoryEntry, float]] = (
            [] if graph_failed else graph_raw  # type: ignore[assignment]
        )
        if vector_failed:
            logger.warning("memory.vector_query_failed", error=str(vector_raw))
        if graph_failed:
            logger.warning("memory.graph_query_failed", error=str(graph_raw))

        if vector_failed and graph_failed:
            self._breaker.record_failure()
        else:
            self._breaker.record_success()

        # Step 2: Fuse via RRF
        fused = _reciprocal_rank_fusion(
            [
                (vector_results, self._vector_weight),
                (graph_results, self._graph_weight),
            ],
            k=self._rrf_k,
        )

        # Step 3: Apply symbolic rules
        ctx = context or {}
        ctx["include_expired"] = q.include_expired
        final = self._rules.apply(fused, context=ctx)

        # Step 4: Enforce top_k
        result = final[: q.top_k]

        logger.debug(
            "memory.query_complete",
            query_text=q.text[:50] if q.text else None,
            vector_hits=len(vector_results),
            graph_hits=len(graph_results),
            fused=len(fused),
            after_rules=len(final),
            returned=len(result),
        )
        return result

    async def ingest_text(
        self,
        content: str,
        source_run_id: str | None = None,
        source_agent_id: str | None = None,
        tags: dict[str, str] | None = None,
        evidence: list[str] | None = None,
        relations: list[tuple[str, str, str]] | None = None,
    ) -> list[str]:
        """High-level convenience: ingest raw text and store all chunks.

        Returns list of stored entry IDs.
        """
        entries = await self._ingestion.ingest(
            content=content,
            source_run_id=source_run_id,
            source_agent_id=source_agent_id,
            tags=tags,
            evidence=evidence,
            relations=relations,
        )
        ids = []
        for entry in entries:
            entry_id = await self.store(entry)
            ids.append(entry_id)
        return ids

    async def delete(self, entry_id: str) -> bool:
        """Delete an entry from all backends."""
        v_ok: bool | BaseException
        g_ok: bool | BaseException
        v_ok, g_ok = await asyncio.gather(
            self._vector.delete(entry_id),
            self._graph.delete(entry_id),
            return_exceptions=True,
        )
        self._versions.record(
            MemoryEntry(id=entry_id, content="[deleted]"),
            operation="delete",
        )
        return bool(v_ok) or bool(g_ok)

    async def health_check(self) -> dict[str, bool]:
        """Check health of all backends."""
        v_ok: bool | BaseException
        g_ok: bool | BaseException
        v_ok, g_ok = await asyncio.gather(
            self._vector.health_check(),
            self._graph.health_check(),
            return_exceptions=True,
        )
        return {
            "vector": bool(v_ok) and not isinstance(v_ok, Exception),
            "graph": bool(g_ok) and not isinstance(g_ok, Exception),
            "symbolic": True,  # always healthy (in-process)
        }

    def stats(self) -> dict[str, Any]:
        """Return memory statistics for the dashboard."""
        graph_stats: dict[str, Any] = {}
        if hasattr(self._graph, "graph_stats"):
            graph_stats = self._graph.graph_stats()
        version_stats = self._versions.stats()
        return {
            "version_store": version_stats,
            "graph": graph_stats,
            "rules": len(self._rules.rules),
        }


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def _reciprocal_rank_fusion(
    ranked_lists: list[tuple[list[tuple[MemoryEntry, float]], float]],
    k: int = 60,
) -> list[tuple[MemoryEntry, float]]:
    """Fuse multiple ranked lists using Reciprocal Rank Fusion (RRF).

    RRF score for entry d across lists:
        RRF(d) = Σ weight_i / (k + rank_i(d))

    This is more robust than simple score averaging because it is
    insensitive to absolute score scales across different backends.

    Args:
        ranked_lists: List of (results, weight) where results is a
                      list of (MemoryEntry, score) sorted by descending score.
        k: RRF constant (default 60, standard for document retrieval).

    Returns:
        Merged and re-ranked list of (MemoryEntry, rrf_score).
    """
    rrf_scores: dict[str, float] = {}
    entry_map: dict[str, MemoryEntry] = {}

    for results, weight in ranked_lists:
        for rank, (entry, _score) in enumerate(results, start=1):
            rrf_scores[entry.id] = rrf_scores.get(entry.id, 0.0) + weight / (k + rank)
            entry_map[entry.id] = entry

    fused = [
        (entry_map[eid], score)
        for eid, score in sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    ]
    return fused
