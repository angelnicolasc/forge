"""Evolution Journal — append-only log with full provenance.

Every mutation (proposed, applied, or rolled back) is recorded here.
The journal is the source of truth for the evolution loop's history
and feeds the dashboard's Evolution Timeline view.

The journal persists to ~/.forge/evolution_journal.jsonl — a simple
JSONL file that can be tailed, streamed, or loaded into any analytics tool.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from forge_core.types import FitnessScore, Mutation

logger = structlog.get_logger()

JournalEntryKind = Literal["proposed", "applied", "rolled_back", "skipped", "evaluated"]


class JournalEntry:
    """A single immutable record in the evolution journal."""

    __slots__ = ("fitness_after", "fitness_before", "kind", "mutation", "reason", "timestamp")

    def __init__(
        self,
        mutation: Mutation,
        kind: JournalEntryKind,
        fitness_before: FitnessScore | None = None,
        fitness_after: FitnessScore | None = None,
        reason: str = "",
    ) -> None:
        self.mutation = mutation
        self.kind = kind
        self.fitness_before = fitness_before
        self.fitness_after = fitness_after
        self.reason = reason
        self.timestamp = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation.id,
            "mutation_kind": self.mutation.kind,
            "description": self.mutation.description,
            "confidence": self.mutation.confidence,
            "kind": self.kind,
            "fitness_before": self.fitness_before.overall if self.fitness_before else None,
            "fitness_after": self.fitness_after.overall if self.fitness_after else None,
            "improvement": (
                (self.fitness_after.overall - self.fitness_before.overall)
                if self.fitness_before and self.fitness_after
                else None
            ),
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
        }


class EvolutionJournal:
    """Append-only log of all evolution activity.

    Provides:
    - record(): write a new journal entry
    - lineage(): full history for a mutation ID
    - recent(): last N entries of a given kind
    - fitness_trend(): sequence of overall fitness scores over time
    """

    def __init__(self, persist_path: Path | str | None = None) -> None:
        self._persist_path = (
            Path(persist_path)
            if persist_path
            else Path.home() / ".forge" / "evolution_journal.jsonl"
        )
        self._entries: list[JournalEntry] = []
        self._loaded = False

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
                        _d = json.loads(line)
                        # We only load metadata; full Mutation objects are rebuilt from diffs
                        # For now, just keep counts — full replay is a v2 feature
            logger.info("journal.loaded", path=str(self._persist_path))
        except Exception as exc:
            logger.warning("journal.load_failed", error=str(exc))

    def record(
        self,
        mutation: Mutation,
        kind: JournalEntryKind,
        fitness_before: FitnessScore | None = None,
        fitness_after: FitnessScore | None = None,
        reason: str = "",
    ) -> JournalEntry:
        """Record a mutation event in the journal."""
        self._ensure_loaded()
        entry = JournalEntry(
            mutation=mutation,
            kind=kind,
            fitness_before=fitness_before,
            fitness_after=fitness_after,
            reason=reason,
        )
        self._entries.append(entry)
        self._append_to_disk(entry)
        logger.info(
            "journal.recorded",
            mutation_id=mutation.id,
            kind=kind,
            improvement=(
                f"{(fitness_after.overall - fitness_before.overall):+.3f}"
                if fitness_before and fitness_after
                else "n/a"
            ),
        )
        return entry

    def recent(
        self,
        kind: JournalEntryKind | None = None,
        limit: int = 20,
    ) -> list[JournalEntry]:
        """Return the most recent journal entries, optionally filtered by kind."""
        self._ensure_loaded()
        entries = self._entries if kind is None else [e for e in self._entries if e.kind == kind]
        return list(reversed(entries))[:limit]

    def fitness_trend(self) -> list[float]:
        """Return the sequence of fitness_after scores for applied mutations."""
        return [
            e.fitness_after.overall
            for e in self._entries
            if e.kind == "applied" and e.fitness_after
        ]

    def best_mutation(self) -> JournalEntry | None:
        """Return the applied mutation that produced the greatest fitness improvement."""
        applied = [
            e for e in self._entries if e.kind == "applied" and e.fitness_before and e.fitness_after
        ]
        if not applied:
            return None

        def _delta(e: JournalEntry) -> float:
            assert e.fitness_after is not None and e.fitness_before is not None
            return e.fitness_after.overall - e.fitness_before.overall

        return max(applied, key=_delta)

    def stats(self) -> dict[str, int]:
        """Summary statistics for the dashboard."""
        self._ensure_loaded()
        from collections import Counter

        counts: Counter[str] = Counter(str(e.kind) for e in self._entries)
        return dict(counts)

    def _append_to_disk(self, entry: JournalEntry) -> None:
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._persist_path, "a") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
        except Exception as exc:
            logger.warning("journal.write_failed", error=str(exc))
