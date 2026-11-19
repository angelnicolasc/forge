"""Versioned input parsing and deterministic comparison for Forge Check."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict, cast

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION = 1
Verdict = Literal["pass", "fail", "insufficient"]
ObservationStatus = Literal["completed", "error", "missing"]
EXIT_CODES: dict[Verdict, int] = {"pass": 0, "fail": 1, "insufficient": 2}
INVALID_EXIT = 3
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


class InputError(ValueError):
    """A validation error safe to include in a public report."""


class ObservationView(TypedDict):
    status: ObservationStatus
    observed: str | None


class CaseResult(TypedDict):
    id: str
    expected: str
    baseline: ObservationView
    candidate: ObservationView
    result: Verdict
    reasons: list[str]


class Coverage(TypedDict):
    eligible: int
    baseline_observed: int
    candidate_observed: int
    comparable: int


class Report(TypedDict):
    schema_version: int
    verdict: Verdict
    exit_code: int
    rule_id: str
    source: str
    baseline_revision: str | None
    candidate_revision: str
    coverage: Coverage
    cases: list[CaseResult]
    next_action: str


@dataclass(frozen=True)
class CaseSet:
    rule_id: str
    source: str
    expected: dict[str, str]


@dataclass(frozen=True)
class Run:
    revision: str
    cases: dict[str, ObservationView]

    def pending_reference(self, case_set: CaseSet) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "rule_id": case_set.rule_id,
            "source": case_set.source,
            "revision": self.revision,
            "reference_status": "pending_review",
            "cases": [{"id": case_id, **self.cases[case_id]} for case_id in sorted(self.cases)],
        }


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise InputError("Duplicate JSON object key")
        result[name] = value
    return result


def _reject_constant(_value: str) -> None:
    raise InputError("Non-finite JSON number")


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InputError(f"{context} must be a JSON object")
    return cast("dict[str, object]", value)


def _read(path: Path, role: str) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as stream:
            value: object = json.load(
                stream, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
    except OSError as exc:
        raise InputError(f"Cannot read {role} file") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"Invalid JSON in {role} (line {exc.lineno}, column {exc.colno})") from exc
    except InputError as exc:
        raise InputError(f"{exc} in {role}") from exc
    except (ValueError, RecursionError) as exc:
        raise InputError(f"{role} must contain valid UTF-8 JSON") from exc

    document = _object(value, role)
    version = document.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise InputError(f"{role}.schema_version must be {SCHEMA_VERSION}")
    return document


def _label(value: object, field: str) -> str:
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None:
        raise InputError(f"{field} must be a compact identifier (1–128 ASCII characters)")
    return value


def _case_rows(document: dict[str, object], role: str) -> dict[str, dict[str, object]]:
    rows = document.get("cases")
    if not isinstance(rows, list):
        raise InputError(f"{role}.cases must be an array")
    indexed: dict[str, dict[str, object]] = {}
    for number, row in enumerate(rows, start=1):
        record = _object(row, f"row {number} in {role}.cases")
        case_id = _label(record.get("id"), f"id for row {number} in {role}.cases")
        if case_id in indexed:
            raise InputError(f"Duplicate case ID at row {number} in {role}.cases")
        indexed[case_id] = record
    return indexed


def load_cases(path: Path) -> CaseSet:
    document = _read(path, "cases")
    return CaseSet(
        rule_id=_label(document.get("rule_id"), "cases.rule_id"),
        source=_label(document.get("source"), "cases.source"),
        expected={
            case_id: _label(row.get("expected"), f"expected outcome for {case_id}")
            for case_id, row in _case_rows(document, "cases").items()
        },
    )


def load_run(path: Path, role: str, case_set: CaseSet) -> Run:
    document = _read(path, role)
    if document.get("rule_id") != case_set.rule_id or document.get("source") != case_set.source:
        raise InputError(f"{role} rule_id and source must match the case file")

    revision = _label(document.get("revision"), f"{role}.revision")
    observations: dict[str, ObservationView] = {}
    for number, (case_id, row) in enumerate(_case_rows(document, role).items(), start=1):
        if case_id not in case_set.expected:
            raise InputError(f"Case ID at row {number} in {role}.cases is outside the eligible set")
        status = row.get("status")
        if status != "completed" and status != "error":
            raise InputError(f"{role} status for {case_id} must be completed or error")
        observed = row.get("observed")
        observations[case_id] = {
            "status": "completed" if status == "completed" else "error",
            "observed": (
                None if observed is None else _label(observed, f"{role} outcome for {case_id}")
            ),
        }
    return Run(revision, observations)


def _issue(observation: ObservationView, expected: str) -> str | None:
    if observation["status"] == "missing":
        return "case_missing"
    if observation["observed"] is not None and observation["observed"] != expected:
        return "unexpected_outcome"
    if observation["status"] == "error":
        return "run_error"
    if observation["observed"] is None:
        return "observation_missing"
    return None


def _case_result(
    case_id: str, expected: str, baseline: ObservationView, candidate: ObservationView
) -> CaseResult:
    baseline_issue = _issue(baseline, expected)
    candidate_issue = _issue(candidate, expected)
    reasons = []
    if baseline_issue is not None:
        reasons.append(f"baseline_{baseline_issue}")
    if candidate_issue is not None:
        reasons.append(f"candidate_{candidate_issue}")

    result: Verdict = "pass"
    if candidate_issue == "unexpected_outcome":
        result = "fail"
    elif reasons:
        result = "insufficient"
    return {
        "id": case_id,
        "expected": expected,
        "baseline": baseline,
        "candidate": candidate,
        "result": result,
        "reasons": reasons,
    }


def _completed_with_observation(observation: ObservationView) -> bool:
    return observation["status"] == "completed" and observation["observed"] is not None


def _next_action(verdict: Verdict, *, has_baseline: bool, has_cases: bool) -> str:
    if verdict == "fail":
        return "Investigate the candidate cases that violated the business rule and compare again."
    if not has_cases:
        return "Add eligible cases and expected outcomes to the case file and compare again."
    if not has_baseline:
        return "Review a recorded run as a reference and use --baseline to compare a new candidate."
    if verdict == "insufficient":
        return "Resolve the listed evidence gaps and compare again."
    return "The candidate satisfies the business rule across all eligible cases."


def compare(case_set: CaseSet, baseline: Run | None, candidate: Run) -> Report:
    if baseline is not None and baseline.revision == candidate.revision:
        raise InputError("Baseline and candidate must have distinct revision IDs")

    baseline_cases = baseline.cases if baseline is not None else {}
    missing: ObservationView = {"status": "missing", "observed": None}
    cases = [
        _case_result(
            case_id,
            case_set.expected[case_id],
            baseline_cases.get(case_id, missing),
            candidate.cases.get(case_id, missing),
        )
        for case_id in sorted(case_set.expected)
    ]

    verdict: Verdict = "pass"
    if any(case["result"] == "fail" for case in cases):
        verdict = "fail"
    elif not cases or any(case["result"] == "insufficient" for case in cases):
        verdict = "insufficient"

    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": verdict,
        "exit_code": EXIT_CODES[verdict],
        "rule_id": case_set.rule_id,
        "source": case_set.source,
        "baseline_revision": baseline.revision if baseline is not None else None,
        "candidate_revision": candidate.revision,
        "coverage": {
            "eligible": len(cases),
            "baseline_observed": sum(case["baseline"]["observed"] is not None for case in cases),
            "candidate_observed": sum(case["candidate"]["observed"] is not None for case in cases),
            "comparable": sum(
                _completed_with_observation(case["baseline"])
                and _completed_with_observation(case["candidate"])
                for case in cases
            ),
        },
        "cases": cases,
        "next_action": _next_action(
            verdict, has_baseline=baseline is not None, has_cases=bool(cases)
        ),
    }
