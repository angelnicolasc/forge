"""forge_spec — Spec-Driven Development for the Forge agent harness."""

from forge_spec.attester import SpecAttester
from forge_spec.constraints import ConstraintViolationError, SpecConstraintGuard
from forge_spec.loader import (
    dump_spec_yaml,
    load_acceptance_yaml,
    load_risk_register_yaml,
    load_spec_yaml,
    merge_spec_files,
)
from forge_spec.runner import SpecRefinerProtocol, SpecRunner
from forge_spec.types import (
    AcceptanceCriterion,
    CheckType,
    CriterionResult,
    CriterionStatus,
    Risk,
    SpecAttestation,
    SpecConstraint,
    SpecDef,
    VerifyResult,
)
from forge_spec.verifier import SpecVerifier

__all__ = [
    # types
    "AcceptanceCriterion",
    "CheckType",
    "ConstraintViolationError",
    "CriterionResult",
    "CriterionStatus",
    "Risk",
    "SpecAttestation",
    "SpecConstraint",
    "SpecDef",
    "VerifyResult",
    # loader
    "dump_spec_yaml",
    "load_acceptance_yaml",
    "load_risk_register_yaml",
    "load_spec_yaml",
    "merge_spec_files",
    # verifier
    "SpecVerifier",
    # attester
    "SpecAttester",
    # constraints
    "SpecConstraintGuard",
    # runner
    "SpecRefinerProtocol",
    "SpecRunner",
]
