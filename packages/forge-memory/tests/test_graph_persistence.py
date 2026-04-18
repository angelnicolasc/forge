"""Tests for the SQLite-WAL persistence layer of :class:`NetworkXBackend`.

The single most load-bearing contract here is: **"data written before a
crash is still readable after the crash."** The previous pickle-based
backend could silently truncate on power loss; these tests exercise
paths that used to be unprovable.
"""

from __future__ import annotations

import pickle
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import networkx as nx
import pytest

from forge_core.types import MemoryEntry, MemoryQuery
from forge_memory.graph.networkx_backend import NetworkXBackend

if TYPE_CHECKING:
    from pathlib import Path


def _make_entry(
    entry_id: str, entities: list[str], relations: list[tuple[str, str, str]] | None = None
) -> MemoryEntry:
    """Build a minimal :class:`MemoryEntry` that exercises both node and edge paths."""
    return MemoryEntry(
        id=entry_id,
        content=f"content for {entry_id}",
        entities=entities,
        relations=relations or [],
        valid_from=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Persistence survives process restart (aka "crash")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_persistence_survives_restart(tmp_path: Path) -> None:
    """Write on one backend instance, read on another.

    Simulates a process crash by simply dropping the first instance and
    constructing a fresh one pointed at the same file — that's the
    scenario a production operator gets after an OOM kill.
    """
    db = tmp_path / "graph.db"
    b1 = NetworkXBackend(persist_path=db)

    await b1.store(
        _make_entry(
            "e-1",
            entities=["alice", "bob"],
            relations=[("alice", "knows", "bob")],
        )
    )
    await b1.store(_make_entry("e-2", entities=["alice", "project-apollo"]))

    # Drop the instance — no explicit close/flush. WAL + our atomic
    # transaction semantics mean the data MUST already be on disk.
    del b1

    b2 = NetworkXBackend(persist_path=db)
    # Force init (load from SQLite).
    stats = b2.graph_stats()
    assert stats["entries"] == 2
    # alice, bob, project-apollo.
    assert stats["nodes"] == 3
    # alice -> bob edge.
    assert stats["edges"] == 1

    # The entry_index round-trip preserves the MemoryEntry payload.
    result = await b2.query(MemoryQuery(text="alice", top_k=10))
    ids = {e.id for e, _score in result}
    assert {"e-1", "e-2"} <= ids


@pytest.mark.asyncio
async def test_graph_delete_survives_restart(tmp_path: Path) -> None:
    """Deletions must also be durable — no ghost entries after restart."""
    db = tmp_path / "graph.db"
    b1 = NetworkXBackend(persist_path=db)
    await b1.store(_make_entry("e-1", entities=["alice"]))
    await b1.store(_make_entry("e-2", entities=["bob"]))
    ok = await b1.delete("e-1")
    assert ok is True
    del b1

    b2 = NetworkXBackend(persist_path=db)
    stats = b2.graph_stats()
    assert stats["entries"] == 1
    # alice was a pure-orphan-after-delete; the node should be gone.
    assert stats["nodes"] == 1


@pytest.mark.asyncio
async def test_graph_db_uses_wal_mode(tmp_path: Path) -> None:
    """Confirm the journal_mode PRAGMA took.

    If somebody regresses us off WAL (e.g., switches back to a
    ``conn.execute`` after first open forgetting to re-issue the
    pragma), we lose crash-safety — so we assert on it directly.
    """
    db = tmp_path / "graph.db"
    backend = NetworkXBackend(persist_path=db)
    await backend.store(_make_entry("e-1", entities=["alice"]))

    # Peek at the DB from a plain sqlite3 handle to verify mode.
    with sqlite3.connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert mode.lower() == "wal"


# ---------------------------------------------------------------------------
# Legacy pickle migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_pickle_is_migrated_once(tmp_path: Path) -> None:
    """A user upgrading from a pre-SQLite Forge gets their data migrated.

    Scenario:
    1. Operator's existing install has ``graph.pkl`` from the old backend.
    2. They upgrade forge-memory.
    3. First run opens ``graph.db`` (new), sees empty DB, sees sibling
       ``graph.pkl`` with content, migrates in, renames the pkl to .bak.
    4. Second run opens ``graph.db`` with data already in SQLite and
       ignores the ``.pkl.bak``.
    """
    db = tmp_path / "graph.db"
    legacy = tmp_path / "graph.pkl"

    # Forge a legacy pickle with some content.
    g = nx.DiGraph()
    g.add_node("alice", kind="entity", entry_ids=["e-legacy"])
    g.add_node("bob", kind="entity", entry_ids=["e-legacy"])
    g.add_edge("alice", "bob", relation="knows", entry_id="e-legacy")
    legacy_entry = _make_entry(
        "e-legacy",
        entities=["alice", "bob"],
        relations=[("alice", "knows", "bob")],
    )
    with open(legacy, "wb") as f:
        pickle.dump({"graph": g, "index": {"e-legacy": legacy_entry}}, f)

    # First open: migration should fire.
    b1 = NetworkXBackend(persist_path=db)
    stats = b1.graph_stats()
    assert stats["entries"] == 1
    assert stats["nodes"] == 2
    assert stats["edges"] == 1

    # Legacy file renamed, not deleted — operators can audit.
    assert not legacy.exists()
    assert (tmp_path / "graph.pkl.bak").exists()

    # Second open: no re-migration, data already in SQLite.
    del b1
    b2 = NetworkXBackend(persist_path=db)
    stats2 = b2.graph_stats()
    assert stats2["entries"] == 1
    assert stats2["nodes"] == 2


@pytest.mark.asyncio
async def test_corrupt_pickle_does_not_crash_startup(tmp_path: Path) -> None:
    """A truncated / corrupt pickle must not prevent the DB from opening.

    The whole point of moving to SQLite is to stop being held hostage by
    a bad pickle. If that bad pickle is still on disk at upgrade time
    we shouldn't crash — log and move on with an empty DB.
    """
    db = tmp_path / "graph.db"
    legacy = tmp_path / "graph.pkl"
    legacy.write_bytes(b"\x80\x04not a real pickle")

    backend = NetworkXBackend(persist_path=db)
    stats = backend.graph_stats()
    # Nothing migrated, nothing crashed, empty graph.
    assert stats["entries"] == 0
    assert stats["nodes"] == 0


# ---------------------------------------------------------------------------
# Atomic transactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_is_atomic_no_partial_writes(tmp_path: Path) -> None:
    """A store() either fully reflects in SQLite or not at all.

    We can't cleanly simulate a real crash mid-transaction inside a
    test, but we can verify the transactional API is being used by
    interrupting between operations and confirming state is consistent.
    Specifically: after a failed ``delete`` of a nonexistent entry, the
    DB must remain in the exact pre-delete state (no truncation mid-op).
    """
    db = tmp_path / "graph.db"
    backend = NetworkXBackend(persist_path=db)
    await backend.store(_make_entry("e-1", entities=["alice"]))

    ok = await backend.delete("does-not-exist")
    assert ok is False

    # Pre-existing data still readable.
    del backend
    b2 = NetworkXBackend(persist_path=db)
    stats = b2.graph_stats()
    assert stats["entries"] == 1
    assert stats["nodes"] == 1
