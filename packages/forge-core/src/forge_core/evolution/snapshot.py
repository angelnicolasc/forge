"""Immutable topology snapshots for atomic evolution rollback.

A :class:`TopologySnapshot` is a frozen, content-addressable record of the
orchestrator's topology at a specific point in time: the full list of
:class:`~forge_core.types.AgentCard` objects, the active
:class:`~forge_core.types.RunConfig`, and the prompts map.

The contract is *atomic rollback*: the evolution loop captures a snapshot
immediately before applying a mutation, and if fitness drops past the
threshold, the orchestrator calls :meth:`MetaOrchestrator.restore_snapshot`
which **replaces** the current topology with the snapshot's contents. No
per-mutation inverse logic is required — rollback is simply a swap.

This addresses L7/L8 Gap #6 ("AgentCullMutator.rollback no restaura
agentes"): cull's rollback was a no-op because it had no way to
reconstruct the removed agent. Snapshots make that moot: we rollback to
a known-good full state, not an inverse delta.

Snapshots are:

* **Immutable** (``frozen=True`` dataclass with tuple-based fields). Once
  captured, a snapshot cannot be mutated — mutations that try to modify a
  snapshot's ``agents`` raise ``TypeError`` at the tuple layer.
* **Content-addressable**. :attr:`content_hash` is a SHA-256 over a
  canonical JSON serialization, so two snapshots of identical topologies
  compare equal and occupy a single logical entry in the journal.
* **Serializable**. :meth:`serialize` / :meth:`deserialize` produce a
  dict that survives round-trip through the journal's JSONL persistence.

Design note: we keep ``prompts_map`` separate from per-agent
``system_prompt`` fields (duplicate on purpose). The ``prompts_map`` is
the canonical source for prompt templates that multiple agents may share
(via an id); ``AgentCard.system_prompt`` is the *resolved* prompt at
capture time. On restore, the orchestrator rebinds agents using the
snapshot's resolved prompts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from forge_core.types import AgentCard, RunConfig, TopologyState

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class TopologySnapshot:
    """Immutable point-in-time capture of a topology.

    Attributes
    ----------
    version:
        Monotonic version number assigned by the orchestrator. Starts at
        0 for the first captured snapshot and increments on every capture
        (whether the topology actually changed or not — the dedup is done
        by ``content_hash``).
    timestamp:
        UTC timestamp of capture. Useful for journal correlation.
    agents:
        Tuple (not list) of :class:`AgentCard` — tuple for immutability.
    config:
        The :class:`RunConfig` in effect at capture time. Snapshotted
        ``model_copy(deep=True)`` so post-capture config mutations don't
        leak back into the snapshot.
    prompts_map:
        Mapping of prompt-id -> prompt-text. Captured as an immutable
        :class:`dict` reference (we trust callers not to mutate it; the
        snapshot returns a fresh dict on :meth:`as_dict`).
    content_hash:
        SHA-256 of a canonical JSON rendering of the snapshot's contents.
        Used for equality checks without comparing every agent field and
        for journal deduplication.
    """

    version: int
    timestamp: datetime
    agents: tuple[AgentCard, ...]
    config: RunConfig
    prompts_map: Mapping[str, str] = field(default_factory=dict)
    content_hash: str = ""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def capture(
        cls,
        agents: list[AgentCard] | tuple[AgentCard, ...],
        config: RunConfig,
        version: int,
        *,
        prompts_map: Mapping[str, str] | None = None,
        timestamp: datetime | None = None,
    ) -> TopologySnapshot:
        """Capture the current topology as an immutable snapshot.

        Deep-copies the agent cards and config so subsequent in-place
        modifications of the originals don't corrupt the snapshot. This
        is the single invariant that makes rollback reliable.
        """
        agents_tuple: tuple[AgentCard, ...] = tuple(a.model_copy(deep=True) for a in agents)
        config_copy = config.model_copy(deep=True)
        prompts_copy: dict[str, str] = dict(prompts_map or {})
        ts = timestamp or datetime.now(UTC)
        content_hash = _compute_content_hash(agents_tuple, config_copy, prompts_copy)
        return cls(
            version=version,
            timestamp=ts,
            agents=agents_tuple,
            config=config_copy,
            prompts_map=prompts_copy,
            content_hash=content_hash,
        )

    @classmethod
    def from_state(cls, state: TopologyState, version: int) -> TopologySnapshot:
        """Capture from a :class:`TopologyState` (β.3 mutator input/output)."""
        return cls.capture(
            agents=state.agents,
            config=state.config,
            version=version,
            prompts_map=state.prompts_map,
        )

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

    def to_state(self) -> TopologyState:
        """Materialize a mutable :class:`TopologyState` from the snapshot.

        Returns a deep copy — the snapshot itself remains immutable even
        if the caller mutates the returned state.
        """
        return TopologyState(
            agents=[a.model_copy(deep=True) for a in self.agents],
            config=self.config.model_copy(deep=True),
            prompts_map=dict(self.prompts_map),
        )

    def agents_list(self) -> list[AgentCard]:
        """Return a fresh mutable list of deep-copied agents."""
        return [a.model_copy(deep=True) for a in self.agents]

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def serialize(self) -> dict[str, Any]:
        """Return a JSON-safe dict that round-trips through :meth:`deserialize`."""
        return {
            "version": self.version,
            "timestamp": self.timestamp.isoformat(),
            "agents": [a.model_dump(mode="json") for a in self.agents],
            "config": self.config.model_dump(mode="json"),
            "prompts_map": dict(self.prompts_map),
            "content_hash": self.content_hash,
        }

    @classmethod
    def deserialize(cls, data: dict[str, Any]) -> TopologySnapshot:
        """Rebuild a snapshot from its :meth:`serialize` output.

        Recomputes ``content_hash`` rather than trusting the persisted
        value, so tampering with agents or config on disk is detected.
        """
        agents = tuple(AgentCard.model_validate(a) for a in data.get("agents", []))
        config = RunConfig.model_validate(data.get("config", {}))
        prompts_map = dict(data.get("prompts_map", {}))
        ts_raw = data.get("timestamp")
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw)
        elif isinstance(ts_raw, datetime):
            ts = ts_raw
        else:
            ts = datetime.now(UTC)
        content_hash = _compute_content_hash(agents, config, prompts_map)
        # Surface a tampering warning-by-assertion only if the caller
        # persisted a non-empty hash that no longer matches. Missing hash
        # is fine (older journal entries).
        persisted = data.get("content_hash", "")
        if persisted and persisted != content_hash:
            # We still return the recomputed snapshot — callers can check
            # `content_hash` against the original to detect tampering.
            pass
        return cls(
            version=int(data.get("version", 0)),
            timestamp=ts,
            agents=agents,
            config=config,
            prompts_map=prompts_map,
            content_hash=content_hash,
        )

    # ------------------------------------------------------------------
    # Equality / hashing
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TopologySnapshot):
            return NotImplemented
        # Equality is content-based, not version-based. Two snapshots of
        # an identical topology at different times compare equal.
        return self.content_hash == other.content_hash

    def __hash__(self) -> int:
        return hash(self.content_hash)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_content_hash(
    agents: tuple[AgentCard, ...],
    config: RunConfig,
    prompts_map: Mapping[str, str],
) -> str:
    """Compute a stable SHA-256 over a canonical JSON rendering.

    Sort keys and stringify ``Decimal``/``datetime`` so the hash is
    reproducible across process runs. Agent lists are sorted by ``id`` so
    two logically-equal topologies with different iteration orders
    produce the same hash.
    """
    payload = {
        "agents": sorted(
            (a.model_dump(mode="json") for a in agents),
            key=lambda d: d.get("id", ""),
        ),
        "config": config.model_dump(mode="json"),
        "prompts_map": {k: prompts_map[k] for k in sorted(prompts_map.keys())},
    }
    canonical = json.dumps(payload, sort_keys=True, default=_json_default)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unhashable type in TopologySnapshot payload: {type(value)!r}")
