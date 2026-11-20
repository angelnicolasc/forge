"""Carry genuine RC2 gate decisions and live rollouts across a wheel upgrade on Vast."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from importlib.metadata import version
from pathlib import Path

from conditional_evidence import (
    BINDING,
    CASE_FIELDS,
    OBSERVER,
    OWNER,
    PROPOSER,
    READER,
    SOURCE,
    Inventory,
    initialize,
    manifest,
    now,
)
from forge_check.governed.control import Control
from forge_check.governed.db import Store
from forge_check.governed.host import Host, HttpProvider
from forge_check.governed.ledger import Ledger, Request
from forge_check.governed.revision import Revision

RC2_SHA256 = "da2e99069084178ca8b85ebcf9504e0ab7be8755bc92b8f355d0eddd043713ec"
KEY = b"fixture-only-conditional-upgrade-read"
CASE = "inventory-case"


def observation(revision: str, stamp: int, sku: str, receipt: str, units: int) -> dict:
    return {
        "kind": "read",
        "receipt": receipt,
        "on_hand": units,
        "source": SOURCE,
        "revision": revision,
        "observed_at": stamp,
        "sku": sku,
        "case_id": CASE,
    }


def inputs(revision: str, stamp: int, *, legacy: bool, ambiguous: bool) -> dict:
    case = {"id": CASE, "status": "completed", "observed": "complete"}
    if not legacy:
        token = revision[:16]
        case["stock_evidence"] = {
            "coverage": "complete",
            "since": stamp - 3,
            "until": stamp,
            "observations": [
                observation(revision, stamp - 2, "PRI", token + ".a", 2),
                observation(revision, stamp - 1, "RES", token + (".a" if ambiguous else ".b"), 14),
            ],
        }
    return {**CASE_FIELDS, "revision": revision, "cases": [case]}


def rollout(control: Control, name: str, evidence: str, mode: str, stamp: int) -> dict:
    return control.start_rollout(
        OWNER,
        name,
        "proposal",
        evidence,
        1,
        mode=mode,
        seed="seed",
        basis_points=10_000,
        ends_at=stamp + 180_000_000,
        max_concurrent=4,
        budget_microusd=200,
    )


def request(attempt: str) -> Request:
    return Request("pinned", READER, attempt, attempt, BINDING, {"sku": "PRI"})


def seed(root: Path) -> None:
    assert version("forge-os-check") == "0.4.0rc2"
    for scenario in ("canary", "shadow", "applied", "legacy"):
        legacy = scenario == "legacy"
        verifier = "forge-check/1" if legacy else "forge-check/2"
        stable = Revision.parse(manifest(budget=10, verifier=verifier))
        candidate = Revision.parse(manifest(budget=20, verifier=verifier))
        case = {"id": CASE, "expected": "complete"}
        cases = {**CASE_FIELDS, "cases": [case]}
        if not legacy:
            case["stock"] = {"primary": "PRI", "reserve": "RES"}
            cases.update(
                schema_version=2, conditional_stock={"threshold": 5, "max_age_seconds": 600}
            )
        control = initialize(root / f"{scenario}.db", stable, cases)
        control.register(PROPOSER, candidate)
        control.propose(PROPOSER, "proposal", candidate.digest)
        stamp = now()
        prior = inputs(stable.digest, stamp, legacy=legacy, ambiguous=False)
        later = inputs(candidate.digest, stamp, legacy=legacy, ambiguous=True)
        evidence = control.evaluate(
            OBSERVER, "rc2", "proposal", prior, later, observed_at=stamp, generation=1
        )
        assert evidence["report"]["verdict"] == "pass" and evidence["verifier"] == verifier
        record = {"evidence": evidence, "prior": prior, "later": later, "stamp": stamp}
        if scenario in ("canary", "shadow"):
            rollout(control, "existing", "rc2", scenario, stamp)
            pin = control.start_task(READER, "pinned", worker="worker")
            assert pin["allocation"]["cohort"] == scenario
            if scenario == "canary":
                ledger = Ledger(control.store, fingerprint_key=KEY)
                assert ledger.admit(request("in-flight")).disposition == "dispatch"
        elif scenario == "applied":
            record["decision"] = control.promote(OWNER, "old-decision", "proposal", "rc2", 1)
            assert record["decision"]["status"] == "applied"
        (root / f"{scenario}.json").write_text(json.dumps(record), encoding="utf-8")


def verify(root: Path, scenario: str) -> dict:
    control = Control(Store(root / f"{scenario}.db", create=False))
    record = json.loads((root / f"{scenario}.json").read_text(encoding="utf-8"))
    stored, prior, later, stamp = (record[k] for k in ("evidence", "prior", "later", "stamp"))
    assert control.get_evidence(OWNER, "rc2") == stored

    def evaluate(evidence_id: str, document: dict) -> dict:
        return control.evaluate(
            OBSERVER, evidence_id, "proposal", prior, document, observed_at=stamp, generation=1
        )

    assert evaluate("rc2", later) == stored
    if scenario == "legacy":
        assert control.promote(OWNER, "new", "proposal", "rc2", 1)["status"] == "applied"
        return {"rc2_verifier": stored["verifier"], "legacy_promotion": "applied"}
    if scenario == "applied":
        assert control.promote(OWNER, "old-decision", "proposal", "rc2", 1) == record["decision"]
        assert control.rollback(OWNER, "recovery", 2)["status"] == "applied"
        assert control.get_alias(OWNER)["revision"] == prior["revision"]
        return {"historical_decision": "replayed", "rollback": "applied"}

    changed = deepcopy(later)
    changed["cases"][0]["stock_evidence"]["observations"][1]["receipt"] += ".corrected"
    try:
        evaluate("rc2", changed)
    except ValueError as exc:
        assert "different evidence" in str(exc)
    else:
        raise AssertionError("Old evidence ID accepted changed observer inputs")
    blocked = control.promote(OWNER, "block", "proposal", "rc2", 1)
    assert blocked["status"] == "rejected" and blocked["reason"] == "evidence_revalidation_required"
    assert control.get_alias(OWNER)["generation"] == 1
    try:
        rollout(control, "new-old-rollout", "rc2", scenario, stamp)
    except ValueError as exc:
        assert "evidence_revalidation_required" in str(exc)
    else:
        raise AssertionError("New rollout accepted a superseded certificate")
    assert control.get_rollout(OWNER, "existing")["state"] == blocked["reason"]
    next_root = control.start_task(READER, "after-upgrade", worker="worker")
    assert next_root["revision"] == prior["revision"]
    assert next_root["allocation"]["reason"] == blocked["reason"]
    with Inventory({"PRI": 2}) as target:
        host = Host(control.store, fingerprint_key=KEY, provider=HttpProvider(target.origin))
        if scenario == "canary":
            assert not host.ledger.mark_dispatched("in-flight")
        result = host.invoke(request("probe"))
        if scenario == "canary":
            assert result.disposition == "denied" and result.attempt["reason"] == blocked["reason"]
            assert target.journal == []
        else:
            assert result.disposition == "dispatch"
            assert [row["revision"] for row in target.journal] == [prior["revision"]]
            assert all("shadow" not in row["evidence"] for row in host.ledger.receipts("pinned"))
        calls = len(target.journal)

    reevaluated = evaluate("again", later)
    assert reevaluated["verifier"] == "forge-check/2.1"
    assert reevaluated["report"]["verdict"] == "insufficient"
    denied = control.promote(OWNER, "ambiguous", "proposal", "again", 1)
    assert denied["reason"] == "gate_insufficient"
    corrected = evaluate("corrected", changed)
    assert evaluate("corrected", changed) == corrected
    assert corrected["report"]["verdict"] == "pass"
    assert control.stop_rollout(OWNER, "existing", 1)["state"] == "stopped"
    assert rollout(control, "new-rollout", "corrected", scenario, stamp)["state"] == "open"
    assert control.promote(OWNER, "release", "proposal", "corrected", 1)["status"] == "applied"
    assert control.get_evidence(OWNER, "rc2") == stored
    return {
        "prior_certificate": stored["verifier"],
        "new_certificate": corrected["verifier"],
        "historical_verdict": stored["report"]["verdict"],
        "reevaluated_verdict": reevaluated["report"]["verdict"],
        "rollout_hold": next_root["allocation"]["reason"],
        "existing_root_provider_calls": calls,
        "corrected_verdict": corrected["report"]["verdict"],
        "new_generation": control.get_alias(OWNER)["generation"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--previous-wheel", type=Path)
    parser.add_argument("--wheel", type=Path, help="Candidate artifact for the receipt")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    lab = os.environ.get("FORGE_VAST_LAB_ID", "")
    if sys.platform != "linux" or not lab.isdigit():
        parser.error("Run inside the Vast lab")
    if args.seed:
        seed(args.seed)
        return
    if args.previous_wheel is None or args.output is None:
        parser.error("Pass --previous-wheel and --output")
    with args.previous_wheel.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    assert digest == RC2_SHA256
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        subprocess.run(
            [sys.executable, __file__, "--seed", directory],
            check=True,
            env={**os.environ, "PYTHONPATH": str(args.previous_wheel.resolve())},
        )
        outcomes = {name: verify(root, name) for name in ("canary", "shadow", "applied", "legacy")}
    receipt = {
        "status": "pass",
        "vast_lab_id": lab,
        "rc2_sha256": digest,
        "candidate": version("forge-os-check"),
        "outcomes": outcomes,
    }
    if args.wheel is not None:
        with args.wheel.open("rb") as stream:
            receipt["wheel_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
