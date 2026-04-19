# forge-memory

Living Collaborative Memory for Forge: a hybrid memory layer that combines semantic retrieval, graph traversal, and deterministic rules in one package.

`forge-memory` is the package that gives Forge cross-run memory. It stores `MemoryEntry` objects, versions every write, preserves provenance, and retrieves relevant context through a three-layer strategy:

- Vector search with ChromaDB
- Graph retrieval with NetworkX plus SQLite WAL persistence
- Symbolic filtering and reranking with a rule engine

The result is a memory system that can ingest agent output, survive restarts, handle concurrent writers, and inject relevant context back into future runs.

## What ships today

- `HybridMemory` as the main async API for store, query, ingest, delete, health checks, and stats
- `IngestionPipeline` for chunking text and extracting entities
- `MemoryContextInjector` for enriching `TaskEnvelope` instances before a run
- `MemoryVersionStore` with append-only version history and lineage queries
- Vector-clock based conflict detection for concurrent writes
- Default backends:
  - `ChromaDBBackend` for semantic retrieval
  - `NetworkXBackend` for entity and relation traversal with SQLite-backed persistence
  - `RuleEngine` for deterministic post-retrieval control

## Installation

Within the Forge workspace:

```bash
uv sync
```

As a standalone package:

```bash
pip install forge-memory
```

Optional extras declared by the package:

```bash
pip install 'forge-memory[neo4j]'
pip install 'forge-memory[qdrant]'
```

Requirements:

- Python 3.11+
- `forge-core==0.1.0`

## Quickstart

```python
import asyncio

from forge_core.types import MemoryQuery
from forge_memory import HybridMemory


async def main() -> None:
    memory = HybridMemory()

    await memory.ingest_text(
        content=(
            "The research agent found that retrieval caching reduced latency "
            "and that verified prompts improved answer quality."
        ),
        source_run_id="run-123",
        source_agent_id="researcher",
        tags={"topic": "retrieval", "verified": "true"},
        relations=[("retrieval caching", "improves", "latency")],
    )

    results = await memory.query(
        MemoryQuery(
            text="what improved retrieval latency?",
            top_k=5,
        )
    )

    for entry, score in results:
        print(f"{score:.2f} {entry.id} {entry.content}")


asyncio.run(main())
```

## Retrieval model

`HybridMemory.query()` runs three stages:

1. Query the vector backend and graph backend concurrently
2. Fuse their ranked results with Reciprocal Rank Fusion
3. Apply symbolic rules for vetoes, boosts, demotions, and tag enrichment

This makes the package useful in cases where semantic similarity alone is not enough. Graph hops can surface structurally related entries, and rules can enforce deterministic business constraints after retrieval.

## Persistence and durability

The default graph backend uses SQLite in WAL mode and keeps an in-memory `networkx.DiGraph` synchronized to disk. The current implementation is designed to preserve state across process restarts and to avoid the fragility of the older pickle-only approach.

By default, package data is persisted under the user's home directory:

- Vector store: `~/.forge/memory/chroma`
- Graph store: `~/.forge/memory/graph.db`
- Version log: `~/.forge/memory/versions.jsonl`

You can override persistence paths by constructing the backends directly and passing them into `HybridMemory`.

## Versioning and concurrent writers

Every stored `MemoryEntry` is versioned and recorded in an append-only log with provenance fields such as:

- `version`
- `supersedes`
- `source_run_id`
- `source_agent_id`
- `evidence`

For multi-writer scenarios, `MemoryVersionStore.submit_write()` uses vector clocks to classify incoming writes as:

- `before`
- `after`
- `equal`
- `concurrent`

Concurrent writes are resolved through a pluggable `ConflictResolver`. The default resolver is last-writer-wins with a deterministic writer-id tiebreak.

## Text ingestion

`IngestionPipeline` converts raw text into `MemoryEntry` objects by:

1. Splitting long text into overlapping chunks
2. Extracting entities
3. Copying metadata such as tags, evidence, relations, and source identifiers

For short content, ingestion yields a single entry. For longer content, it emits multiple chunks up to a configurable maximum.

Example:

```python
from forge_memory.ingestion import IngestionPipeline

pipeline = IngestionPipeline(chunk_size=512, chunk_overlap=64)
entries = await pipeline.ingest(
    content="Long-form report text goes here...",
    source_run_id="run-456",
    source_agent_id="writer",
    tags={"domain": "ops"},
)
```

## Context injection

`MemoryContextInjector` is the read path that closes the loop between stored memory and future runs. It queries memory, formats relevant hits, and enriches a `TaskEnvelope` with both:

- Prompt-friendly text in `memory_context`
- Structured hits in `context["forge_memory"]`

Example:

```python
import os

from forge_core.types import MemoryConfig, TaskEnvelope
from forge_memory import HybridMemory, MemoryContextInjector

os.environ["FORGE_ENABLE_MEMORY_INJECTION"] = "1"

memory = HybridMemory()
injector = MemoryContextInjector(
    memory=memory,
    config=MemoryConfig(enable_context_injection=True, top_k=3, min_relevance=0.3),
)

envelope = TaskEnvelope(input={"query": "Summarize the latest retrieval findings"})
enriched = await injector.inject(envelope)
```

Design contract:

- Injection is best-effort
- Memory failures should not abort the run
- The original envelope passes through unchanged when injection is disabled or yields no relevant hits

## Feature flags

Two optional behaviors are gated by environment flags:

- `FORGE_ENABLE_MEMORY_INJECTION=1`
  - Enables `MemoryContextInjector` to enrich envelopes before a run
- `FORGE_ENABLE_LLM_ENTITY_EXTRACTION=1`
  - Enables LLM-based entity extraction when an `llm_client` is supplied to `IngestionPipeline`

Without those flags, the package stays on the default deterministic paths:

- No context injection
- Regex-based entity extraction

## Public API

Top-level exports:

- `HybridMemory`
- `MemoryContextInjector`
- `MemoryVersionStore`
- `VectorClock`
- `ConflictResolver`
- `ConflictContext`
- `LastWriterWinsResolver`

Primary modules:

- `forge_memory.hybrid`
- `forge_memory.ingestion`
- `forge_memory.injection`
- `forge_memory.versioning`
- `forge_memory.vector.chromadb_backend`
- `forge_memory.graph.networkx_backend`
- `forge_memory.symbolic.rules`

## Notes for backend swaps

`HybridMemory` is built around structural typing rather than a hard inheritance tree. If a backend implements the expected async methods such as `store()`, `query()`, `delete()`, and `health_check()`, it can be plugged in directly.

That makes it straightforward to replace the defaults with external systems such as Qdrant or Neo4j while keeping the same high-level memory API.

## Testing

From the repository root:

```bash
pytest packages/forge-memory/tests
```

The test suite covers:

- reciprocal-rank fusion
- symbolic rules
- ingestion behavior
- version history and lineage
- vector-clock conflict handling
- graph persistence and legacy migration
- circuit-breaker behavior during backend failure
- context injection contracts

## License

Apache-2.0
