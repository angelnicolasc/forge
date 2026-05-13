"""forge_review.types — core data models for the review and policy-gate pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class FindingSeverity(StrEnum):
    P0 = "p0"  # critical  — always blocks; implies security risk or data loss
    P1 = "p1"  # error     — blocks by default; overridable with explicit config
    P2 = "p2"  # warning   — reported, does not block
    P3 = "p3"  # info      — informational only


class HookKind(StrEnum):
    ON_PLAN   = "on_plan"    # agent has produced a plan, before execution starts
    PRE_APPLY = "pre_apply"  # EvolutionLoop is about to apply a mutation
    PRE_STOP  = "pre_stop"   # orchestrator is about to cancel a running task
    PRE_MERGE = "pre_merge"  # run completed; gate before results are accepted


class Finding(BaseModel):
    """A single reviewer observation with severity and confidence."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    severity: FindingSeverity
    title: str
    message: str
    source: str = ""       # name of the reviewer that produced this finding
    location: str = ""     # e.g. "agent:planner", "field:system_prompt", or ""
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReviewResult(BaseModel):
    """Aggregated output of all reviewers for one hook invocation."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    hook: HookKind
    run_id: str = ""
    findings: list[Finding] = Field(default_factory=list)
    duration_ms: float = 0.0
    reviewers_run: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def blocking_findings(self) -> list[Finding]:
        """Findings with P0 or P1 severity."""
        return [f for f in self.findings if f.severity in (FindingSeverity.P0, FindingSeverity.P1)]

    @property
    def has_blocking(self) -> bool:
        return bool(self.blocking_findings)

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {s.value: 0 for s in FindingSeverity}
        for f in self.findings:
            counts[f.severity] += 1
        return counts


class PolicyDecision(BaseModel):
    """Output of PolicyGate.evaluate() — allows or blocks based on findings."""

    allowed: bool
    blocked_by: list[Finding] = Field(default_factory=list)
    review_result_id: str = ""
    reason: str = ""


class ReviewConfig(BaseModel):
    """Tunable policy parameters for a ReviewRunner."""

    block_on: list[FindingSeverity] = Field(
        default_factory=lambda: [FindingSeverity.P0, FindingSeverity.P1]
    )
    min_confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    # When True, LLM reviewers are skipped if heuristic reviewers produce blocking findings.
    cascade_on_blocking: bool = True


@dataclass
class ReviewContext:
    """Payload passed to every ReviewAgent.review() call.

    ``data`` holds hook-specific context — use typed helpers on ReviewRunner
    (on_plan, pre_apply, pre_merge, pre_stop) instead of building this directly.
    """

    hook: HookKind
    run_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
