"""Exercise conditional inventory evidence using installed wheels and a loopback destination."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

from forge_check.governed.code_mode import CodeMode
from forge_check.governed.control import Control
from forge_check.governed.db import Store
from forge_check.governed.host import Host, HttpProvider
from forge_check.governed.ledger import Request
from forge_check.governed.revision import Revision

BINDING = "inventory.stock"
READER = "inventory-reader"
OWNER = "release-owner"
PROPOSER = "candidate-proposer"
OBSERVER = "inventory-observer"
SCOPE = "conditional-inventory"
SOURCE = "inventory-journal"
CASE_FIELDS = {"schema_version": 1, "rule_id": "stock-complete-v1", "source": SOURCE}
PRINCIPALS = {
    READER: {"roles": ["invoke"], "bindings": [BINDING]},
    "inventory-admin": {"roles": ["admin"], "bindings": []},
    OWNER: {"roles": ["approve", "inspect"], "bindings": []},
    PROPOSER: {"roles": ["propose"], "bindings": []},
    OBSERVER: {"roles": ["evidence"], "bindings": []},
}
THRESHOLD = 5


def now() -> int:
    return time.time_ns() // 1000


def schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


SKU = {"type": "string", "format": "identifier"}
QUANTITY = {"type": "integer", "minimum": 0}


def manifest(*, budget: int, verifier: str = "forge-check/1", ttl: int = 0) -> dict:
    return {
        "schema_version": 2,
        "scope": SCOPE,
        "budget_microusd": budget,
        "policy": {"allowed_bindings": [BINDING]},
        "check": {"rule_id": CASE_FIELDS["rule_id"], "verifier": verifier},
        "bindings": {
            BINDING: {
                "kind": "tool",
                "version": "1",
                "tariff": "inventory-v1",
                "reserve_microusd": 2,
                "server": "inventory",
                "method": "stock",
                "effect": "read",
                "idempotency": True,
                "cache_ttl_seconds": ttl,
                "contract": {
                    "schema_version": 1,
                    "input": schema({"sku": SKU}),
                    "output": schema({"sku": SKU, "on_hand": QUANTITY}),
                    "visible": ["sku", "on_hand"],
                },
            }
        },
    }


class Inventory(HTTPServer):
    """Record provider reads independently of Forge's ledger and executor output."""

    def __init__(self, stock: dict[str, int]) -> None:
        super().__init__(("127.0.0.1", 0), InventoryHandler)
        self.stock = stock
        self.journal: list[dict] = []
        self.observer_connected = True
        self.thread = Thread(target=self.serve_forever, daemon=True)

    def __enter__(self) -> Inventory:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()
        self.thread.join(5)
        self.server_close()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class InventoryHandler(BaseHTTPRequestHandler):
    server: Inventory

    def log_message(self, *_: object) -> None:
        pass

    def send_json(self, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/journal":
            self.send_error(404)
            return
        if not self.server.observer_connected:
            self.send_error(503)
            return
        self.send_json(self.server.journal)

    def do_POST(self) -> None:
        if self.path != "/dispatch":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sku = request["input"]["sku"]
        if request["binding_id"] != BINDING or sku not in self.server.stock:
            self.send_error(400)
            return
        quantity = self.server.stock[sku]
        receipt = f"destination-{len(self.server.journal) + 1}"
        self.server.journal.append(
            {
                "sku": sku,
                "on_hand": quantity,
                "revision": request["revision"],
                "attempt_id": request["attempt_id"],
                "observed_at": now(),
                "receipt": receipt,
            }
        )
        self.send_json(
            {
                "attempt_id": request["attempt_id"],
                "operation_key": request["operation_key"],
                "observation_id": receipt,
                "outcome": "succeeded",
                "confirmation": "verified",
                "charge_microusd": 1,
                "result": {"sku": sku, "on_hand": quantity},
            }
        )


def initialize(path: Path, revision: Revision, cases: dict) -> Control:
    control = Control(Store(path))
    control.bootstrap(SCOPE, revision, cases, PRINCIPALS)
    return control


def execute(
    control: Control,
    target: Inventory,
    case_id: str,
    primary: str,
    reserve: str,
    *,
    route: str,
    task: str,
    omit: bool = False,
) -> dict:
    pin = control.start_task(READER, task, worker="acceptance", case_id=case_id, source=route)
    host = Host(
        control.store,
        fingerprint_key=b"fixture-admission-key-adr0003-v1",
        provider=HttpProvider(target.origin),
    )
    started = now()
    trace = len(target.journal)
    if route == "code_mode":
        program = """
            const primary = tools['inventory.stock']({sku: input.primary}).result;
            if (!primary) throw new Error('Primary unavailable');
            const output = {primary: primary.on_hand};
            if (!input.omit && primary.on_hand < input.threshold) {
                const reserve = tools['inventory.stock']({sku: input.reserve}).result;
                if (!reserve) throw new Error('Reserve unavailable');
                output.reserve = reserve.on_hand;
            }
            return output;
        """
        output_schema = schema({"primary": QUANTITY, "reserve": QUANTITY})
        output_schema["required"] = ["primary"]
        result = CodeMode(host, enabled=True, bindings={BINDING}).run(
            READER,
            task,
            "conditional-stock",
            program,
            inputs={"primary": primary, "reserve": reserve, "threshold": THRESHOLD, "omit": omit},
            output_schema=output_schema,
        )
        status, output, attempt_ids = result.status, result.output, list(result.attempt_ids)
        assert status == "completed", result.reason
    else:
        attempt_ids = []

        def read(sku: str, role: str) -> dict:
            attempt_id = f"{task}-{role}"
            result = host.invoke(Request(task, READER, role, attempt_id, BINDING, {"sku": sku}))
            assert result.result is not None, result.attempt
            attempt_ids.append(attempt_id)
            return result.result

        output = {"primary": read(primary, "primary")["on_hand"]}
        if not omit and output["primary"] < THRESHOLD:
            output["reserve"] = read(reserve, "reserve")["on_hand"]
        status = "completed"
    finished = now() + 1
    observed = target.journal[trace:]
    assert all(row["revision"] == pin["revision"] for row in observed)
    assert all(row["attempt_id"] in attempt_ids for row in observed)
    return {
        "executor": {"status": status, "output": output, "attempt_ids": attempt_ids},
        "destination": observed,
        "receipts": host.ledger.receipts(task),
        "since": started,
        "until": finished,
    }


def run_document(revision: Revision, case_id: str, execution: dict, observed: str | None) -> dict:
    return {
        **CASE_FIELDS,
        "revision": revision.digest,
        "cases": [{"id": case_id, "status": execution["executor"]["status"], "observed": observed}],
    }


def reproduce_rc1(root: Path) -> dict:
    assert version("forge-os-check") == "0.4.0rc1"
    case_id = "conditional-01"
    primary, reserve = "PRI-A4", "RES-K4"
    cases = {**CASE_FIELDS, "cases": [{"id": case_id, "expected": "complete"}]}
    stable = Revision.parse(manifest(budget=10))
    candidate = Revision.parse(manifest(budget=20))
    release = initialize(root / "release.sqlite3", stable, cases)
    trial = initialize(root / "trial.sqlite3", candidate, cases)
    release.register(PROPOSER, candidate)
    release.propose(PROPOSER, "proposal", candidate.digest)
    with Inventory({primary: 2, reserve: 14}) as target:
        baseline = execute(
            release, target, case_id, primary, reserve, route="direct", task="reference"
        )
        omitted = execute(
            trial, target, case_id, primary, reserve, route="code_mode", task="omitted", omit=True
        )
        assert [row["sku"] for row in baseline["destination"]] == [primary, reserve]
        assert [row["sku"] for row in omitted["destination"]] == [primary]
        assert omitted["executor"]["output"] == {"primary": 2}
        count = len(target.journal)
        baseline_run = run_document(stable, case_id, baseline, "complete")
        raw_run = run_document(candidate, case_id, omitted, None)
        # RC1 accepts the observer's normalized business label. With raw reads alone, no label is inferred.
        raw_gate = release.evaluate(
            OBSERVER,
            "raw-journal",
            "proposal",
            baseline_run,
            raw_run,
            observed_at=now(),
            generation=1,
        )
        assert raw_gate["report"]["verdict"] == "insufficient"
        # The independent destination journal covers the whole execution in this fixture.
        skipped = baseline["destination"][1]["sku"] == reserve and all(
            row["sku"] != reserve for row in omitted["destination"]
        )
        assert skipped
        oracle_run = run_document(candidate, case_id, omitted, "reserve-omitted")
        oracle_gate = release.evaluate(
            OBSERVER,
            "oracle-label",
            "proposal",
            baseline_run,
            oracle_run,
            observed_at=now(),
            generation=1,
        )
        assert oracle_gate["report"]["verdict"] == "fail"
        assert len(target.journal) == count
    rc2_cases = {
        **cases,
        "schema_version": 2,
        "conditional_stock": {"threshold": THRESHOLD, "max_age_seconds": 120},
        "cases": [
            {
                "id": case_id,
                "expected": "complete",
                "stock": {"primary": primary, "reserve": reserve},
            }
        ],
    }
    check_report = root / "invalid-check.json"
    forge(
        "check",
        "--cases",
        save_json(root / "rc2-cases.json", rc2_cases),
        "--candidate",
        save_json(root / "run.json", raw_run),
        "--output",
        check_report,
        exit_code=3,
    )
    return {
        "case": case_id,
        "executor": omitted["executor"],
        "destination": omitted["destination"],
        "reference_destination": baseline["destination"],
        "external_oracle": {"reserve_read_omitted": skipped, "required_sku": reserve},
        "gate_without_normalized_label": raw_gate,
        "gate_with_external_oracle_label": oracle_gate,
        "conditional_schema_rejected": json.loads(check_report.read_text(encoding="utf-8"))[
            "verdict"
        ]
        == "invalid",
        "evaluation_dispatches": 0,
    }


@dataclass(frozen=True)
class Scenario:
    name: str
    primary: str
    reserve: str
    quantity: int
    reserve_quantity: int
    verdict: str
    omit: bool = False
    reason: str | None = None


DEMO = (
    Scenario("omitted", "PRI-A4", "RES-K4", 2, 14, "fail", True, "reserve_omitted"),
    Scenario("complete", "PRI-A4", "RES-K4", 2, 14, "pass"),
    Scenario("offline", "PRI-A4", "RES-K4", 2, 14, "insufficient", True, "source_unavailable"),
)
MATRIX = (
    Scenario("equal", "PRI-M23", "RES-Y84", 5, 19, "pass"),
    Scenario("above", "PRI-N52", "RES-P67", 9, 21, "pass"),
    Scenario("complete", "PRI-D91", "RES-T76", 3, 17, "pass"),
    Scenario("omitted", "PRI-H39", "RES-V14", 4, 22, "fail", True, "reserve_omitted"),
    Scenario("partial", "PRI-G44", "RES-K82", 1, 18, "insufficient", True, "partial_coverage"),
    Scenario(
        "unknown-primary", "PRI-L26", "RES-A33", 4, 8, "insufficient", True, "primary_missing"
    ),
    Scenario(
        "unconfirmed-reserve",
        "PRI-B64",
        "RES-S90",
        2,
        11,
        "insufficient",
        False,
        "reserve_unconfirmed",
    ),
    Scenario(
        "wrong-identity", "PRI-J31", "RES-R64", 4, 12, "insufficient", True, "partial_coverage"
    ),
    Scenario("cached", "PRI-Q53", "RES-X09", 3, 13, "pass"),
    Scenario(
        "expired-cache", "PRI-E57", "RES-Z10", 3, 16, "insufficient", False, "reserve_unconfirmed"
    ),
)
EXPIRES_BEFORE_PROMOTION = Scenario("promotion-expiry", "PRI-W71", "RES-C22", 2, 20, "pass")
MAX_AGE = 120
CACHE_AGE = 3
EXIT = {"pass": 0, "fail": 1, "insufficient": 2}


def save_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def forge(*args: object, exit_code: int = 0) -> dict:
    result = subprocess.run(
        [str(Path(sys.executable).with_name("forge")), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == exit_code, (args, result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout) if result.stdout and args[0] != "check" else {}


def bootstrap_cli(root: Path, initial: dict, cases: dict) -> tuple[Control, Path]:
    db = root / "release.sqlite3"
    config = save_json(
        root / "bootstrap.json",
        {"scope": SCOPE, "initial": initial, "cases": cases, "principals": PRINCIPALS},
    )
    reply = forge("bootstrap", "--db", db, "--config", config)
    assert reply == {"scope": SCOPE, "revision": Revision.parse(initial).digest, "generation": 1}
    return Control(Store(db, create=False)), db


def exercise_legacy(root: Path) -> dict:
    root.mkdir()
    cases = {**CASE_FIELDS, "cases": [{"id": "legacy-case", "expected": "complete"}]}
    initial_doc = manifest(budget=10)
    stable = Revision.parse(initial_doc)
    candidate = Revision.parse(manifest(budget=20))
    release, db = bootstrap_cli(root, initial_doc, cases)
    release.register(PROPOSER, candidate)
    release.propose(PROPOSER, "legacy", candidate.digest)
    completed = {"executor": {"status": "completed"}}
    baseline = save_json(
        root / "baseline.json", run_document(stable, "legacy-case", completed, "complete")
    )
    candidate_path = save_json(
        root / "candidate.json", run_document(candidate, "legacy-case", completed, "complete")
    )
    check_report = root / "check.json"
    forge(
        "check",
        "--cases",
        save_json(root / "cases.json", cases),
        "--baseline",
        baseline,
        "--candidate",
        candidate_path,
        "--output",
        check_report,
    )
    saved = json.loads(check_report.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1 and saved["verdict"] == "pass"
    assert "conditional_stock" not in saved["cases"][0]
    evaluation = forge(
        "evaluate",
        "--db",
        db,
        "--actor",
        OBSERVER,
        "--proposal",
        "legacy",
        "--evidence",
        "legacy-evidence",
        "--baseline",
        baseline,
        "--candidate",
        candidate_path,
        "--generation",
        1,
        "--observed-at",
        now(),
    )
    assert evaluation["report"] == saved
    decision = release.promote(OWNER, "legacy-promotion", "legacy", "legacy-evidence", 1)
    assert decision["status"] == "applied"
    return {"saved_check": saved, "gate": evaluation, "decision": decision}


def stock_evidence(target: Inventory, execution: dict, case_id: str) -> dict:
    coverage = "complete"
    try:
        with urlopen(f"{target.origin}/journal", timeout=5) as response:
            journal = json.load(response)
    except HTTPError as exc:
        if exc.code != 503:
            raise
        coverage = "unavailable"
        journal = []
    return {
        "coverage": coverage,
        "since": execution["since"],
        "until": execution["until"],
        "observations": [
            {
                "case_id": case_id,
                "revision": row["revision"],
                "source": SOURCE,
                "kind": "read",
                "sku": row["sku"],
                "on_hand": row["on_hand"],
                "observed_at": row["observed_at"],
                "receipt": row["receipt"],
            }
            for row in journal
            if row["attempt_id"] in execution["executor"]["attempt_ids"]
        ],
    }


def prime_reserve(control: Control, target: Inventory, case_id: str, sku: str) -> dict:
    control.start_task(READER, "cache-origin", worker="acceptance", case_id=case_id)
    host = Host(
        control.store,
        fingerprint_key=b"fixture-admission-key-adr0003-v1",
        provider=HttpProvider(target.origin),
    )
    result = host.invoke(
        Request("cache-origin", READER, "prime", "cache-source", BINDING, {"sku": sku})
    )
    assert result.result == {"sku": sku, "on_hand": target.stock[sku]}
    return target.journal[-1]


def cached_observation(execution: dict, origin: dict, case_id: str) -> dict:
    (hit,) = [row for row in execution["receipts"] if row["phase"] == "cache_hit"]
    assert hit["evidence"]["cached_from_attempt_id"] == origin["attempt_id"]
    assert hit["revision"] == origin["revision"]
    assert hit["case_id"] == case_id
    at = hit["recorded_at"]
    assert at < hit["evidence"]["valid_until"]
    return {
        "case_id": case_id,
        "revision": hit["revision"],
        "source": SOURCE,
        "kind": "cache",
        "sku": origin["sku"],
        "on_hand": origin["on_hand"],
        "observed_at": at,
        "receipt": hit["event_id"],
        "origin_receipt": origin["receipt"],
        "origin_observed_at": origin["observed_at"],
        "cache_valid_until": hit["evidence"]["valid_until"],
    }


def exercise_candidate(root: Path, scenario: Scenario, route: str, *, demo: bool = False) -> dict:
    root.mkdir()
    case_id = "conditional-01" if demo else f"holdout-{scenario.name}"
    policy = {"threshold": THRESHOLD, "max_age_seconds": MAX_AGE}
    verify_expiry = scenario.name == EXPIRES_BEFORE_PROMOTION.name
    use_cache = verify_expiry or scenario.name in {"cached", "expired-cache"}
    if use_cache:
        policy["cache_max_age_seconds"] = CACHE_AGE
    cases = {
        **CASE_FIELDS,
        "schema_version": 2,
        "conditional_stock": policy,
        "cases": [
            {
                "id": case_id,
                "expected": "complete",
                "stock": {"primary": scenario.primary, "reserve": scenario.reserve},
            }
        ],
    }
    initial_doc = manifest(budget=10, verifier="forge-check/2", ttl=30 if use_cache else 0)
    candidate_doc = manifest(budget=20, verifier="forge-check/2", ttl=30 if use_cache else 0)
    stable, candidate = Revision.parse(initial_doc), Revision.parse(candidate_doc)
    release, db = bootstrap_cli(root, initial_doc, cases)
    trial = initialize(root / "trial.sqlite3", candidate, cases)
    candidate_file = save_json(root / "revision.json", candidate_doc)
    assert forge("register", "--db", db, "--actor", PROPOSER, "--manifest", candidate_file) == {
        "revision": candidate.digest
    }
    release.propose(PROPOSER, "proposal", candidate.digest)
    with Inventory(
        {scenario.primary: scenario.quantity, scenario.reserve: scenario.reserve_quantity}
    ) as target:
        baseline = execute(
            release,
            target,
            case_id,
            scenario.primary,
            scenario.reserve,
            route=route,
            task="reference",
        )
        origin = prime_reserve(trial, target, case_id, scenario.reserve) if use_cache else None
        if scenario.name == "expired-cache":
            time.sleep(CACHE_AGE + 0.05)
        observed = execute(
            trial,
            target,
            case_id,
            scenario.primary,
            scenario.reserve,
            route=route,
            task="candidate",
            omit=scenario.omit,
        )
        reads = [scenario.primary]
        if scenario.quantity < THRESHOLD and not scenario.omit and not use_cache:
            reads.append(scenario.reserve)
        assert [row["sku"] for row in observed["destination"]] == reads
        assert observed["executor"]["status"] == "completed"
        expected_output = {"primary": scenario.quantity}
        if scenario.quantity < THRESHOLD and not scenario.omit:
            expected_output["reserve"] = scenario.reserve_quantity
        assert observed["executor"]["output"] == expected_output
        baseline_run = run_document(stable, case_id, baseline, "complete")
        baseline_evidence = stock_evidence(target, baseline, case_id)
        baseline_run["cases"][0]["stock_evidence"] = baseline_evidence
        candidate_run = run_document(candidate, case_id, observed, "complete")
        # The stock API remains reachable. Disconnect the observer that exports the read journal.
        target.observer_connected = scenario.name != "offline"
        evidence = stock_evidence(target, observed, case_id)
        candidate_run["cases"][0]["stock_evidence"] = evidence
        if origin is not None:
            evidence["observations"].append(cached_observation(observed, origin, case_id))
        if scenario.name in {"partial", "wrong-identity"}:
            evidence["coverage"] = "partial"
        elif scenario.name == "unknown-primary":
            evidence["observations"][0]["on_hand"] = None
        elif scenario.name == "unconfirmed-reserve":
            evidence["observations"][1]["on_hand"] = None
        expected_discards = set()
        if scenario.name == "wrong-identity":
            primary_observation = evidence["observations"][0]
            borrowed = {**primary_observation, "sku": scenario.reserve}
            for mismatch, (field, value) in {
                "wrong_case": ("case_id", "different-case"),
                "wrong_revision": ("revision", stable.digest),
                "wrong_sku": ("sku", "RES-OTHER"),
                "wrong_source": ("source", "untrusted-journal"),
                "outside_window": ("observed_at", evidence["since"] - 1),
            }.items():
                evidence["observations"].append({**borrowed, field: value, "receipt": mismatch})
                expected_discards.add(mismatch)
        if scenario.name == "expired-cache":
            expected_discards.add("cache_expired")

        baseline_path = save_json(root / "baseline.json", baseline_run)
        candidate_path = save_json(root / "candidate.json", candidate_run)
        observed_at = now()
        before = release.get_alias(OWNER)
        count = len(target.journal)
        evaluate_args = (
            "evaluate",
            "--db",
            db,
            "--actor",
            OBSERVER,
            "--proposal",
            "proposal",
            "--evidence",
            "evaluation",
            "--baseline",
            baseline_path,
            "--candidate",
            candidate_path,
            "--generation",
            1,
            "--observed-at",
            observed_at,
        )
        evaluation = forge(*evaluate_args, exit_code=EXIT[scenario.verdict])
        assert forge(*evaluate_args, exit_code=EXIT[scenario.verdict]) == evaluation
        assert evaluation["report"]["verdict"] == scenario.verdict
        if demo:
            cases_path = save_json(root / "cases.json", cases)
            reference_path = root / "saved-reference.json"
            check_path = root / "check.json"
            forge(
                "check",
                "--cases",
                cases_path,
                "--candidate",
                baseline_path,
                "--output",
                check_path,
                "--save-reference",
                reference_path,
                exit_code=2,
            )
            check = subprocess.run(
                [
                    str(Path(sys.executable).with_name("forge")),
                    "check",
                    "--cases",
                    cases_path,
                    "--baseline",
                    reference_path,
                    "--candidate",
                    candidate_path,
                    "--output",
                    check_path,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert check.returncode == EXIT[scenario.verdict], (check.stdout, check.stderr)
            assert json.loads(check_path.read_text())["verdict"] == scenario.verdict
            if scenario.reason:
                assert scenario.reason in check.stdout and scenario.reserve in check.stdout
        assert evaluation["report"]["cases"][0]["candidate"]["status"] == "completed"
        diagnostics = evaluation["report"]["cases"][0]["conditional_stock"]["candidate"]
        assert diagnostics["result"] == scenario.verdict
        if scenario.reason is not None:
            assert scenario.reason in diagnostics["reasons"], diagnostics
        actual_discards = {reason for row in diagnostics["discarded"] for reason in row["reasons"]}
        assert expected_discards <= actual_discards, diagnostics
        promote_args = ("promote", "--db", db, "--proposal", "proposal", "--evidence", "evaluation")
        if scenario.name == "equal":
            denied = forge(
                *promote_args,
                "--actor",
                PROPOSER,
                "--decision",
                "unauthorized",
                "--generation",
                1,
                exit_code=1,
            )
            assert denied == {"error": "unauthorized"}
            assert release.get_alias(OWNER) == before
            downgraded = save_json(root / "downgraded.json", manifest(budget=30))
            forge(
                "register", "--db", db, "--actor", PROPOSER, "--manifest", downgraded, exit_code=3
            )
            other = Revision.parse(manifest(budget=30, verifier="forge-check/2"))
            release.register(PROPOSER, other)
            release.propose(PROPOSER, "other-proposal", other.digest)
            mismatch = release.promote(OWNER, "wrong-proposal", "other-proposal", "evaluation", 1)
            assert mismatch["reason"] == "evidence_mismatch"
        expected_promotion = {
            "pass": "gate_passed",
            "fail": "gate_failed",
            "insufficient": "gate_insufficient",
        }[scenario.verdict]
        if verify_expiry:
            assert origin is not None
            expires_at = origin["observed_at"] + CACHE_AGE * 1_000_000
            assert evaluation["expires_at"] == expires_at
            time.sleep(max(0, (expires_at - now()) / 1_000_000) + 0.05)
            expected_promotion = "evidence_expired"
        passed = expected_promotion == "gate_passed"
        decision = forge(
            *promote_args,
            "--actor",
            OWNER,
            "--decision",
            "promotion",
            "--generation",
            1,
            exit_code=0 if passed else 2,
        )
        assert decision["reason"] == expected_promotion
        after = release.get_alias(OWNER)
        if passed:
            assert (after["generation"], after["revision"]) == (2, candidate.digest)
        else:
            assert after == before
        if scenario.name == "equal":
            conflict = release.promote(OWNER, "stale-generation", "proposal", "evaluation", 1)
            assert conflict["reason"] == "stale_generation"
            original = release.start_task(
                READER, "reference", worker="acceptance", case_id=case_id, source=route
            )
            assert original["revision"] == stable.digest
            assert release.rollback(OWNER, "restore-previous", 2)["status"] == "applied"
            assert release.get_alias(OWNER)["revision"] == stable.digest
        assert len(target.journal) == count, "Evaluation or promotion contacted the destination"
    return {
        "scenario": scenario.name,
        "route": route,
        "executor": observed["executor"],
        "destination": observed["destination"],
        "baseline_destination": baseline["destination"],
        "cache_origin": origin,
        "gate_input": candidate_run,
        "gate": evaluation,
        "decision": decision,
        "alias_before": before,
        "alias_after": after,
        "evaluation_dispatches": 0,
    }


def demo_row(row: dict) -> dict:
    case = row["gate"]["report"]["cases"][0]
    return {
        "scenario": row["scenario"],
        "route": row["route"],
        "executor_status": row["executor"]["status"],
        "output": row["executor"]["output"],
        "destination": [
            {"sku": read["sku"], "on_hand": read["on_hand"]} for read in row["destination"]
        ],
        "verdict": row["gate"]["report"]["verdict"],
        "reasons": case["conditional_stock"]["candidate"]["reasons"],
        "expected_reserve": "RES-K4",
        "alias_before": row["alias_before"]["generation"],
        "alias_after": row["alias_after"]["generation"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rc1", action="store_true")
    mode.add_argument("--demo", action="store_true")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    options = parser.parse_args()
    lab = os.environ.get("FORGE_VAST_LAB_ID", "")
    if sys.platform != "linux" or (not options.demo and not lab.isdigit()):
        parser.error("Run in the verified Vast lab")
    options.output_dir.mkdir(parents=True, exist_ok=False)
    with options.wheel.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with tempfile.TemporaryDirectory(prefix="forge-conditional-evidence-") as temporary:
        root = Path(temporary)
        if options.rc1:
            evidence = reproduce_rc1(root)
        else:
            demo = [
                exercise_candidate(root / f"demo-{s.name}", s, "code_mode", demo=True) for s in DEMO
            ]
            save_json(options.output_dir / "demo.json", {"rows": [demo_row(row) for row in demo]})
            results = (
                []
                if options.demo
                else [
                    exercise_candidate(root / f"{route}-{scenario.name}", scenario, route)
                    for route in ("direct", "code_mode")
                    for scenario in MATRIX
                ]
            )
            evidence = {"demo": demo, "matrix": results}
            if not options.demo:
                from conditional_stock_consistency import check as check_stock_consistency

                evidence["consistency"] = check_stock_consistency()
                evidence["legacy"] = exercise_legacy(root / "legacy")
                evidence["promotion_expiry"] = exercise_candidate(
                    root / "promotion-expiry", EXPIRES_BEFORE_PROMOTION, "direct"
                )
    receipt = {
        "status": "pass",
        "vast_lab_id": lab,
        "version": version("forge-os-check"),
        "wheel_sha256": digest,
        **evidence,
    }
    (options.output_dir / "conditional-evidence.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": "pass", "wheel_sha256": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
