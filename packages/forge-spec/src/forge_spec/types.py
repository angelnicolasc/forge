"""forge_spec.types — core data models for Spec-Driven Development."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from forge_core.types import MutationKind


class CheckType(StrEnum):
    ASSERTION = "assertion"  # Python bool expression; run_result + helpers in scope
    REGEX = "regex"  # re.search(expected, str(output))
    CONTAINS = "contains"  # expected in str(output)
    LLM_JUDGE = "llm_judge"  # deferred — requires forge-spec[llm] (DT-3)
    SCRIPT = "script"  # deferred — external script execution (DT-4)


class CriterionStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"  # LLM_JUDGE / SCRIPT before their engine is wired
    ERROR = "error"  # exception during evaluation


class AcceptanceCriterion(BaseModel):
    """A single verifiable acceptance criterion for a spec."""

    id: str
    description: str
    check_type: CheckType = CheckType.ASSERTION
    expected: Any = None
    weight: float = 1.0
    tags: list[str] = Field(default_factory=list)


class CriterionResult(BaseModel):
    """Outcome of evaluating one AcceptanceCriterion."""

    criterion_id: str
    status: CriterionStatus
    message: str = ""
    actual: Any = None
    expected: Any = None


class Risk(BaseModel):
    """A risk entry in the risk register."""

    id: str
    description: str
    likelihood: float = Field(ge=0.0, le=1.0, default=0.5)
    impact: float = Field(ge=0.0, le=1.0, default=0.5)
    mitigation: str = ""

    @property
    def score(self) -> float:
        """Risk score = likelihood × impact ∈ [0, 1]."""
        return self.likelihood * self.impact


class SpecConstraint(BaseModel):
    """A constraint that gates EvolutionLoop mutations.

    If *mutation_kinds_blocked* is empty, the constraint blocks ALL mutations.
    """

    id: str
    description: str
    mutation_kinds_blocked: list[MutationKind] = Field(default_factory=list)


class SpecDef(BaseModel):
    """A complete spec definition — the single source of truth for a run's requirements."""

    name: str
    version: str = "0.1.0"
    description: str = ""
    objectives: list[str] = Field(default_factory=list)
    constraints: list[SpecConstraint] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def spec_hash(self) -> str:
        """SHA-256 hex digest (first 16 chars) of the canonical JSON representation."""
        canonical = self.model_dump_json(exclude={"created_at"})
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def criterion_by_id(self, criterion_id: str) -> AcceptanceCriterion | None:
        for c in self.acceptance_criteria:
            if c.id == criterion_id:
                return c
        return None


class VerifyResult(BaseModel):
    """Outcome of verifying a SpecDef against a RunResult."""

    spec_name: str
    spec_version: str
    spec_hash: str
    run_id: str
    results: list[CriterionResult] = Field(default_factory=list)
    compliance_rate: float = 0.0
    passed: bool = False
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {s.value: 0 for s in CriterionStatus}
        for r in self.results:
            counts[r.status] += 1
        return counts


class SpecAttestation(BaseModel):
    """SLSA Level 2-inspired provenance attestation for a verified spec run.

    Follows the SLSA provenance v1 envelope format. No cryptographic signing
    in this release (DT-5). The document can be consumed by SLSA tooling once
    signing is added.
    """

    id: str = Field(default_factory=lambda: uuid4().hex[:16])
    predicate_type: str = "https://slsa.dev/provenance/v1"
    build_type: str = "https://forge.dev/spec/v1"
    spec_name: str
    spec_version: str
    spec_hash: str
    run_id: str
    passed: bool
    compliance_rate: float
    passed_criteria: int
    failed_criteria: int
    skipped_criteria: int
    total_criteria: int
    attested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    materials: list[dict[str, Any]] = Field(default_factory=list)
