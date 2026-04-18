"""Tests for vector-clock-based concurrent write detection (fase δ.2).

The monotonic ``version: int`` counter on :class:`MemoryEntry` handles
serial updates, but multi-writer scenarios — two agents across two
runs updating the same fact — need a partial order. These tests lock
down the vector-clock primitive and the conflict-resolver contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from forge_core.types import MemoryEntry
from forge_memory.versioning import (
    ConflictContext,
    LastWriterWinsResolver,
    MemoryVersionStore,
    VectorClock,
)

# ---------------------------------------------------------------------------
# VectorClock primitive
# ---------------------------------------------------------------------------


def test_empty_clocks_are_equal() -> None:
    assert VectorClock().compare(VectorClock()) == "equal"


def test_increment_produces_after_relationship() -> None:
    c0 = VectorClock()
    c1 = c0.increment("writer_a")
    assert c0.compare(c1) == "before"
    assert c1.compare(c0) == "after"


def test_detects_concurrent_writes() -> None:
    c_a = VectorClock({"writer_a": 1})
    c_b = VectorClock({"writer_b": 1})
    assert c_a.compare(c_b) == "concurrent"
    assert c_b.compare(c_a) == "concurrent"


def test_merge_is_componentwise_max() -> None:
    c1 = VectorClock({"a": 2, "b": 1})
    c2 = VectorClock({"a": 1, "b": 3, "c": 1})
    merged = c1.merge(c2)
    assert merged.counters == {"a": 2, "b": 3, "c": 1}


def test_merged_clock_dominates_inputs() -> None:
    c1 = VectorClock({"a": 1})
    c2 = VectorClock({"b": 1})
    merged = c1.merge(c2)
    assert c1.compare(merged) == "before"
    assert c2.compare(merged) == "before"


def test_serialization_roundtrip() -> None:
    c = VectorClock({"a": 5, "b": 2})
    restored = VectorClock.from_dict(c.to_dict())
    assert restored == c


def test_immutable_increment() -> None:
    c = VectorClock({"a": 1})
    c.increment("a")
    # Original unchanged.
    assert c.counters == {"a": 1}


# ---------------------------------------------------------------------------
# MemoryVersionStore.submit_write
# ---------------------------------------------------------------------------


def _entry(eid: str, content: str, when: datetime | None = None) -> MemoryEntry:
    return MemoryEntry(
        id=eid,
        content=content,
        created_at=when or datetime.now(UTC),
        valid_from=when or datetime.now(UTC),
    )


def test_first_write_is_accepted(tmp_path) -> None:
    store = MemoryVersionStore(persist_path=tmp_path / "v.jsonl")
    winner, clock, ordering = store.submit_write(_entry("e1", "first"), writer_id="w1")
    assert winner.content == "first"
    assert ordering == "after"
    assert clock.counters == {"w1": 1}


def test_sequential_write_from_same_writer_replaces(tmp_path) -> None:
    store = MemoryVersionStore(persist_path=tmp_path / "v.jsonl")
    _, c1, _ = store.submit_write(_entry("e1", "v1"), writer_id="w1")
    # Writer increments its local clock to signal a new event before
    # submitting — this is the normal happens-after protocol.
    winner, c2, ordering = store.submit_write(
        _entry("e1", "v2"), writer_id="w1", incoming_clock=c1.increment("w1")
    )
    assert ordering == "after"
    assert winner.content == "v2"
    assert c2.counters == {"w1": 3}


def test_stale_replay_write_is_rejected(tmp_path) -> None:
    """A replay (identical clock to a prior write) must not overwrite."""
    store = MemoryVersionStore(persist_path=tmp_path / "v.jsonl")
    _, c1, _ = store.submit_write(_entry("e1", "v1"), writer_id="w1")
    # Same writer replays with the same clock snapshot (no local bump) —
    # the store must recognise this as stale/equal and keep v1.
    winner, _c2, ordering = store.submit_write(
        _entry("e1", "replay"), writer_id="w1", incoming_clock=c1
    )
    assert ordering == "equal"
    assert winner.content == "v1"


def test_concurrent_writes_go_through_resolver(tmp_path) -> None:
    store = MemoryVersionStore(persist_path=tmp_path / "v.jsonl")
    _, c0, _ = store.submit_write(_entry("e1", "base"), writer_id="seed")

    # Two writers both start from c0 and independently advance.
    later = datetime.now(UTC) + timedelta(seconds=1)
    _winner, _merged, ordering = store.submit_write(
        _entry("e1", "branch_a", when=later),
        writer_id="alice",
        incoming_clock=c0.increment("alice"),
    )
    assert ordering == "after"  # still dominates base

    # Now an independent branch from bob — his clock has c0 + bob, but
    # the store already advanced to alice+seed — the two are concurrent.
    winner2, _merged2, ordering2 = store.submit_write(
        _entry("e1", "branch_b", when=later + timedelta(seconds=1)),
        writer_id="bob",
        incoming_clock=c0.increment("bob"),
    )
    assert ordering2 == "concurrent"
    # LWW: bob's timestamp wins.
    assert winner2.content == "branch_b"


def test_lww_tiebreak_is_deterministic() -> None:
    resolver = LastWriterWinsResolver()
    when = datetime.now(UTC)
    incoming = _entry("e1", "I", when=when)
    existing = _entry("e1", "E", when=when)
    # Identical timestamps — lex tiebreak picks the higher writer_id.
    ctx = ConflictContext(
        incoming=incoming,
        existing=existing,
        incoming_clock=VectorClock({"alice": 1}),
        existing_clock=VectorClock({"bob": 1}),
        incoming_writer="alice",
        existing_writer="bob",
    )
    # "bob" > "alice" lexicographically → existing wins.
    winner = resolver.resolve(ctx)
    assert winner is existing

    ctx2 = ConflictContext(
        incoming=incoming,
        existing=existing,
        incoming_clock=VectorClock({"zeta": 1}),
        existing_clock=VectorClock({"bob": 1}),
        incoming_writer="zeta",
        existing_writer="bob",
    )
    # "zeta" > "bob" → incoming wins.
    assert resolver.resolve(ctx2) is incoming


def test_custom_resolver_can_be_injected(tmp_path) -> None:
    class _AlwaysExisting:
        def resolve(self, ctx: ConflictContext) -> MemoryEntry:
            return ctx.existing

    store = MemoryVersionStore(
        persist_path=tmp_path / "v.jsonl",
        conflict_resolver=_AlwaysExisting(),
    )
    _, c0, _ = store.submit_write(_entry("e1", "base"), writer_id="w0")
    store.submit_write(
        _entry("e1", "alice"), writer_id="alice", incoming_clock=c0.increment("alice")
    )
    winner, _, ordering = store.submit_write(
        _entry("e1", "bob"), writer_id="bob", incoming_clock=c0.increment("bob")
    )
    assert ordering == "concurrent"
    assert winner.content == "alice"  # existing preserved by custom resolver
