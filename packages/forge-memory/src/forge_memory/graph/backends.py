"""Graph backend protocol and Neo4j interface stub.

Defines ``GraphBackend``, the structural typing contract for all graph stores
used by :class:`~forge_memory.hybrid.HybridMemory`.  Any class that implements
the methods below satisfies the protocol — no inheritance required.

Backend interfaces:
  - :class:`NetworkXBackend` uses NetworkX with SQLite-WAL persistence on one host.
  - :class:`Neo4jGraphBackend` reserves the interface for a future backend.

Why a separate protocol rather than reusing ``forge_core.protocols.MemoryBackend``?
The ``MemoryBackend`` protocol expresses the *query* surface (store / query /
delete / health_check).  ``GraphBackend`` adds graph-specific traversal and
introspection operations that only make sense for a graph store, and that
callers of the memory API never need to call directly.  Keeping them separate
means graph clients can depend on the rich interface while the rest of the
codebase stays coupled only to the simpler ``MemoryBackend`` contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from forge_core.types import MemoryEntry, MemoryQuery


@runtime_checkable
class GraphBackend(Protocol):
    """Structural protocol for graph memory backends.

    Combines the generic MemoryBackend surface (store / query / delete /
    health_check) with graph-specific traversal and persistence operations.
    ``@runtime_checkable`` lets callers do ``isinstance(backend, GraphBackend)``
    for discovery and introspection without importing concrete classes.
    """

    # -- MemoryBackend surface ------------------------------------------------

    @property
    def backend_type(self) -> str:
        """Must return ``'graph'``."""
        ...

    async def store(self, entry: MemoryEntry) -> str:
        """Store a MemoryEntry in the graph. Returns the entry ID."""
        ...

    async def query(self, q: MemoryQuery) -> list[tuple[MemoryEntry, float]]:
        """Return (entry, score) pairs sorted by descending relevance."""
        ...

    async def delete(self, entry_id: str) -> bool:
        """Delete an entry by ID. Returns True if found and removed."""
        ...

    async def health_check(self) -> bool:
        """Return True if the backend is reachable and operational."""
        ...

    # -- Graph-specific surface -----------------------------------------------

    def graph_stats(self) -> dict[str, Any]:
        """Return node/edge counts and other store-specific diagnostics."""
        ...

    async def add_node(self, node_id: str, properties: dict[str, Any]) -> None:
        """Add or update a named node with arbitrary properties."""
        ...

    async def add_edge(
        self,
        src: str,
        dst: str,
        relation: str,
        properties: dict[str, Any],
    ) -> None:
        """Add or update a directed edge (src → dst) with a relation label."""
        ...

    async def traverse(
        self,
        start: str,
        max_depth: int,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """BFS/DFS from ``start`` up to ``max_depth`` hops.

        ``filters`` is a store-specific predicate dict (e.g. ``{"relation":
        "CITES"}``).  Returns a list of node-property dicts in traversal order.
        """
        ...

    async def get_neighbors(self, node_id: str, *, direction: str = "both") -> list[str]:
        """Return IDs of nodes adjacent to ``node_id``.

        ``direction`` is ``'in'``, ``'out'``, or ``'both'``.
        """
        ...

    async def persist(self) -> None:
        """Flush any in-memory state to durable storage.

        ``NetworkXBackend`` auto-persists on every write; implementations
        that batch writes should flush here on graceful shutdown.
        """
        ...


class Neo4jGraphBackend:
    """Reserved interface for a Neo4j graph backend."""

    _MSG = "Neo4jGraphBackend is not yet implemented. Use NetworkXBackend for local storage."

    @property
    def backend_type(self) -> str:
        return "graph"

    async def store(self, entry: MemoryEntry) -> str:
        raise NotImplementedError(self._MSG)

    async def query(self, q: MemoryQuery) -> list[tuple[MemoryEntry, float]]:
        raise NotImplementedError(self._MSG)

    async def delete(self, entry_id: str) -> bool:
        raise NotImplementedError(self._MSG)

    async def health_check(self) -> bool:
        raise NotImplementedError(self._MSG)

    def graph_stats(self) -> dict[str, Any]:
        raise NotImplementedError(self._MSG)

    async def add_node(self, node_id: str, properties: dict[str, Any]) -> None:
        raise NotImplementedError(self._MSG)

    async def add_edge(self, src: str, dst: str, relation: str, properties: dict[str, Any]) -> None:
        raise NotImplementedError(self._MSG)

    async def traverse(
        self, start: str, max_depth: int, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(self._MSG)

    async def get_neighbors(self, node_id: str, *, direction: str = "both") -> list[str]:
        raise NotImplementedError(self._MSG)

    async def persist(self) -> None:
        raise NotImplementedError(self._MSG)
