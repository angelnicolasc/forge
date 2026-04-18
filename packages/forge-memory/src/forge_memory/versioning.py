"""Memory versioning and provenance tracking for Forge.

Every MemoryEntry write is versioned with a full provenance chain:
  - version: monotonic counter per logical entry
  - supersedes: ID of the entry this replaces (null for new facts)
  - evidence: list of entry IDs that support this fact
  - created_by: run_id or user_id that created this entry

This enables enterprise auditability: you can trace exactly how
any piece of knowledge was created, updated, or derived.

Concurrent writes (δ.2)
-----------------------

The monotonic ``version`` counter handles serial updates cleanly, but
multi-writer scenarios — two agents updating the same fact from
different runs — need a partial ordering to detect *concurrent* writes
(neither happens-before the other). We layer :class:`VectorClock` on
top: each writer keeps a counter, and comparing two clocks yields
``before``, ``after``, ``concurrent``, or ``equal``. ``concurrent``
hands off to a pluggable :class:`ConflictResolver`; the default
resolver is Last-Writer-Wins with a deterministic lexicographic
tiebreak on ``writer_id`` so two observers always agree on the winner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog

if TYPE_CHECKING:
    from forge_core.types import MemoryEntry

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Vector clocks and conflict resolution  (fase δ.2)
# ---------------------------------------------------------------------------


ClockOrdering = Literal["before", "after", "concurrent", "equal"]


@dataclass(frozen=True)
class VectorClock:
    """A vector clock over a set of named writers.

    Immutable by design — :meth:`increment` and :meth:`merge` return a
    new clock rather than mutating in place. That keeps the clocks
    safe to stash in dataclasses, event payloads, and caches without
    surprise aliasing.

    The single invariant: ``counters[writer]`` is the number of local
    events that writer has observed (either by emitting or by merging
    with an incoming clock). Comparing two clocks reduces to checking
    the counter relationships component-wise.
    """

    counters: dict[str, int] = field(default_factory=dict)

    def increment(self, writer_id: str) -> VectorClock:
        """Return a new clock with ``writer_id``'s counter bumped by one."""
        new = dict(self.counters)
        new[writer_id] = new.get(writer_id, 0) + 1
        return VectorClock(counters=new)

    def merge(self, other: VectorClock) -> VectorClock:
        """Component-wise maximum — used when absorbing an incoming event."""
        keys = set(self.counters) | set(other.counters)
        merged = {k: max(self.counters.get(k, 0), other.counters.get(k, 0)) for k in keys}
        return VectorClock(counters=merged)

    def compare(self, other: VectorClock) -> ClockOrdering:
        """Return the partial order between this clock and ``other``.

        * ``equal``     — identical counters (no divergence).
        * ``before``    — every component ≤ other's, and at least one <.
        * ``after``     — every component ≥ other's, and at least one >.
        * ``concurrent``— otherwise (each has seen events the other hasn't).
        """
        if self.counters == other.counters:
            return "equal"
        keys = set(self.counters) | set(other.counters)
        le = True
        ge = True
        for k in keys:
            a = self.counters.get(k, 0)
            b = other.counters.get(k, 0)
            if a > b:
                le = False
            if a < b:
                ge = False
            if not le and not ge:
                return "concurrent"
        if le and not ge:
            return "before"
        if ge and not le:
            return "after"
        # Reached only if counters differ but neither dominates — that
        # is the definition of concurrent.
        return "concurrent"

    def to_dict(self) -> dict[str, int]:
        return dict(self.counters)

    @classmethod
    def from_dict(cls, data: dict[str, int] | None) -> VectorClock:
        return cls(counters=dict(data or {}))


@dataclass
class ConflictContext:
    """Inputs handed to a :class:`ConflictResolver` on a concurrent write.

    Keeping this as a dataclass (rather than positional args) means
    future fields — merge hints, provenance, user overrides — don't
    break the resolver API.
    """

    incoming: MemoryEntry
    existing: MemoryEntry
    incoming_clock: VectorClock
    existing_clock: VectorClock
    incoming_writer: str
    existing_writer: str


