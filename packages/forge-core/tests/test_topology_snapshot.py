"""Acceptance tests for :class:`TopologySnapshot` and orchestrator swap/restore.

These are the β.1 deliverable: proving that the evolution loop can safely
swap a topology and roll back to the exact previous state without
relying on per-mutator inverse logic. The key properties:

* Snapshots are immutable (tuple-based, frozen dataclass).
* ``capture()`` produces a deep copy — mutating the source doesn't leak.
* ``content_hash`` is order-independent and stable across serialization.
* Orchestrator ``swap_topology`` + ``restore_snapshot`` round-trips
  restore the agent list, config, and prompts_map exactly.
* Rollback works for agent removal (the original ``AgentCullMutator``
  rollback bug — see L7/L8 Gap #6).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fixtures.mock_llm_adapter import MockLLMFlowAdapter

from forge_core.evolution.snapshot import TopologySnapshot
from forge_core.harness import MetaOrchestrator
from forge_core.types import AgentCard, RunConfig, TopologyState


def _agents(*names: str) -> list[AgentCard]:
    return [
        AgentCard(id=f"agent-{n}", name=n, role="worker", model="claude-haiku-4-20250514")
        for n in names
    ]


# ---------------------------------------------------------------------------
# Snapshot immutability
# ---------------------------------------------------------------------------


def test_snapshot_agents_tuple_is_immutable() -> None:
    """The snapshot's ``agents`` is a tuple, not a list — cannot be mutated."""
    snap = TopologySnapshot.capture(
        agents=_agents("a", "b"),
        config=RunConfig(),
        version=0,
    )
    assert isinstance(snap.agents, tuple)
    with pytest.raises((TypeError, AttributeError)):
        snap.agents.append(_agents("c")[0])  # type: ignore[attr-defined]


def test_snapshot_is_isolated_from_source_mutation() -> None:
    """Mutating the source list after capture must not affect the snapshot."""
    source = _agents("a", "b")
    snap = TopologySnapshot.capture(source, RunConfig(), version=0)
    source.clear()
    source.extend(_agents("x", "y", "z"))
    assert len(snap.agents) == 2
    assert {a.name for a in snap.agents} == {"a", "b"}


def test_snapshot_agent_fields_are_deep_copied() -> None:
    """Capture deep-copies agents — post-capture field edits don't leak in."""
    agents = _agents("a")
    snap = TopologySnapshot.capture(agents, RunConfig(), version=0)
    agents[0].system_prompt = "MUTATED AFTER CAPTURE"
    assert snap.agents[0].system_prompt != "MUTATED AFTER CAPTURE"


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------


def test_content_hash_is_order_independent() -> None:
    """Two snapshots with the same agents in different orders have the same hash."""
    agents_a = _agents("planner", "worker", "reviewer")
    agents_b = list(reversed(agents_a))
    snap_a = TopologySnapshot.capture(agents_a, RunConfig(), version=0)
    snap_b = TopologySnapshot.capture(agents_b, RunConfig(), version=1)
    assert snap_a.content_hash == snap_b.content_hash
    assert snap_a == snap_b  # content-based equality


def test_content_hash_differs_when_agents_change() -> None:
    snap_before = TopologySnapshot.capture(_agents("a", "b"), RunConfig(), version=0)
    snap_after = TopologySnapshot.capture(_agents("a", "b", "c"), RunConfig(), version=1)
    assert snap_before.content_hash != snap_after.content_hash
    assert snap_before != snap_after


def test_content_hash_reflects_config_changes() -> None:
    agents = _agents("a")
    cfg_60 = RunConfig(timeout_seconds=60.0)
    cfg_90 = RunConfig(timeout_seconds=90.0)
    snap_60 = TopologySnapshot.capture(agents, cfg_60, version=0)
    snap_90 = TopologySnapshot.capture(agents, cfg_90, version=0)
    assert snap_60.content_hash != snap_90.content_hash


# ---------------------------------------------------------------------------
# Serialize / deserialize round-trip
# ---------------------------------------------------------------------------


def test_serialize_round_trip() -> None:
    snap = TopologySnapshot.capture(
        agents=_agents("p", "w"),
        config=RunConfig(timeout_seconds=120.0, cost_ceiling=Decimal("5.00")),
        version=3,
        prompts_map={"sys.helper": "You are a helper"},
    )
    raw = snap.serialize()
    restored = TopologySnapshot.deserialize(raw)
    assert restored.version == snap.version
    assert restored.content_hash == snap.content_hash
    assert restored.config.timeout_seconds == 120.0
    assert restored.config.cost_ceiling == Decimal("5.00")
    assert dict(restored.prompts_map) == {"sys.helper": "You are a helper"}
    assert [a.name for a in restored.agents] == ["p", "w"]


