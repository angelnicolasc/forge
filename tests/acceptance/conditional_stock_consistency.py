"""Vast acceptance checks for conditional receipt identity and expiry on the installed wheel."""

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import version

from forge_check.contract import compare, parse_cases, parse_run

END = 10_000_000
REVISION = "candidate"


def observed(case, revision, sku, quantity, receipt, **fields):
    return {
        "case_id": case,
        "revision": revision,
        "source": "inventory",
        "sku": sku,
        "on_hand": quantity,
        "observed_at": END - 1_000_000,
        "kind": "read",
        "receipt": receipt,
        **fields,
    }


def run_doc(revision, groups):
    return {
        "schema_version": 1,
        "rule_id": "stock",
        "source": "inventory",
        "revision": revision,
        "cases": [
            {
                "id": case,
                "status": "completed",
                "observed": "done",
                "stock_evidence": {
                    "coverage": "complete",
                    "since": END - 2_000_000,
                    "until": END,
                    "observations": items,
                },
            }
            for case, items in groups.items()
        ],
    }


def evaluate(groups, *, at=END):
    cases = parse_cases(
        {
            "schema_version": 2,
            "rule_id": "stock",
            "source": "inventory",
            "conditional_stock": {
                "threshold": 5,
                "max_age_seconds": 60,
                "cache_max_age_seconds": 30,
            },
            "cases": [
                {
                    "id": case,
                    "expected": "done",
                    "stock": {"primary": "P", "reserve": "R"},
                }
                for case in groups
            ],
        }
    )
    baseline = {case: [observed(case, "stable", "P", 8, f"stable-{case}")] for case in groups}
    prior = parse_run(run_doc("stable", baseline), "baseline", cases)
    candidate = parse_run(run_doc(REVISION, groups), "candidate", cases)
    return compare(cases, prior, candidate, observed_at=at)


def check():
    primary = observed("A", REVISION, "P", 2, "primary-A")
    reserve = observed("A", REVISION, "R", 14, "reserve-A")
    cache = dict(
        reserve,
        kind="cache",
        receipt="hit-A",
        origin_receipt="origin-A",
        origin_observed_at=END - 4_000_000,
        cache_valid_until=END + 1_000_000,
    )
    rows = {}

    def record(name, expected, obs, *, at=END, reason=None):
        report = evaluate(obs, at=at)
        assert report["verdict"] == expected, (name, report)
        details = [case["conditional_stock"]["candidate"] for case in report["cases"]]
        if reason:
            codes = {code for detail in details for code in detail["reasons"]}
            codes.update(
                code
                for detail in details
                for item in detail["discarded"]
                for code in item["reasons"]
            )
            assert reason in codes, (name, report)
        rows[name] = expected
        return report

    direct = record("consistent", "pass", {"A": [primary, reserve]})
    replay = record("repeat_and_permute", "pass", {"A": [reserve, primary, reserve]})
    assert replay == direct
    record(
        "one_receipt_two_subjects",
        "insufficient",
        {"A": [primary, dict(reserve, receipt=primary["receipt"])]},
        reason="receipt_conflicting",
    )
    record(
        "one_receipt_two_cases",
        "insufficient",
        {"A": [dict(primary, on_hand=8)], "B": [dict(primary, case_id="B", on_hand=8)]},
        reason="receipt_conflicting",
    )
    record(
        "different_reserve_quantities",
        "insufficient",
        {"A": [primary, reserve, dict(reserve, receipt="reserve-A2", on_hand=17)]},
        reason="reserve_conflicting",
    )
    current = record("cache_valid", "pass", {"A": [primary, cache]}, at=END + 999_999)
    assert current["cases"][0]["conditional_stock"]["candidate"]["expires_at"] == END + 1_000_000
    record(
        "cache_at_expiry",
        "insufficient",
        {"A": [primary, cache]},
        at=END + 1_000_000,
        reason="cache_expired",
    )
    old_export = {key: value for key, value in cache.items() if key != "cache_valid_until"}
    record(
        "legacy_cache_without_expiry",
        "insufficient",
        {"A": [primary, old_export]},
        reason="cache_expiry_missing",
    )
    second = dict(cache, case_id="B", receipt="hit-B")
    multi = {"A": [primary, cache], "B": [dict(primary, case_id="B", receipt="primary-B"), second]}
    record("two_hits_one_consistent_origin", "pass", multi)
    multi["B"][1] = dict(second, on_hand=3)
    record(
        "conflicting_origin_for_two_hits", "insufficient", multi, reason="cache_provenance_invalid"
    )
    record(
        "cache_receipt_as_own_origin",
        "insufficient",
        {"A": [primary, dict(cache, origin_receipt=cache["receipt"])]},
        reason="cache_provenance_invalid",
    )
    return rows


if __name__ == "__main__":
    if sys.platform != "linux" or not os.environ.get("FORGE_VAST_LAB_ID"):
        raise SystemExit("Requires Linux and a nonempty FORGE_VAST_LAB_ID.")
    results = check()
    print(
        json.dumps({"version": version("forge-os-check"), "scenarios": results, "status": "pass"})
    )
