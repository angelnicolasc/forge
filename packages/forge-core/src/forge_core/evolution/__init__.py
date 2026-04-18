"""Forge Self-Evolution Engine.

Biology-inspired cycle: Observe -> Hypothesize -> Mutate -> Evaluate -> Commit/Rollback.
"""

from forge_core.evolution.eval_suite import EvalCase, EvalSuite
from forge_core.evolution.snapshot import TopologySnapshot

__all__ = ["EvalCase", "EvalSuite", "TopologySnapshot"]