def test_deserialize_recomputes_hash_even_if_persisted() -> None:
    """Tampering with the persisted payload is detectable via hash mismatch."""
    snap = TopologySnapshot.capture(_agents("a"), RunConfig(), version=0)
    raw = snap.serialize()
    raw["agents"][0]["name"] = "TAMPERED"
    restored = TopologySnapshot.deserialize(raw)
    # The restored snapshot's hash reflects the tampered content — it
    # does NOT preserve the old persisted hash.
    assert restored.content_hash != snap.content_hash


# ---------------------------------------------------------------------------
# TopologyState bridging
# ---------------------------------------------------------------------------


def test_from_state_and_to_state_round_trip() -> None:
    state = TopologyState(
        agents=_agents("a", "b"),
        config=RunConfig(max_steps=42),
        prompts_map={"k": "v"},
    )
    snap = TopologySnapshot.from_state(state, version=7)
    assert snap.version == 7
    rebuilt = snap.to_state()
    assert [a.name for a in rebuilt.agents] == ["a", "b"]
    assert rebuilt.config.max_steps == 42
    assert rebuilt.prompts_map == {"k": "v"}


def test_to_state_returns_independent_copy() -> None:
    """Mutating the returned state must not corrupt the snapshot."""
    snap = TopologySnapshot.capture(_agents("a"), RunConfig(), version=0)
    state = snap.to_state()
    state.agents.clear()
    state.agents.append(_agents("z")[0])
    assert [a.name for a in snap.agents] == ["a"]  # snapshot unchanged


# ---------------------------------------------------------------------------
# Orchestrator swap / restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_swap_topology_replaces_agents() -> None:
    orch = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(agent_names=("planner", "researcher"))
    orch.set_adapter(adapter)
    await adapter.load("unused")
    assert len(orch.topology()) == 2

    orch.swap_topology(_agents("solo"))
    assert len(orch.topology()) == 1
    assert orch.topology()[0].name == "solo"
    assert orch.topology_version == 1


@pytest.mark.asyncio
async def test_restore_snapshot_reverts_agent_cull() -> None:
    """Gap #6 fix: restoring a snapshot brings back culled agents.

    The old ``AgentCullMutator.rollback`` was a no-op because it had
    no way to reconstruct removed agents. Snapshot-based rollback
    sidesteps the issue entirely — we restore the full state.
    """
    orch = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(agent_names=("a", "b", "c"))
    orch.set_adapter(adapter)
    await adapter.load("unused")

    pre = orch.capture_snapshot()
    assert len(orch.topology()) == 3  # MockLLMFlowAdapter builds one per configured name

    # Simulate a cull that removes two of three agents.
    remaining = [orch.topology()[0]]
    orch.swap_topology(remaining)
    assert len(orch.topology()) == 1

    # Rollback via snapshot.
    orch.restore_snapshot(pre)
    assert len(orch.topology()) == 3
    # Topology version advances, not rewinds — restore is a forward op.
    assert orch.topology_version >= 2


@pytest.mark.asyncio
async def test_restore_preserves_config() -> None:
    orch = MetaOrchestrator()
    adapter = MockLLMFlowAdapter()
    orch.set_adapter(adapter)
    await adapter.load("unused")

    original_timeout = orch.config.default_timeout_seconds
    pre = orch.capture_snapshot()

    # Simulate a parameter tune that doubles the timeout.
    state = orch.current_state()
    state.config.timeout_seconds = original_timeout * 2
    orch.swap_topology(state)
    assert orch.config.default_timeout_seconds == original_timeout * 2

    orch.restore_snapshot(pre)
    assert orch.config.default_timeout_seconds == original_timeout


@pytest.mark.asyncio
async def test_swap_topology_requires_adapter() -> None:
    orch = MetaOrchestrator()
    with pytest.raises(RuntimeError, match="no adapter"):
        orch.swap_topology(_agents("a"))


@pytest.mark.asyncio
async def test_snapshot_version_monotonic() -> None:
    orch = MetaOrchestrator()
    adapter = MockLLMFlowAdapter()
    orch.set_adapter(adapter)
    await adapter.load("unused")

    v0 = orch.topology_version
    orch.swap_topology(_agents("x"))
    v1 = orch.topology_version
    orch.swap_topology(_agents("y"))
    v2 = orch.topology_version
    orch.restore_snapshot(orch.capture_snapshot())  # even no-op swaps tick
    v3 = orch.topology_version

    assert v0 < v1 < v2 < v3
