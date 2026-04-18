"""Forge Memory — Living Collaborative Memory for multi-agent systems.

The HybridMemory fuses three layers:
  - Vector: semantic similarity via ChromaDB (default) or Qdrant
  - Graph:  entity-relationship traversal via NetworkX (default) or Neo4j
  - Symbolic: rule-based constraints and fact store (deterministic override)

Results are fused via Reciprocal Rank Fusion (RRF) for a single ranked output.
Every write is versioned with a full provenance chain for enterprise auditability.
"""

from forge_memory.hybrid import HybridMemory
from forge_memory.injection import MemoryContextInjector
from forge_memory.versioning import (
    ConflictContext,
    ConflictResolver,
    LastWriterWinsResolver,
    MemoryVersionStore,
    VectorClock,
)

__all__ = [
    "ConflictContext",
    "ConflictResolver",
    "HybridMemory",
    "LastWriterWinsResolver",
    "MemoryContextInjector",
    "MemoryVersionStore",
    "VectorClock",
]
