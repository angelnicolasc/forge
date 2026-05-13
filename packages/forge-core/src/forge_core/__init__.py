"""Forge Core — The brain of the universal agent harness.

Provides the meta-orchestrator, type system, protocol definitions,
and self-evolution loop for production multi-agent systems.
"""

from forge_core._version import __version__
from forge_core.intent import IntentClassifier, get_intent_classifier
from forge_core.types import (
    AgentCard,
    CostSummary,
    Mutation,
    MutationKind,
    RunConfig,
    RunEvent,
    RunEventKind,
    RunResult,
    RunStatus,
    TaskEnvelope,
    ToolRef,
)

__all__ = [
    "AgentCard",
    "CostSummary",
    "IntentClassifier",
    "Mutation",
    "MutationKind",
    "RunConfig",
    "RunEvent",
    "RunEventKind",
    "RunResult",
    "RunStatus",
    "TaskEnvelope",
    "ToolRef",
    "__version__",
    "get_intent_classifier",
]
