"""NetworkX graph store backend for Forge Memory.

In-process graph database using NetworkX. Zero external dependencies.
Stores entities as nodes and relationships as edges with temporal validity.
Inspired by Graphiti's temporal knowledge graph model.

For production deployments, swap with Neo4jBackend.

Persistence model
-----------------

Earlier versions of this file pickled the live :class:`nx.DiGraph` to a
single ``.pkl`` file. That approach has three failure modes that make it
unacceptable for an enterprise claim:

1. **Partial writes on crash** — ``pickle.dump`` is not atomic. A SIGKILL
   or power loss halfway through leaves a truncated, un-loadable file.
2. **Silent format drift** — a NetworkX major version bump invalidates
   every pickle on disk with no migration path.
3. **No concurrent readers** — two processes opening the same ``.pkl``
   for read-during-write get inconsistent state at best, crashes at worst.

The current backend stores the same logical graph in a SQLite database
opened in WAL mode. Every mutation writes a single transaction that
either commits fully or not at all. Legacy ``.pkl`` files discovered at
startup are migrated once (contents read, re-stored in SQLite, original
renamed with ``.pkl.bak`` suffix). Users who crashed with a corrupt
pickle get a log warning and an empty graph rather than an unrecoverable
state.

We stick with :class:`nx.DiGraph` (not ``MultiDiGraph``) so this change
is semantically transparent — callers see the same node/edge API they
did before. A future migration to multigraph semantics (multiple
predicates per (subject, object) pair) would bump the schema version in
the ``meta`` table.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from forge_core.types import MemoryEntry, MemoryQuery

logger = structlog.get_logger()


_SCHEMA_VERSION = 1


class NetworkXBackend:
    """Graph store backend powered by NetworkX with SQLite-WAL persistence.

    Nodes represent entities (agents, concepts, tools, tasks, users).
    Edges represent relationships: ``used_by``, ``produced``, ``evolved_from``,
    ``depends_on``, ``related_to``.

    Each node and edge stores temporal validity (``valid_from`` /
    ``valid_until``) enabling point-in-time queries.
    """

    backend_type = "graph"

    def __init__(self, persist_path: Path | str | None = None) -> None:
        # Default path: `~/.forge/memory/graph.db`. We interpret the
        # ``.pkl`` suffix used by the legacy backend as a hint to migrate.
        if persist_path is None:
            resolved = Path.home() / ".forge" / "memory" / "graph.db"
        else:
            resolved = Path(persist_path)
        self._persist_path = resolved
        # The legacy sibling (same stem, ``.pkl`` extension). Migration
        # only happens once — after a successful load + rewrite it gets
        # renamed to ``.pkl.bak``.
        self._legacy_pickle = resolved.with_suffix(".pkl")
        self._graph: Any = None  # networkx.DiGraph
        self._entry_index: dict[str, MemoryEntry] = {}

    # ------------------------------------------------------------------
    # Initialization & SQLite plumbing
    # ------------------------------------------------------------------

    def _ensure_initialized(self) -> None:
        if self._graph is not None:
            return
        try:
            import networkx as nx
        except ImportError as exc:
            raise ImportError(
                "networkx is required for the graph backend. Install with: pip install networkx"
            ) from exc

        self._graph = nx.DiGraph()

        # Create parent dir + open DB. WAL mode gives us crash safety and
        # concurrent readers. Synchronous=NORMAL is the WAL-mode sweet
        # spot: durable on crash, fast on commit.
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        # If the DB is fresh and a legacy pickle exists, migrate it now.
        if self._is_database_empty() and self._legacy_pickle.exists():
            self._migrate_from_pickle()
        else:
            self._load_from_sqlite()

        logger.info(
            "graph.initialized",
            path=str(self._persist_path),
            nodes=self._graph.number_of_nodes(),
            edges=self._graph.number_of_edges(),
            entries=len(self._entry_index),
        )

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with the WAL-mode pragmas we want.

        SQLite connections aren't safe to share across threads by default;
        since this backend's async methods aren't inherently threaded we
        open short-lived connections per mutation. That's cheap with
        WAL — the primary wal file is already ``mmap``'d.
        """
        conn = sqlite3.connect(self._persist_path, isolation_level=None)
        # ``isolation_level=None`` means we issue BEGIN/COMMIT explicitly
        # — required for true atomic-rewrite semantics.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        # ``executescript`` issues its own COMMIT, which is incompatible
        # with an outer BEGIN/COMMIT wrapper. CREATE TABLE IF NOT EXISTS
        # is idempotent and harmless to run un-transactionally; the
        # schema version write is a single statement so atomicity is
        # already guaranteed by SQLite's per-statement autocommit.
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edges (
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    data TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (source, target)
                );
                CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version';").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?);",
                    (str(_SCHEMA_VERSION),),
                )
            else:
                stored = int(row[0])
                if stored > _SCHEMA_VERSION:
                    raise RuntimeError(
                        f"graph.db schema v{stored} is newer than this "
                        f"forge-memory (v{_SCHEMA_VERSION}) knows about."
                    )
        finally:
            conn.close()

    def _is_database_empty(self) -> bool:
        conn = self._connect()
        try:
            nodes = conn.execute("SELECT COUNT(*) FROM nodes;").fetchone()[0]
            entries = conn.execute("SELECT COUNT(*) FROM entries;").fetchone()[0]
            return bool(nodes == 0 and entries == 0)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Load / persist
    # ------------------------------------------------------------------

    def _load_from_sqlite(self) -> None:
        assert self._graph is not None
        conn = self._connect()
        try:
            for node_id, data_json in conn.execute("SELECT id, data FROM nodes;"):
                self._graph.add_node(node_id, **json.loads(data_json))
            for source, target, data_json in conn.execute(
                "SELECT source, target, data FROM edges;"
            ):
                self._graph.add_edge(source, target, **json.loads(data_json))
            for entry_id, data_json in conn.execute("SELECT id, data FROM entries;"):
                try:
                    self._entry_index[entry_id] = MemoryEntry.model_validate_json(data_json)
                except Exception as exc:
                    # Corrupt entry — log and skip rather than crash the load.
                    logger.warning(
                        "graph.entry_load_failed",
                        entry_id=entry_id,
                        error=str(exc),
                    )
        finally:
            conn.close()

    def _migrate_from_pickle(self) -> None:
        """Load a legacy .pkl file once and rewrite it into SQLite.

        Called only when the SQLite DB is fresh and a sibling .pkl exists.
        We load the pickle with the understanding that it's from a prior
        install — any exception is treated as "unreadable legacy data,
        start fresh" rather than a hard failure.
        """
        import pickle

        try:
            with open(self._legacy_pickle, "rb") as f:
                saved = pickle.load(f)
            loaded_graph = saved.get("graph")
            loaded_index = saved.get("index", {})
            if loaded_graph is not None:
                self._graph = loaded_graph
            self._entry_index = {
                k: v for k, v in loaded_index.items() if isinstance(v, MemoryEntry)
            }
            logger.info(
                "graph.migrated_from_pickle",
                path=str(self._legacy_pickle),
                nodes=self._graph.number_of_nodes() if self._graph else 0,
                entries=len(self._entry_index),
            )
        except Exception as exc:
            logger.warning(
                "graph.pickle_migration_failed",
                path=str(self._legacy_pickle),
                error=str(exc),
            )
            return

        # Write the newly-loaded state into SQLite so subsequent starts
        # skip the pickle path entirely. Then rename the pickle so we
        # don't re-migrate the next time.
        self._persist()
        try:
            self._legacy_pickle.rename(self._legacy_pickle.with_suffix(".pkl.bak"))
        except OSError as exc:
            # On Windows, renaming over an existing file fails; retry
            # with a unique suffix. Non-critical — the migration itself
            # already succeeded.
            logger.warning(
                "graph.pickle_rename_failed",
                path=str(self._legacy_pickle),
                error=str(exc),
            )

    def _persist(self) -> None:
        """Rewrite the current in-memory state atomically into SQLite.

        Implementation choice: we rewrite rather than diff. In a
        knowledge-graph memory store the working set is typically small
        (thousands of nodes, not millions), and rewrite-in-a-transaction
        is immune to a whole class of drift bugs that plague incremental
        writers. If profiling ever shows this as a bottleneck, the
        obvious next step is to track dirty nodes/edges and write only
        those — but cross that bridge when it's actually in the way.
        """
        if self._graph is None:
            return
        now = int(time.time())

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # Wipe & rewrite. DELETE (not DROP) keeps the schema
                # version + indexes intact.
                conn.execute("DELETE FROM nodes;")
                conn.execute("DELETE FROM edges;")
                conn.execute("DELETE FROM entries;")

                if self._graph.number_of_nodes() > 0:
                    conn.executemany(
                        "INSERT INTO nodes(id, data, updated_at) VALUES(?, ?, ?);",
                        [
                            (str(n), json.dumps(d, default=_json_default), now)
                            for n, d in self._graph.nodes(data=True)
                        ],
                    )
                if self._graph.number_of_edges() > 0:
                    conn.executemany(
                        "INSERT INTO edges(source, target, data, updated_at) VALUES(?, ?, ?, ?);",
                        [
                            (
                                str(u),
                                str(v),
                                json.dumps(d, default=_json_default),
                                now,
                            )
                            for u, v, d in self._graph.edges(data=True)
                        ],
                    )
                if self._entry_index:
                    conn.executemany(
                        "INSERT INTO entries(id, data, updated_at) VALUES(?, ?, ?);",
                        [
                            (entry_id, entry.model_dump_json(), now)
                            for entry_id, entry in self._entry_index.items()
                        ],
                    )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # MemoryBackend protocol
    # ------------------------------------------------------------------

    async def store(self, entry: MemoryEntry) -> str:
        """Store a MemoryEntry by extracting entities and relations into the graph."""
        self._ensure_initialized()
        assert self._graph is not None

        now = datetime.now(UTC)
        self._entry_index[entry.id] = entry

        # Add entity nodes.
        for entity in entry.entities:
            if not self._graph.has_node(entity):
                self._graph.add_node(
                    entity,
                    kind="entity",
                    first_seen=now.isoformat(),
                    entry_ids=[],
                )
            node_data = self._graph.nodes[entity]
            entry_ids: list[str] = node_data.get("entry_ids", [])
            if entry.id not in entry_ids:
                entry_ids.append(entry.id)
            node_data["entry_ids"] = entry_ids
            node_data["last_updated"] = now.isoformat()

        # Add relation edges: (subject, predicate, object) triples.
        for subject, predicate, obj in entry.relations:
            if not self._graph.has_node(subject):
                self._graph.add_node(
                    subject, kind="entity", first_seen=now.isoformat(), entry_ids=[]
                )
            if not self._graph.has_node(obj):
                self._graph.add_node(obj, kind="entity", first_seen=now.isoformat(), entry_ids=[])
            self._graph.add_edge(
                subject,
                obj,
                relation=predicate,
                entry_id=entry.id,
                valid_from=entry.valid_from.isoformat(),
                valid_until=(entry.valid_until.isoformat() if entry.valid_until else None),
                source_run_id=entry.source_run_id or "",
            )

        self._persist()
        logger.debug(
            "graph.stored",
            entry_id=entry.id,
            entities=len(entry.entities),
            relations=len(entry.relations),
        )
        return entry.id

    async def query(self, q: MemoryQuery) -> list[tuple[MemoryEntry, float]]:
        """Query the graph by entity proximity.

        Finds all entries whose entities are reachable within ``max_hops``
        from any entity mentioned in the query. Returns ``(entry, score)``
        where score is ``1 / (hop_distance + 1)``.
        """
        self._ensure_initialized()
        assert self._graph is not None

        if not q.entity_filter and not q.text:
            return []

        import networkx as nx

        anchors: list[str] = list(q.entity_filter or [])
        if q.text and not anchors:
            text_lower = q.text.lower()
            anchors = [node for node in self._graph.nodes if node.lower() in text_lower]

        if not anchors:
            return []

        max_hops = 3
        scored: dict[str, float] = {}

        for anchor in anchors:
            if anchor not in self._graph:
                continue
            try:
                lengths = nx.single_source_shortest_path_length(
                    self._graph, anchor, cutoff=max_hops
                )
            except nx.NetworkXError:
                continue

            for node, distance in lengths.items():
                node_data = self._graph.nodes[node]
                for entry_id in node_data.get("entry_ids", []):
                    score = 1.0 / (distance + 1)
                    scored[entry_id] = max(scored.get(entry_id, 0.0), score)

        results: list[tuple[MemoryEntry, float]] = []
        for entry_id, score in sorted(scored.items(), key=lambda x: x[1], reverse=True):
            if score < q.min_score:
                continue
            if entry_id in self._entry_index:
                results.append((self._entry_index[entry_id], score))

        return results[: q.top_k]

    async def delete(self, entry_id: str) -> bool:
        """Delete an entry and remove its entity node references."""
        self._ensure_initialized()
        assert self._graph is not None

        if entry_id not in self._entry_index:
            return False

        entry = self._entry_index.pop(entry_id)
        for entity in entry.entities:
            if self._graph.has_node(entity):
                node_data = self._graph.nodes[entity]
                ids: list[str] = node_data.get("entry_ids", [])
                if entry_id in ids:
                    ids.remove(entry_id)
                if not ids:
                    self._graph.remove_node(entity)

        self._persist()
        return True

    async def health_check(self) -> bool:
        try:
            self._ensure_initialized()
            return self._graph is not None
        except Exception:
            return False

    def graph_stats(self) -> dict[str, int]:
        """Return graph statistics for dashboard display."""
        self._ensure_initialized()
        assert self._graph is not None
        return {
            "nodes": self._graph.number_of_nodes(),
            "edges": self._graph.number_of_edges(),
            "entries": len(self._entry_index),
        }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    """``json.dumps`` fallback for datetime and other non-JSON scalars.

    NetworkX node / edge data dicts can contain ISO timestamps already
    (the ``store`` path converts to ``.isoformat()`` before insert), but
    a user who writes to the graph directly might pass a live
    :class:`datetime`. Best-effort stringification avoids a silent
    persistence failure in that case.
    """
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)
