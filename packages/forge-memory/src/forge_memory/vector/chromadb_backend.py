"""ChromaDB vector store backend for Forge Memory.

Zero-config in-process vector store. Data persists to disk at
~/.forge/memory/chroma by default. Swap for QdrantBackend in production.
"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

import structlog

from forge_core.types import MemoryEntry, MemoryQuery

logger = structlog.get_logger()

_COLLECTION_NAME = "forge_memory"


class ChromaDBBackend:
    """Vector store backend powered by ChromaDB.

    Implements the MemoryBackend protocol (structural typing — no inheritance).
    """

    backend_type = "vector"

    def __init__(
        self,
        persist_dir: Path | str | None = None,
        embedding_model: str = "all-MiniLM-L6-v2",
        collection_name: str = _COLLECTION_NAME,
    ) -> None:
        self._persist_dir = (
            Path(persist_dir) if persist_dir else Path.home() / ".forge" / "memory" / "chroma"
        )
        self._embedding_model = embedding_model
        self._collection_name = collection_name
        self._client: Any = None
        self._collection: Any = None
        self._ef: Any = None

    async def _ensure_initialized(self) -> None:
        """Lazy initialization — avoids importing chromadb at module load time."""
        if self._client is not None:
            return
        try:
            import chromadb
            from chromadb.utils import embedding_functions
        except ImportError as exc:
            raise ImportError(
                "chromadb is required for the vector backend. "
                "Install with: pip install 'forge-memory[chromadb]' or pip install chromadb"
            ) from exc

        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self._persist_dir))
        self._ef = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=self._embedding_model
        )
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            embedding_function=self._ef,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            "chromadb.initialized",
            persist_dir=str(self._persist_dir),
            collection=self._collection_name,
        )

    async def store(self, entry: MemoryEntry) -> str:
        """Store a MemoryEntry in ChromaDB. Returns the entry ID."""
        await self._ensure_initialized()
        assert self._collection is not None

        metadata = {
            "source_run_id": entry.source_run_id or "",
            "source_agent_id": entry.source_agent_id or "",
            "version": str(entry.version),
            "supersedes": entry.supersedes or "",
            "created_at": entry.created_at.isoformat(),
            "valid_from": entry.valid_from.isoformat(),
            "valid_until": entry.valid_until.isoformat() if entry.valid_until else "",
            "entities": json.dumps(entry.entities),
            "tags": json.dumps(entry.tags),
        }

        self._collection.upsert(
            ids=[entry.id],
            documents=[entry.content],
            metadatas=[metadata],
        )
        logger.debug("vector.stored", entry_id=entry.id, content_len=len(entry.content))
        return entry.id

    async def query(self, q: MemoryQuery) -> list[tuple[MemoryEntry, float]]:
        """Query ChromaDB by semantic similarity.

        Returns list of (MemoryEntry, score) sorted by descending relevance.
        """
        await self._ensure_initialized()
        assert self._collection is not None

        if not q.text and not q.embedding:
            return []

        where_filter: dict[str, Any] | None = None
        if q.tag_filter:
            # ChromaDB where clause — simple equality filter on tags
            where_filter = (
                {"$and": [{f"tags.{k}": {"$eq": v}} for k, v in q.tag_filter.items()]}
                if len(q.tag_filter) > 1
                else {
                    f"tags.{next(iter(q.tag_filter))}": {"$eq": next(iter(q.tag_filter.values()))}
                }
            )

        query_kwargs: dict[str, Any] = {
            "n_results": min(q.top_k, self._collection.count() or 1),
            "include": ["documents", "metadatas", "distances"],
        }
        if q.embedding:
            query_kwargs["query_embeddings"] = [q.embedding]
        else:
            query_kwargs["query_texts"] = [q.text]
        if where_filter:
            query_kwargs["where"] = where_filter

        try:
            results = self._collection.query(**query_kwargs)
        except Exception as exc:
            logger.warning("vector.query_failed", error=str(exc))
            return []

        entries: list[tuple[MemoryEntry, float]] = []
        if not results["ids"] or not results["ids"][0]:
            return entries

        for idx, entry_id in enumerate(results["ids"][0]):
            distance = results["distances"][0][idx]
            score = 1.0 - distance  # cosine distance -> similarity
            if score < q.min_score:
                continue

            doc = results["documents"][0][idx]
            meta = results["metadatas"][0][idx] if results["metadatas"] else {}

            entry = _meta_to_entry(entry_id, doc, meta)
            entries.append((entry, score))

        return sorted(entries, key=lambda x: x[1], reverse=True)

    async def delete(self, entry_id: str) -> bool:
        """Delete an entry by ID."""
        await self._ensure_initialized()
        assert self._collection is not None
        try:
            self._collection.delete(ids=[entry_id])
            logger.debug("vector.deleted", entry_id=entry_id)
            return True
        except Exception:
            return False

    async def health_check(self) -> bool:
        """Check if ChromaDB is healthy."""
        try:
            await self._ensure_initialized()
            return self._client is not None
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _meta_to_entry(entry_id: str, document: str, meta: dict[str, Any]) -> MemoryEntry:
    """Reconstruct a MemoryEntry from ChromaDB metadata."""
    from datetime import datetime

    return MemoryEntry(
        id=entry_id,
        content=document,
        source_run_id=meta.get("source_run_id") or None,
        source_agent_id=meta.get("source_agent_id") or None,
        version=int(meta.get("version", 1)),
        supersedes=meta.get("supersedes") or None,
        entities=json.loads(meta.get("entities", "[]")),
        tags=json.loads(meta.get("tags", "{}")),
        created_at=datetime.fromisoformat(meta["created_at"])
        if meta.get("created_at")
        else datetime.now(UTC),
        valid_from=datetime.fromisoformat(meta["valid_from"])
        if meta.get("valid_from")
        else datetime.now(UTC),
        valid_until=datetime.fromisoformat(meta["valid_until"])
        if meta.get("valid_until")
        else None,
    )
