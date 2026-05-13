"""forge_spec.attester — SLSA Level 2-inspired attestation generator."""

from __future__ import annotations

import json

import yaml

from forge_spec.types import CriterionStatus, SpecAttestation, SpecDef, VerifyResult


class SpecAttester:
    """Build a SpecAttestation from a VerifyResult.

    The attestation follows SLSA provenance v1 envelope format (predicate_type +
    build_type). Cryptographic signing is deferred (DT-5).
    """

    def attest(self, spec: SpecDef, verify_result: VerifyResult) -> SpecAttestation:
        summary = verify_result.summary
        return SpecAttestation(
            spec_name=spec.name,
            spec_version=spec.version,
            spec_hash=verify_result.spec_hash,
            run_id=verify_result.run_id,
            passed=verify_result.passed,
            compliance_rate=verify_result.compliance_rate,
            passed_criteria=summary.get(CriterionStatus.PASSED, 0),
            failed_criteria=summary.get(CriterionStatus.FAILED, 0),
            skipped_criteria=summary.get(CriterionStatus.SKIPPED, 0),
            total_criteria=len(verify_result.results),
            materials=[
                {
                    "uri": f"forge://spec/{spec.name}@{spec.version}",
                    "digest": {"sha256": spec.spec_hash},
                }
            ],
        )

    def to_json(self, attestation: SpecAttestation) -> str:
        """Serialize attestation to canonical JSON."""
        return attestation.model_dump_json(indent=2)

    def to_yaml(self, attestation: SpecAttestation) -> str:
        """Serialize attestation to YAML."""
        data = attestation.model_dump(mode="json", exclude_none=True)
        return yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)

    def from_json(self, text: str) -> SpecAttestation:
        """Deserialize an attestation from JSON."""
        return SpecAttestation.model_validate(json.loads(text))
