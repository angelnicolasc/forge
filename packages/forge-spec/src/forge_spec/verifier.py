"""forge_spec.verifier — evaluate acceptance criteria against a RunResult."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import structlog

from forge_core.types import RunEvent, RunEventKind, RunResult

from forge_spec.types import (
    AcceptanceCriterion,
    CheckType,
    CriterionResult,
    CriterionStatus,
    SpecDef,
    VerifyResult,
)

if TYPE_CHECKING:
    from forge_core.protocols import EventBus

log = structlog.get_logger(__name__)

# Safe builtins available inside ASSERTION expressions.
_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "round": round,
    "set": set,
    "str": str,
    "sum": sum,
    "tuple": tuple,
}


class SpecVerifier:
    """Evaluate a SpecDef against a RunResult and produce a VerifyResult."""

    def __init__(self, *, bus: "EventBus | None" = None) -> None:
        self._bus = bus

    async def verify(self, spec: SpecDef, run_result: RunResult) -> VerifyResult:
        results: list[CriterionResult] = []
        for criterion in spec.acceptance_criteria:
            result = self._evaluate(criterion, run_result)
            results.append(result)
            log.debug(
                "criterion_evaluated",
                criterion_id=criterion.id,
                status=result.status,
            )

        compliance_rate = _compute_compliance(results, spec.acceptance_criteria)
        passed = compliance_rate >= 1.0 or (
            all(
                r.status in (CriterionStatus.PASSED, CriterionStatus.SKIPPED)
                for r in results
            )
            and any(r.status == CriterionStatus.PASSED for r in results)
        )

        verify_result = VerifyResult(
            spec_name=spec.name,
            spec_version=spec.version,
            spec_hash=spec.spec_hash,
            run_id=run_result.run_id,
            results=results,
            compliance_rate=compliance_rate,
            passed=passed,
        )

        if self._bus is not None:
            await self._bus.publish(
                RunEvent(
                    kind=RunEventKind.SPEC_VERIFIED,
                    run_id=run_result.run_id,
                    data={
                        "spec": spec.name,
                        "compliance_rate": compliance_rate,
                        "passed": passed,
                    },
                )
            )

        return verify_result

    # ------------------------------------------------------------------
    # Internal evaluation dispatch
    # ------------------------------------------------------------------

    def _evaluate(
        self, criterion: AcceptanceCriterion, run_result: RunResult
    ) -> CriterionResult:
        try:
            if criterion.check_type == CheckType.ASSERTION:
                return self._check_assertion(criterion, run_result)
            if criterion.check_type == CheckType.REGEX:
                return self._check_regex(criterion, run_result)
            if criterion.check_type == CheckType.CONTAINS:
                return self._check_contains(criterion, run_result)
            # LLM_JUDGE and SCRIPT are deferred
            return CriterionResult(
                criterion_id=criterion.id,
                status=CriterionStatus.SKIPPED,
                message=f"check_type '{criterion.check_type}' not yet supported (deferred)",
                expected=criterion.expected,
            )
        except Exception as exc:  # noqa: BLE001
            return CriterionResult(
                criterion_id=criterion.id,
                status=CriterionStatus.ERROR,
                message=f"{type(exc).__name__}: {exc}",
                expected=criterion.expected,
            )

    def _check_assertion(
        self, criterion: AcceptanceCriterion, run_result: RunResult
    ) -> CriterionResult:
        expr = str(criterion.expected)
        namespace: dict[str, Any] = {
            "__builtins__": _SAFE_BUILTINS,
            "run_result": run_result,
        }
        # DT-1: eval with restricted builtins — safe for developer tool, not for untrusted input.
        actual = eval(expr, namespace)  # noqa: S307
        ok = bool(actual)
        return CriterionResult(
            criterion_id=criterion.id,
            status=CriterionStatus.PASSED if ok else CriterionStatus.FAILED,
            message="" if ok else f"Expression evaluated to {actual!r}",
            actual=actual,
            expected=expr,
        )

    def _check_regex(
        self, criterion: AcceptanceCriterion, run_result: RunResult
    ) -> CriterionResult:
        pattern = str(criterion.expected)
        text = str(run_result.output)
        match = re.search(pattern, text)
        ok = match is not None
        return CriterionResult(
            criterion_id=criterion.id,
            status=CriterionStatus.PASSED if ok else CriterionStatus.FAILED,
            message="" if ok else f"Pattern {pattern!r} not found in output",
            actual=text[:200] if not ok else match.group(0),
            expected=pattern,
        )

    def _check_contains(
        self, criterion: AcceptanceCriterion, run_result: RunResult
    ) -> CriterionResult:
        needle = str(criterion.expected)
        text = str(run_result.output)
        ok = needle in text
        return CriterionResult(
            criterion_id=criterion.id,
            status=CriterionStatus.PASSED if ok else CriterionStatus.FAILED,
            message="" if ok else f"{needle!r} not found in output",
            actual=text[:200] if not ok else needle,
            expected=needle,
        )


# ---------------------------------------------------------------------------
# Compliance calculation (module-level for reuse)
# ---------------------------------------------------------------------------


def _compute_compliance(
    results: list[CriterionResult],
    criteria: list[AcceptanceCriterion],
) -> float:
    """Weighted compliance rate ∈ [0, 1] — skips deferred check types."""
    criteria_by_id = {c.id: c for c in criteria}
    total_weight = 0.0
    passed_weight = 0.0
    for r in results:
        c = criteria_by_id.get(r.criterion_id)
        if c is None:
            continue
        if c.check_type in (CheckType.LLM_JUDGE, CheckType.SCRIPT):
            continue
        total_weight += c.weight
        if r.status == CriterionStatus.PASSED:
            passed_weight += c.weight
    return passed_weight / total_weight if total_weight > 0 else 0.0