class ConflictResolver(Protocol):
    """Pluggable strategy for resolving concurrent memory writes.

    Implementations receive both the incoming and existing entries and
    return the entry that should "win." Returning a freshly synthesized
    entry (e.g., from an LLM merge) is allowed — the store stamps it
    with the merged clock and records the resolution.
    """

    def resolve(self, ctx: ConflictContext) -> MemoryEntry: ...


class LastWriterWinsResolver:
    """Default resolver: winner = highest ``created_at``, lex tiebreak on writer.

    ``created_at`` alone is not deterministic across two observers
    because clocks drift; a second-precision tie between two runs is
    realistic. The writer-id tiebreak guarantees that if observers A
    and B both see the same two conflicting writes, they choose the
    same winner without coordination.
    """

    def resolve(self, ctx: ConflictContext) -> MemoryEntry:
        if ctx.incoming.created_at > ctx.existing.created_at:
            return ctx.incoming
        if ctx.incoming.created_at < ctx.existing.created_at:
            return ctx.existing
        # Exact tie — fall back to lexicographic writer-id ordering.
        if ctx.incoming_writer > ctx.existing_writer:
            return ctx.incoming
        return ctx.existing


class MemoryVersionStore:
    """Tracks version history for memory entries.

    Maintains an append-only log of all entry versions.
    Provides lineage queries: "show me all versions of this fact".
    """

    def __init__(
        self,
        persist_path: Path | str | None = None,
        *,
        conflict_resolver: ConflictResolver | None = None,
    ) -> None:
        self._persist_path = (
            Path(persist_path)
            if persist_path
            else Path.home() / ".forge" / "memory" / "versions.jsonl"
        )
        self._versions: list[dict[str, Any]] = []
        self._loaded = False
        # δ.2 — in-memory clock + entry cache keyed by entry_id. The
        # version store owns conflict detection because it already owns
        # the ordering invariant; backends (vector, graph) stay ignorant.
        self._clocks: dict[str, VectorClock] = {}
        self._entries: dict[str, MemoryEntry] = {}
        self._writers: dict[str, str] = {}  # entry_id -> writer that last wrote
        self._resolver: ConflictResolver = conflict_resolver or LastWriterWinsResolver()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._persist_path.exists():
            return
        try:
            with open(self._persist_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._versions.append(json.loads(line))
            logger.info("versions.loaded", count=len(self._versions))
        except Exception as exc:
            logger.warning("versions.load_failed", error=str(exc))

    def record(self, entry: MemoryEntry, operation: str = "store") -> None:
        """Record a versioned write to the append-only log."""
        self._ensure_loaded()
        record = {
            "entry_id": entry.id,
            "version": entry.version,
            "operation": operation,
            "supersedes": entry.supersedes,
            "source_run_id": entry.source_run_id,
            "source_agent_id": entry.source_agent_id,
            "evidence": entry.evidence,
            "content_hash": _hash_content(entry.content),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._versions.append(record)
        self._append_to_disk(record)
        logger.debug(
            "version.recorded",
            entry_id=entry.id,
            version=entry.version,
            operation=operation,
        )

    def lineage(self, entry_id: str) -> list[dict[str, Any]]:
        """Return the full version history for an entry ID.

        Follows the supersedes chain backwards to show how this fact evolved.
        """
        self._ensure_loaded()
        # Collect all versions touching this entry_id (direct or via supersedes chain)
        relevant = []
        target_ids = {entry_id}
        for record in reversed(self._versions):
            if record["entry_id"] in target_ids:
                relevant.append(record)
                if record.get("supersedes"):
                    target_ids.add(record["supersedes"])
        return list(reversed(relevant))

    def latest_version(self, entry_id: str) -> int:
        """Return the latest version number for an entry ID."""
        self._ensure_loaded()
        versions = [r["version"] for r in self._versions if r["entry_id"] == entry_id]
        return max(versions, default=0)

    def next_version(self, entry: MemoryEntry) -> MemoryEntry:
        """Return a copy of entry with an incremented version number."""
        current = self.latest_version(entry.id)
        return entry.model_copy(update={"version": current + 1})

    def stats(self) -> dict[str, int]:
        """Return version store statistics."""
        self._ensure_loaded()
        unique_entries = len({r["entry_id"] for r in self._versions})
        return {
            "total_writes": len(self._versions),
            "unique_entries": unique_entries,
        }

    # ------------------------------------------------------------------
    # Vector-clock aware submission  (δ.2)
    # ------------------------------------------------------------------

    def get_clock(self, entry_id: str) -> VectorClock:
        """Return the current vector clock for ``entry_id`` (empty if unseen)."""
        return self._clocks.get(entry_id, VectorClock())

    def submit_write(
        self,
        entry: MemoryEntry,
        *,
        writer_id: str,
        incoming_clock: VectorClock | None = None,
    ) -> tuple[MemoryEntry, VectorClock, ClockOrdering]:
        """Reconcile a write against the current vector clock.

        Returns a ``(winner, merged_clock, ordering)`` triple:

        * ``ordering`` is the partial order of the incoming clock vs.
          the stored clock — one of ``before``, ``after``, ``concurrent``,
          ``equal``. Callers can log or alert on ``concurrent`` writes.
        * ``winner`` is the :class:`MemoryEntry` that should be persisted
          downstream. For ``before`` / ``equal`` the incoming write is
          discarded (the stored entry is returned unchanged). For ``after``
          the incoming write replaces it. For ``concurrent`` the
          :class:`ConflictResolver` decides, and the resolution is logged.
        * ``merged_clock`` is the component-wise max of both clocks,
          with the winner's writer bumped once. Stored as the new clock.
        """
        incoming_clock = incoming_clock or VectorClock()
        existing_clock = self._clocks.get(entry.id, VectorClock())
        existing = self._entries.get(entry.id)

        # Fast path: first time we've seen this id — accept and stamp.
        if existing is None:
            stamped = incoming_clock.increment(writer_id)
            self._clocks[entry.id] = stamped
            self._entries[entry.id] = entry
            self._writers[entry.id] = writer_id
            return entry, stamped, "after"

        ordering = incoming_clock.compare(existing_clock)

        if ordering in ("before", "equal"):
            # Incoming is stale or a no-op. Discard — but still merge
            # the clock so future reads never go backwards.
            merged = existing_clock.merge(incoming_clock)
            self._clocks[entry.id] = merged
            logger.debug("version.write_stale", entry_id=entry.id, ordering=ordering)
            return existing, merged, ordering

        if ordering == "after":
            # Incoming dominates — straightforward replace.
            merged = existing_clock.merge(incoming_clock).increment(writer_id)
            self._clocks[entry.id] = merged
            self._entries[entry.id] = entry
            self._writers[entry.id] = writer_id
            return entry, merged, ordering

        # Concurrent: neither dominates. Delegate to the resolver.
        ctx = ConflictContext(
            incoming=entry,
            existing=existing,
            incoming_clock=incoming_clock,
            existing_clock=existing_clock,
            incoming_writer=writer_id,
            existing_writer=self._writers.get(entry.id, ""),
        )
        winner = self._resolver.resolve(ctx)
        merged = existing_clock.merge(incoming_clock).increment(writer_id)
        self._clocks[entry.id] = merged
        self._entries[entry.id] = winner
        # Track who "owns" the current state for future tiebreaks.
        self._writers[entry.id] = (
            writer_id if winner is entry else self._writers.get(entry.id, writer_id)
        )
        logger.info(
            "version.conflict_resolved",
            entry_id=entry.id,
            incoming_writer=writer_id,
            existing_writer=ctx.existing_writer,
            winner=(
                "incoming" if winner is entry else "existing" if winner is existing else "merged"
            ),
        )
        return winner, merged, ordering

    def _append_to_disk(self, record: dict[str, Any]) -> None:
        """Append a single record to the JSONL file."""
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._persist_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.warning("versions.write_failed", error=str(exc))


def _hash_content(content: str) -> str:
    """Return a short hash of content for change detection."""
    import hashlib

    return hashlib.sha256(content.encode()).hexdigest()[:16]
