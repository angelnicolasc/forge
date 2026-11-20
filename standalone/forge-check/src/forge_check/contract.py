"""Versioned input parsing and deterministic comparison for Forge Check."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION = 1
CONDITIONAL_CASES_VERSION = 2
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
    conditional_stock: NotRequired[dict[str, StockCaseDetail]]


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
    stock: StockRule | None = None

    def document(self) -> dict[str, object]:
        rows: list[dict[str, object]] = [
            {"id": case_id, "expected": expected}
            for case_id, expected in sorted(self.expected.items())
        ]
        document: dict[str, object] = {
            "schema_version": SCHEMA_VERSION if self.stock is None else CONDITIONAL_CASES_VERSION,
            "rule_id": self.rule_id,
            "source": self.source,
            "cases": rows,
        }
        if self.stock is not None:
            document["conditional_stock"] = self.stock.policy
            for row in rows:
                row["stock"] = self.stock.subjects[str(row["id"])]
        return document


@dataclass(frozen=True)
class StockRule:
    policy: dict[str, int]
    subjects: dict[str, dict[str, str]]


class StockObservation(TypedDict):
    case_id: str
    revision: str
    source: str
    sku: str
    on_hand: int | None
    observed_at: int
    kind: Literal["read", "cache"]
    receipt: str
    origin_receipt: NotRequired[str]
    origin_observed_at: NotRequired[int]
    cache_valid_until: NotRequired[int]


@dataclass(frozen=True)
class StockEvidence:
    coverage: Literal["complete", "partial", "unavailable"]
    since: int
    until: int
    observations: tuple[StockObservation, ...]


class StockSubject(TypedDict):
    sku: str
    observation: StockObservation | None


class StockDiscard(TypedDict):
    observation: StockObservation
    reasons: list[str]


class StockCaseDetail(TypedDict):
    threshold: int
    source: str
    coverage: str
    window: dict[str, int] | None
    primary: StockSubject
    reserve: StockSubject
    reserve_required: bool | None
    observations: list[StockObservation]
    discarded: list[StockDiscard]
    result: Verdict
    reasons: list[str]
    expires_at: NotRequired[int]


@dataclass(frozen=True)
class Run:
    revision: str
    cases: dict[str, ObservationView]
    stock: dict[str, StockEvidence] = field(default_factory=dict)

    def pending_reference(self, case_set: CaseSet) -> dict[str, object]:
        rows: list[dict[str, object]] = [
            {"id": case_id, **self.cases[case_id]} for case_id in sorted(self.cases)
        ]
        for row in rows:
            evidence = self.stock.get(str(row["id"]))
            if evidence is not None:
                row["stock_evidence"] = {
                    "coverage": evidence.coverage,
                    "since": evidence.since,
                    "until": evidence.until,
                    "observations": list(evidence.observations),
                }
        return {
            "schema_version": SCHEMA_VERSION,
            "rule_id": case_set.rule_id,
            "source": case_set.source,
            "revision": self.revision,
            "reference_status": "pending_review",
            "cases": rows,
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


def _read(path: Path, role: str) -> object:
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

    return value


def _checked_document(value: object, role: str) -> dict[str, object]:
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
    return parse_cases(_read(path, "cases"))


def _stock_integer(value: object, field: str, minimum: int, maximum: int = 2**53 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _parse_stock_rule(value: object, rows: dict[str, dict[str, object]]) -> StockRule:
    raw = _object(value, "conditional_stock")
    if raw.keys() - {"threshold", "max_age_seconds", "cache_max_age_seconds"}:
        raise InputError("Unknown conditional_stock policy field")
    policy = {
        "threshold": _stock_integer(raw.get("threshold"), "threshold", 0),
        "max_age_seconds": _stock_integer(raw.get("max_age_seconds"), "max_age_seconds", 1, 86_400),
    }
    if "cache_max_age_seconds" in raw:
        policy["cache_max_age_seconds"] = _stock_integer(
            raw["cache_max_age_seconds"], "cache_max_age_seconds", 1, 86_400
        )
    subjects = {}
    for case_id, row in rows.items():
        pair = _object(row.get("stock"), f"stock for {case_id}")
        if pair.keys() != {"primary", "reserve"}:
            raise InputError(f"stock for {case_id} requires primary and reserve")
        primary = _label(pair["primary"], f"primary stock subject for {case_id}")
        reserve = _label(pair["reserve"], f"reserve stock subject for {case_id}")
        if primary == reserve:
            raise InputError(f"Primary and reserve stock subjects must differ for {case_id}")
        subjects[case_id] = {"primary": primary, "reserve": reserve}
    return StockRule(policy, subjects)


def parse_cases(value: object) -> CaseSet:
    document = _object(value, "cases")
    version = document.get("schema_version")
    if type(version) is not int or version not in (SCHEMA_VERSION, CONDITIONAL_CASES_VERSION):
        raise InputError("cases.schema_version must be 1 or 2")
    if ("conditional_stock" in document) != (version == CONDITIONAL_CASES_VERSION):
        raise InputError("cases.schema_version 2 and conditional_stock must appear together")
    rows = _case_rows(document, "cases")
    return CaseSet(
        rule_id=_label(document.get("rule_id"), "cases.rule_id"),
        source=_label(document.get("source"), "cases.source"),
        expected={
            case_id: _label(row.get("expected"), f"expected outcome for {case_id}")
            for case_id, row in rows.items()
        },
        stock=(
            _parse_stock_rule(document["conditional_stock"], rows)
            if "conditional_stock" in document
            else None
        ),
    )


def load_run(path: Path, role: str, case_set: CaseSet) -> Run:
    return parse_run(_read(path, role), role, case_set)


def _parse_stock_observation(value: object) -> StockObservation:
    row = _object(value, "stock observation")
    kind = row.get("kind")
    if kind not in ("read", "cache"):
        raise InputError("stock observation kind must be read or cache")
    quantity = row.get("on_hand")
    if "on_hand" not in row:
        raise InputError("stock observation requires on_hand (null if unconfirmed)")
    observation: StockObservation = {
        "case_id": _label(row.get("case_id"), "stock observation case"),
        "revision": _label(row.get("revision"), "stock observation revision"),
        "source": _label(row.get("source"), "stock observation source"),
        "sku": _label(row.get("sku"), "stock observation subject"),
        "on_hand": None if quantity is None else _stock_integer(quantity, "on_hand", 0),
        "observed_at": _stock_integer(row.get("observed_at"), "stock observation time", 1),
        "kind": kind,
        "receipt": _label(row.get("receipt"), "stock observation receipt"),
    }
    if kind == "cache":
        observation["origin_receipt"] = _label(row.get("origin_receipt"), "cache origin receipt")
        observation["origin_observed_at"] = _stock_integer(
            row.get("origin_observed_at"), "cache origin time", 1
        )
        if "cache_valid_until" in row:
            observation["cache_valid_until"] = _stock_integer(
                row["cache_valid_until"], "cache validity end", 1
            )
    return observation


def _parse_stock_evidence(value: object) -> StockEvidence:
    row = _object(value, "stock_evidence")
    coverage = row.get("coverage")
    if coverage not in ("complete", "partial", "unavailable"):
        raise InputError("stock_evidence coverage must be complete, partial or unavailable")
    since = _stock_integer(row.get("since"), "stock evidence window start", 1)
    until = _stock_integer(row.get("until"), "stock evidence window end", since + 1)
    observations = row.get("observations")
    if not isinstance(observations, list):
        raise InputError("stock_evidence observations must be an array")
    # Exporters can return the same immutable receipts in different pages or orders.
    unique = {}
    for item in observations:
        observation = _parse_stock_observation(item)
        unique[json.dumps(observation, sort_keys=True)] = observation
    return StockEvidence(
        coverage,
        since,
        until,
        tuple(unique[key] for key in sorted(unique)),
    )


def parse_run(value: object, role: str, case_set: CaseSet) -> Run:
    document = _checked_document(value, role)
    if document.get("rule_id") != case_set.rule_id or document.get("source") != case_set.source:
        raise InputError(f"{role} rule_id and source must match the case file")

    revision = _label(document.get("revision"), f"{role}.revision")
    observations: dict[str, ObservationView] = {}
    stock: dict[str, StockEvidence] = {}
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
        if case_set.stock is not None and "stock_evidence" in row:
            stock[case_id] = _parse_stock_evidence(row["stock_evidence"])
    return Run(revision, observations, stock)


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


def _conflicting_receipts(run: Run | None, source: str) -> set[str]:
    """Check the identity of source records across all cases in one revision."""
    conflicts: set[str] = set()
    if run is None:
        return conflicts
    records: dict[str, StockObservation] = {}
    # An original read may live outside this window and be cited by several cache hits.
    reads: dict[str, tuple[str, int, int | None]] = {}
    for case_id, evidence in run.stock.items():
        if evidence.coverage == "unavailable":
            continue
        for observation in evidence.observations:
            if (
                observation["case_id"] != case_id
                or observation["revision"] != run.revision
                or observation["source"] != source
            ):
                continue
            receipt = observation["receipt"]
            if records.setdefault(receipt, observation) != observation:
                conflicts.add(receipt)
            if observation["kind"] == "cache":
                origin = observation["origin_receipt"]
                when = observation["origin_observed_at"]
            else:
                origin = receipt
                when = observation["observed_at"]
            facts = observation["sku"], when, observation["on_hand"]
            if reads.setdefault(origin, facts) != facts:
                conflicts.add(origin)
    conflicts.update(key for key in reads if key in records and records[key]["kind"] != "read")
    return conflicts


def _stock_snapshot(
    rule: StockRule, source: str, case_id: str, run: Run | None, as_of: int, conflicts: set[str]
) -> StockCaseDetail:
    subjects = rule.subjects[case_id]
    primary: StockSubject = {"sku": subjects["primary"], "observation": None}
    reserve: StockSubject = {"sku": subjects["reserve"], "observation": None}
    evidence = run.stock.get(case_id) if run is not None else None
    available: dict[str, list[tuple[StockObservation, int]]] = {
        subject: [] for subject in subjects.values()
    }
    discarded: list[StockDiscard] = []
    matching_attempts: set[str] = set()
    max_age = rule.policy["max_age_seconds"] * 1_000_000
    cache_age = rule.policy.get("cache_max_age_seconds", 0) * 1_000_000
    reasons: list[str] = []
    result: StockCaseDetail = {
        "threshold": rule.policy["threshold"],
        "source": source,
        "coverage": evidence.coverage if evidence else "missing",
        "window": {"since": evidence.since, "until": evidence.until} if evidence else None,
        "primary": primary,
        "reserve": reserve,
        "reserve_required": None,
        "observations": list(evidence.observations) if evidence else [],
        "discarded": discarded,
        "result": "insufficient",
        "reasons": reasons,
    }
    if evidence is None or run is None:
        reasons.append("evidence_missing")
    elif evidence.coverage == "unavailable":
        reasons.append("source_unavailable")
    elif not evidence.until <= as_of < evidence.until + max_age:
        reasons.append("window_not_closed" if as_of < evidence.until else "window_expired")

    if evidence is None or run is None:
        return result
    for observation in evidence.observations:
        sku = observation["sku"]
        when = observation["observed_at"]
        expired = when + max_age
        issues = [
            reason
            for condition, reason in (
                (observation["case_id"] != case_id, "wrong_case"),
                (observation["revision"] != run.revision, "wrong_revision"),
                (observation["source"] != source, "wrong_source"),
                (sku not in available, "wrong_sku"),
                (not evidence.since <= when < evidence.until, "outside_window"),
            )
            if condition
        ]
        if not issues:
            matching_attempts.add(sku)
        if observation["receipt"] in conflicts:
            issues.append("receipt_conflicting")
        if when > as_of or as_of >= expired:
            issues.append("observation_expired" if when <= as_of else "observation_in_future")
        if observation["on_hand"] is None:
            issues.append("quantity_unconfirmed")
        if observation["kind"] == "cache":
            origin = observation["origin_observed_at"]
            expired = min(expired, origin + max_age, origin + cache_age)
            valid_until = observation.get("cache_valid_until")
            if valid_until is not None:
                expired = min(expired, valid_until)
            if origin > when or observation["origin_receipt"] in conflicts:
                issues.append("cache_provenance_invalid")
            elif cache_age == 0:
                issues.append("cache_disabled")
            elif valid_until is None:
                issues.append("cache_expiry_missing")
            elif as_of >= expired:
                issues.append("cache_expired")
        if issues or reasons:
            discarded.append({"observation": observation, "reasons": issues + reasons})
        else:
            available[sku].append((observation, expired))
    if reasons:
        return result
    if evidence.coverage == "partial":
        reasons.append("partial_coverage")

    candidates = available[subjects["primary"]]
    quantities = {observation["on_hand"] for observation, _ in candidates}
    if len(quantities) != 1:
        reasons.append("primary_missing" if not quantities else "primary_conflicting")
        return result
    first, expiry = max(candidates, key=lambda item: item[1])
    primary["observation"] = first
    quantity = first["on_hand"]
    assert quantity is not None
    result["reserve_required"] = quantity < rule.policy["threshold"]
    if result["reserve_required"]:
        confirmed = available[subjects["reserve"]]
        if not confirmed:
            if subjects["reserve"] in matching_attempts:
                reasons.append("reserve_unconfirmed")
            elif not reasons:
                result["result"] = "fail"
                reasons.append("reserve_omitted")
            else:
                reasons.append("reserve_missing")
            return result
        if len({observation["on_hand"] for observation, _ in confirmed}) != 1:
            reasons.append("reserve_conflicting")
            return result
        last, reserve_expiry = max(confirmed, key=lambda item: item[1])
        reserve["observation"] = last
        expiry = min(expiry, reserve_expiry)
    if not reasons:
        result["result"] = "pass"
        result["expires_at"] = min(expiry, evidence.until + max_age)
    return result


def compare(
    case_set: CaseSet, baseline: Run | None, candidate: Run, *, observed_at: int | None = None
) -> Report:
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
    if case_set.stock is not None:
        as_of = observed_at
        if as_of is None:
            as_of = max(
                (
                    evidence.until
                    for run in (baseline, candidate)
                    if run is not None
                    for evidence in run.stock.values()
                ),
                default=0,
            )
        sources = [
            (side, run, _conflicting_receipts(run, case_set.source))
            for side, run in (("baseline", baseline), ("candidate", candidate))
        ]
        for case in cases:
            details = {
                side: _stock_snapshot(
                    case_set.stock, case_set.source, case["id"], run, as_of, conflicts
                )
                for side, run, conflicts in sources
            }
            case["conditional_stock"] = details
            for side, detail in details.items():
                case["reasons"].extend(f"{side}_stock_{code}" for code in detail["reasons"])
            if details["candidate"]["result"] == "fail":
                case["result"] = "fail"
            elif case["result"] == "pass" and any(
                detail["result"] != "pass" for detail in details.values()
            ):
                case["result"] = "insufficient"

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
