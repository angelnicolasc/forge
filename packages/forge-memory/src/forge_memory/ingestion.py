"""Ingestion pipeline for Forge Memory.

Converts raw text / agent output into MemoryEntry objects via:
  1. Chunking — split long text into overlapping windows
  2. Embedding — compute vector embeddings (lazy, via sentence-transformers)
  3. Entity extraction — naive NER for graph indexing
  4. Deduplication — detect near-duplicate entries before storage

Usage:
    pipeline = IngestionPipeline()
    entries = await pipeline.ingest(
        content="The research agent found three papers on RAG...",
        source_run_id="abc123",
        source_agent_id="researcher",
        tags={"topic": "RAG"},
    )
    for entry in entries:
        await memory.store(entry)
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, Field

from forge_core.types import MemoryEntry

logger = structlog.get_logger()

_LLM_FLAG = "FORGE_ENABLE_LLM_ENTITY_EXTRACTION"
_LLM_MODEL = "claude-haiku-4-20250514"


def _llm_flag_enabled() -> bool:
    val = os.environ.get(_LLM_FLAG, "").strip().lower()
    return val in {"1", "true", "yes", "on"}


class EntityExtractionTool(BaseModel):
    """Structured-output schema for the LLM entity extractor.

    We ask for a bounded list so one bad chunk can't produce a 500-item
    entity explosion. ``type`` is free-form because different domains
    care about different ontologies; the graph layer uses it only as a
    node attribute and doesn't validate the vocabulary.
    """

    entities: list[str] = Field(default_factory=list, max_length=40)


# Simple regex-based entity patterns (upgrade to spaCy for production)
_ENTITY_PATTERNS = [
    re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b"),  # Person names (naive)
    re.compile(r"\b[A-Z]{2,}\b"),  # Acronyms
    re.compile(r"\bgpt-\d+\w*\b", re.IGNORECASE),  # Model names
    re.compile(r"\bclaude-\S+\b", re.IGNORECASE),
    re.compile(r"\bgemini-\S+\b", re.IGNORECASE),
    re.compile(r"\b\w+Agent\b"),  # Agent names
    re.compile(r"\b\w+Tool\b"),  # Tool names
]


class IngestionPipeline:
    """Converts raw content into versioned MemoryEntry objects.

    Configurable chunk size, overlap, and entity extraction strategy.
    """

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        max_chunks_per_doc: int = 20,
        llm_client: Any | None = None,
    ) -> None:
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._max_chunks = max_chunks_per_doc
        # δ.4 — if a client is supplied AND the env flag is on, we route
        # entity extraction through an LLM. Regex remains the fallback
        # path and the default for anyone who doesn't opt in.
        self._llm_client = llm_client

    async def ingest(
        self,
        content: str,
        source_run_id: str | None = None,
        source_agent_id: str | None = None,
        tags: dict[str, str] | None = None,
        evidence: list[str] | None = None,
        relations: list[tuple[str, str, str]] | None = None,
        valid_until: datetime | None = None,
    ) -> list[MemoryEntry]:
        """Ingest content and return a list of MemoryEntry objects ready to store.

        For short content (<= chunk_size), returns a single entry.
        For long content, splits into overlapping chunks.
        """
        content = content.strip()
        if not content:
            return []

        chunks = self._chunk(content)
        entries: list[MemoryEntry] = []

        for chunk in chunks:
            entities = await _extract_entities_async(chunk, self._llm_client)
            entry = MemoryEntry(
                content=chunk,
                entities=entities,
                relations=relations or [],
                source_run_id=source_run_id,
                source_agent_id=source_agent_id,
                tags=tags or {},
                evidence=evidence or [],
                valid_from=datetime.now(UTC),
                valid_until=valid_until,
            )
            entries.append(entry)
            logger.debug(
                "ingestion.chunk_created",
                entry_id=entry.id,
                entities=len(entities),
                chunk_len=len(chunk),
            )

        logger.info(
            "ingestion.complete",
            chunks=len(entries),
            source_run_id=source_run_id,
        )
        return entries

    def _chunk(self, text: str) -> list[str]:
        """Split text into overlapping chunks."""
        if len(text) <= self._chunk_size:
            return [text]

        chunks: list[str] = []
        start = 0
        while start < len(text) and len(chunks) < self._max_chunks:
            end = min(start + self._chunk_size, len(text))
            # Try to break at sentence boundary
            if end < len(text):
                boundary = text.rfind(". ", start, end)
                if boundary > start + self._chunk_size // 2:
                    end = boundary + 1
            chunks.append(text[start:end].strip())
            start = end - self._chunk_overlap
            if start >= len(text):
                break

        return [c for c in chunks if c]


def _extract_entities(text: str) -> list[str]:
    """Regex fallback entity extraction. Cheap, deterministic, shallow."""
    entities: set[str] = set()
    for pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            entity = match.group().strip()
            if len(entity) >= 2:
                entities.add(entity)
    return sorted(entities)


async def _extract_entities_async(
    text: str,
    llm_client: Any | None,
) -> list[str]:
    """Extract entities, routing through the LLM when enabled.

    Contract:

    * If the ``FORGE_ENABLE_LLM_ENTITY_EXTRACTION`` flag is unset or
      ``llm_client`` is ``None``, fall back to the regex extractor.
    * If the LLM call raises for any reason — rate-limit, bad schema,
      network down — log a warning and fall back to the regex path.
      Ingestion must never hard-fail because of a flaky extractor.
    """
    if llm_client is None or not _llm_flag_enabled():
        return _extract_entities(text)
    try:
        response = await llm_client.structured_call(
            model=_LLM_MODEL,
            schema=EntityExtractionTool,
            system=(
                "Extract named entities — people, organizations, products, "
                "tools, models, concepts — from the input. Return a bounded "
                "list of short strings (names only, no descriptions)."
            ),
            prompt=f"TEXT:\n{text[:4000]}",
            max_tokens=512,
            temperature=0.0,
        )
        entities = [e.strip() for e in response.entities if e and e.strip()]
        # Dedupe preserving order — LLMs occasionally emit case-variants.
        seen: set[str] = set()
        ordered: list[str] = []
        for e in entities:
            key = e.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(e)
        return ordered
    except Exception as exc:
        logger.warning("entity_extraction.llm_failed_fallback", error=str(exc))
        return _extract_entities(text)
