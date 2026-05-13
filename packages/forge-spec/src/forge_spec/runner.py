"""forge_spec.runner — SpecRunner facade: verify + attest in one call."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from forge_spec.attester import SpecAttester
from forge_spec.constraints import SpecConstraintGuard
from forge_spec.verifier import SpecVerifier

if TYPE_CHECKING:
    from forge_core.protocols import EventBus
    from forge_core.types import RunResult
    from forge_spec.types import SpecAttestation, SpecDef, VerifyResult


@runtime_checkable
class SpecRefinerProtocol(Protocol):
    """LLM-assisted spec refinement — deferred to forge-spec[llm] (DT-2)."""

    async def refine(self, spec: SpecDef, verify_result: VerifyResult) -> SpecDef: ...


class SpecRunner:
    """Facade that wires SpecVerifier + SpecAttester into a single pipeline.

    Usage::

        runner = SpecRunner()
        verify_result, attestation = await runner.run_pipeline(spec, run_result)
    """

    def __init__(
        self,
        *,
        refiner: SpecRefinerProtocol | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._verifier = SpecVerifier(bus=bus)
        self._attester = SpecAttester()
        self._refiner = refiner

    async def run_pipeline(
        self,
        spec: SpecDef,
        run_result: RunResult,
    ) -> tuple[VerifyResult, SpecAttestation]:
        """Verify and attest in one call.

        Returns ``(VerifyResult, SpecAttestation)``.
        """
        verify_result = await self._verifier.verify(spec, run_result)
        attestation = self._attester.attest(spec, verify_result)
        return verify_result, attestation

    async def verify(self, spec: SpecDef, run_result: RunResult) -> VerifyResult:
        """Verify only — no attestation."""
        return await self._verifier.verify(spec, run_result)

    def attest(self, spec: SpecDef, verify_result: VerifyResult) -> SpecAttestation:
        """Attest only — given an existing VerifyResult."""
        return self._attester.attest(spec, verify_result)

    def guard(self, spec: SpecDef) -> SpecConstraintGuard:
        """Return a SpecConstraintGuard bound to *spec*."""
        return SpecConstraintGuard(spec)

    async def refine(self, spec: SpecDef, verify_result: VerifyResult) -> SpecDef:
        """Refine spec using injected LLM refiner; no-op if none configured."""
        if self._refiner is None:
            return spec
        return await self._refiner.refine(spec, verify_result)
