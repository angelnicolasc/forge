"""Exercise transactional rollout capacity against a reference and a test-owned model."""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import traceback
from collections import Counter
from contextlib import chdir, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from select import select
from tempfile import TemporaryDirectory
from unittest.mock import patch

from forge_check.governed import rollout
from forge_check.governed.control import Control
from forge_check.governed.db import Store, StoreError
from forge_check.governed.ledger import Observation
from forge_check.governed.revision import MAX_SAFE_INTEGER
from governed_oracle import Behavior, Oracle
from governed_trials import (
    MODEL,
    OPERATOR,
    OWNER,
    World,
    candidate,
    collect,
    dump,
    evaluate,
    manifest,
    now,
    rejects,
    request,
    require,
    running,
    state_matches,
)

SUPPORT = Path(__file__).resolve().parents[2] / "examples" / "check-support"
SEEDS = (104729, 130363, 155921)


class Audit:
    """Call both decision functions on the same connection before the transaction writes."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    @contextmanager
    def decisions(self):
        selected = ContextVar("capacity_calculator", default=rollout.capacity)
        plan = rollout.plan_root
        dispatch = rollout.dispatch_block

        def capacity(conn, rollout_id):
            return selected.get()(conn, rollout_id)

        def checked(operation, name, conn, *args, **kwargs):
            actual = operation(conn, *args, **kwargs)
            alternatives = []
            for calculate in (rollout.reference_capacity, rollout.projected_capacity):
                token = selected.set(calculate)
                try:
                    alternatives.append(operation(conn, *args, **kwargs))
                finally:
                    selected.reset(token)
            self.events.append(
                {"decision": name, "reference": repr(alternatives[0]), "projection": repr(alternatives[1])}
            )
            require(actual == alternatives[0] == alternatives[1], f"Decision mismatch: {self.events[-1]}")
            return actual

        def checked_plan(conn, **kwargs):
            return checked(plan, "root", conn, **kwargs)

        def checked_dispatch(conn, task, timestamp):
            return checked(dispatch, "dispatch", conn, task, timestamp)

        with (
            patch.object(rollout, "capacity", capacity),
            patch.object(rollout, "plan_root", checked_plan),
            patch.object(rollout, "dispatch_block", checked_dispatch),
        ):
            yield

    def totals(self, store, rollout_id, expected, phase):
        with store.read() as conn:
            reference = rollout.reference_capacity(conn, rollout_id)
            projection = rollout.projected_capacity(conn, rollout_id)
            event = {
                "transition": phase,
                "rollout": rollout_id,
                "oracle": expected,
                "reference": reference,
                "projection": projection,
            }
            self.events.append(event)
            actual = rollout.capacity(conn, rollout_id)
            event["selected"] = actual
        require(actual == expected == reference == projection, f"Capacity mismatch: {event}")


def create_world(name: str, oracle: Oracle, *, budget: int = 100) -> tuple[World, str, str]:
    definition = dict(budget=budget, ticket_reserve=budget, model_reserve=budget, ticket_cache=30)
    world = World(name, oracle, manifest(**definition))
    proposal, digest = candidate(world, name, **definition)
    evaluate(world, f"{name}-evidence", proposal, digest)
    return world, proposal, digest


def open_rollout(
    world, proposal, name, *, slots=4, budget=10_000, mode="canary", source="direct"
):
    return world.control.start_rollout(
        OWNER, name, proposal, f"{world.path.name}-evidence", 1,
        mode=mode, seed="capacity-oracle", basis_points=10_000,
        ends_at=now() + 180_000_000, max_concurrent=slots, budget_microusd=budget, source=source,
    )


def select_mode(store, mode):
    with store.write() as conn:
        rollout.set_capacity_mode(conn, mode)


def check_destination(oracle, ledger, expected, start):
    wire = oracle.dispatches()[start:]
    seen = Counter(row["attempt_id"] for row in wire)
    require(seen == Counter(expected.keys()), f"Duplicate or missing physical dispatch: {seen}")
    for row in wire:
        attempt = row["attempt_id"]
        if attempt in expected:
            charge, revision = expected[attempt]
            require(row["charge_microusd"] == charge, f"Destination charge differs for {attempt}")
            require(row["revision"] == revision, f"Destination pin differs for {attempt}")
            require(ledger.attempt(attempt)["charge_microusd"] == charge, f"Ledger charge differs for {attempt}")


def deterministic(oracle, audit):
    start = len(oracle.dispatches())
    world, proposal, revision = create_world("deterministic", oracle)
    select_mode(world.store, "compare")
    with audit.decisions():
        shadow = "det-shadow"
        open_rollout(world, proposal, shadow, mode="shadow")
        require(world.task("shadow-task")["allocation"]["cohort"] == "shadow", "Expected shadow")
        oracle.arrange("shadow-ticket", Behavior(charge=9))
        state_matches(world.submit(request("shadow-task", "shadow-charge", "shadow-ticket")), "dispatch", "succeeded")
        world.control.finish_task(OPERATOR, "shadow-task")
        audit.totals(world.store, shadow, (0, 0, 0), "shadow excluded")
        world.control.stop_rollout(OWNER, shadow, 1)

        first, second, zero = "det-first", "det-second", "det-zero"
        run = "det-canary"
        open_rollout(world, proposal, run)
        world.task(first)
        world.task(second)
        audit.totals(world.store, run, (2, 0, 200), "two allocations")
        world.task("det-child", parent_id=first)
        oracle.arrange("det-read", Behavior(charge=70))
        read = request("det-child", "det-read", "det-read")
        state_matches(world.submit(read), "dispatch", "succeeded")
        audit.totals(world.store, run, (2, 70, 200), "child charge")
        state_matches(world.submit(read), "replay", "succeeded")
        state_matches(world.submit(request("det-child", "det-cache", "det-read")), "cached", "cached")
        audit.totals(world.store, run, (2, 70, 200), "replay and cache")
        oracle.arrange("zero-ticket", Behavior(charge=0))
        world.task(zero)
        state_matches(world.submit(request(zero, "det-zero", "zero-ticket")), "dispatch", "succeeded")
        world.control.finish_task(OPERATOR, zero)
        audit.totals(world.store, run, (2, 70, 200), "explicit zero charge")

        duplicate_root = "det-duplicate-root"
        world.task(duplicate_root)
        duplicate = request(duplicate_root, "det-duplicate-claim", "duplicate-ticket")
        require(world.ledger.admit(duplicate).disposition == "dispatch", "Expected reservable duplicate fixture")
        require(world.ledger.mark_dispatched(duplicate.attempt_id), "Duplicate fixture did not claim intent")
        disputed = world.ledger.settle(
            duplicate.attempt_id,
            Observation("det-duplicate", "succeeded", "verified", 70, charge_ref="charge-det-read"),
        )
        require(disputed["charge_microusd"] is None, "Another attempt's bill was recognized twice")
        with rejects(ValueError):
            world.control.finish_task(OPERATOR, duplicate_root)
        audit.totals(world.store, run, (3, 70, 300), "duplicate billing evidence holds exposure")
        world.ledger.settle(
            duplicate.attempt_id,
            Observation("det-duplicate-corrected", "succeeded", "verified", 0, charge_ref="det-duplicate-none"),
        )
        world.control.finish_task(OPERATOR, duplicate_root)
        audit.totals(world.store, run, (2, 70, 200), "unique definitive bill closes duplicate fixture")

        lost = request(second, "det-late", "det-lost", binding=MODEL)
        oracle.arrange("det-lost", Behavior(mode="drop", charge=120))
        state_matches(world.submit(lost), "dispatch", "uncertain")
        balance = world.ledger.balance(second)
        require(balance["pending_microusd"] == 100, "Missing binding reservation")
        with rejects(ValueError):
            world.control.finish_task(OPERATOR, second)
        audit.totals(world.store, run, (2, 70, 200), "unknown child/root charge still held")
        recovered = world.host.reconcile(OWNER, lost.attempt_id)
        require(recovered.attempt["charge_microusd"] == 120, "Late over-cap charge not recognized")
        require(world.ledger.balance(second)["over_limit_microusd"] == 20, "Missing over-cap balance")
        audit.totals(world.store, run, (2, 190, 220), "minimum example: cap max before close")
        world.control.finish_task(OPERATOR, first)
        audit.totals(world.store, run, (1, 190, 190), "minimum example: first close")

        original = Observation("det-repeat", "succeeded", "verified", 70, charge_ref="charge-det-read")
        world.ledger.settle(read.attempt_id, original)
        world.ledger.settle(read.attempt_id, original)
        audit.totals(world.store, run, (1, 190, 190), "post-close charge replay")
        conflict = Observation("det-conflict", "succeeded", "verified", 120, charge_ref="charge-det-read")
        outcome = world.ledger.settle(read.attempt_id, conflict)
        require(outcome["charge_microusd"] == 70, "Contradiction rewrote the recognized charge")
        require(
            any(receipt["phase"] == "charge_conflict" for receipt in world.ledger.receipts("det-child")),
            "Post-close contradiction did not write a conflict receipt",
        )
        with world.store.read() as conn:
            require(conn.execute("SELECT 1 FROM read_cache WHERE source_attempt_id = 'det-read'").fetchone() is None, "Contradiction failed to evict the source cache")
            require(conn.execute("SELECT 1 FROM binding_suspensions WHERE attempt_id = 'det-read'").fetchone() is not None, "Contradiction failed to suspend the binding")
        audit.totals(world.store, run, (1, 190, 190), "post-close contradictory charge")
        state_matches(world.submit(request("det-child", "det-denied", "denied-ticket")), "denied", "denied")
        require(not oracle.dispatches("denied-ticket"), "Closed root made a new dispatch")
        audit.totals(world.store, run, (1, 190, 190), "post-close denial")

        world.control.set_paused(OWNER, "det-pause", True)
        with rejects(PermissionError):
            world.task("det-paused")
        require(world.task(second)["revision"] == revision, "Pause changed existing pin")
        world.control.set_paused(OWNER, "det-unpause", False)
        world.control.stop_rollout(OWNER, run, 1)
        open_rollout(world, proposal, "det-next")
        world.task("det-no-attempts")
        world.control.revoke_revision(OWNER, "det-revoke", revision)
        state_matches(world.submit(request(second, "det-revoked", "revoked-ticket")), "denied", "denied")
        world.control.finish_task(OPERATOR, second)
        audit.totals(world.store, run, (0, 190, 190), "close after newer rollout; original attributed")
        audit.totals(world.store, "det-next", (1, 0, 100), "new rollout unaffected")
        world.control.finish_task(OPERATOR, "det-no-attempts")
        audit.totals(world.store, "det-next", (0, 0, 0), "close without attempts")
    check_destination(oracle, world.ledger, {
        "shadow-charge": (9, world.base.digest),
        "det-read": (70, revision),
        "det-zero": (0, revision),
        "det-late": (120, revision),
    }, start)


def worker():
    _, database, operation, root = sys.argv[1:]
    store = Store(database, create=False)
    control = Control(store)
    audit = Audit()
    print("ready", flush=True)
    require(sys.stdin.readline() == "go\n", "Missing process barrier")
    with audit.decisions():
        if operation == "start":
            result = control.start_task(OPERATOR, root, worker="worker-A")
        else:
            result = control.finish_task(OPERATOR, root)
    print(json.dumps({"result": result, "audit": audit.events}))


def race(database, operation, roots):
    def spawn(root):
        return subprocess.Popen(
            [sys.executable, __file__, "--worker", str(database), operation, root],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    with running(spawn(roots[0])) as first, running(spawn(roots[1])) as second:
        children = (first, second)
        for child in children:
            readable, _, _ = select([child.stdout], [], [], 10)
            require(bool(readable) and child.stdout.readline() == "ready\n", "Race worker never reached barrier")
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        return [collect(child) for child in children]


def thresholds(oracle, audit, *, metric, slots, budget):
    label = f"race-{metric}"
    world, proposal, revision = create_world(label, oracle)
    select_mode(world.store, "compare")
    open_rollout(world, proposal, label, slots=slots, budget=budget)
    results = race(world.db, "start", (f"{label}-1", f"{label}-2"))
    audit.events.extend(event for result in results for event in result["audit"])
    rows = [result["result"] for result in results]
    ordered = sorted(rows, key=lambda row: row["allocation"]["cohort"])
    winner, fallback = ordered
    require(winner["allocation"]["reason"] == "selected" and winner["revision"] == revision, "No final-slot winner")
    require(fallback["allocation"]["reason"] == f"{metric}_limit" and fallback["revision"] == world.base.digest, "Wrong serialized fallback")
    audit.totals(world.store, label, (1, 0, 100), f"concurrent {metric}")
    closed = race(world.db, "finish", (winner["id"], winner["id"]))
    require(closed[0]["result"] == closed[1]["result"], "Concurrent close did not replay")
    audit.totals(world.store, label, (0, 0, 0), "double close")
    if metric == "spend":
        world, proposal, _ = create_world("race-below", oracle)
        select_mode(world.store, "compare")
        open_rollout(world, proposal, "race-below", budget=budget - 1)
        with audit.decisions():
            row = world.task("race-below")
        require(row["allocation"]["reason"] == "spend_limit", "A one-unit deficit was authorized")
        audit.totals(world.store, "race-below", (0, 0, 0), "one below inclusive limit")


@dataclass
class Root:
    canary: bool
    closed: bool = False
    charges: dict[str, int] = field(default_factory=dict)


def seeded(oracle, audit, seed):
    start = len(oracle.dispatches())
    rng = random.Random(seed)
    label = f"seed-{seed}"
    world, proposal, revision = create_world(label, oracle)
    select_mode(world.store, "compare")
    open_rollout(world, proposal, label, slots=4, budget=400)
    roots: dict[str, Root] = {}
    expected = {}
    def totals():
        active = consumed = exposure = 0
        for root in roots.values():
            if root.canary:
                charge = sum(root.charges.values())
                active += int(not root.closed)
                consumed += charge
                exposure += charge if root.closed else max(100, charge)
        return active, consumed, exposure

    with audit.decisions():
        for index in range(80):
            key = f"{label}-{index}"
            opened = [root_id for root_id, root in roots.items() if not root.closed]
            billable = [root_id for root_id in opened if not roots[root_id].charges]
            action = rng.choice(["start", "start", "close", "call"]) if opened else "start"
            if action == "start":
                active, _, exposure = totals()
                expected_reason = (
                    "concurrency_limit" if active >= 4 else
                    "spend_limit" if exposure + 100 > 400 else "selected"
                )
                allocation = world.task(key)["allocation"]
                audit.events.append({"start": key, "oracle_reason": expected_reason, "allocation": allocation})
                require(allocation["reason"] == expected_reason, f"Generated decision diverged: {key}")
                roots[key] = Root(expected_reason == "selected")
            elif action == "call" and billable:
                root_id = rng.choice(billable)
                charge = rng.choice([0, 1, 37, 99, 100])
                oracle.arrange(key, Behavior(charge=charge))
                call = request(root_id, key, key)
                state_matches(world.submit(call), "dispatch", "succeeded")
                roots[root_id].charges[key] = charge
                expected[key] = (charge, revision if roots[root_id].canary else world.base.digest)
                if rng.randrange(2):
                    state_matches(world.submit(call), "replay", "succeeded")
            else:
                root_id = rng.choice(opened)
                world.control.finish_task(OPERATOR, root_id)
                roots[root_id].closed = True
            audit.totals(world.store, label, totals(), key)
    check_destination(oracle, world.ledger, expected, start)


def precision(oracle, audit):
    label = "exact-integers"
    world, proposal, _ = create_world(label, oracle, budget=0)
    select_mode(world.store, "projection")
    open_rollout(world, proposal, label, budget=MAX_SAFE_INTEGER)
    first = world.task("exact-first")
    second = world.task("exact-second")
    # Claim every zero-reserve intent before the first cap breach suspends the binding.
    # With 1025 maximum-size charges on one root, SQLite's signed SUM overflows.
    count = (2**63 - 1) // MAX_SAFE_INTEGER + 1
    attempts = [
        request(first["id"] if index < count else second["id"], f"exact-{index}", f"exact-{index}")
        for index in range(count + 1)
    ]
    for call in attempts:
        require(world.ledger.admit(call).disposition == "dispatch", f"Unclaimed precision fixture {call.attempt_id}")
        require(world.ledger.mark_dispatched(call.attempt_id), f"No dispatch intent {call.attempt_id}")
    for call in attempts:
        observed = world.ledger.settle(
            call.attempt_id,
            Observation(f"observe-{call.attempt_id}", "succeeded", "verified", MAX_SAFE_INTEGER,
                        charge_ref=f"bill-{call.attempt_id}"),
        )
        require(observed["charge_microusd"] == MAX_SAFE_INTEGER, "Maximum legal charge changed")
    exact = MAX_SAFE_INTEGER * (count + 1)
    audit.totals(world.store, label, (2, exact, exact), "signed SQL sum overflow across open roots")
    world.control.finish_task(OPERATOR, first["id"])
    audit.totals(world.store, label, (1, exact, exact), "signed SQL sum overflow on close")
    world.control.finish_task(OPERATOR, second["id"])
    audit.totals(world.store, label, (0, exact, exact), "large closed accumulator round trip")
    select_mode(world.store, "compare")
    audit.totals(world.store, label, (0, exact, exact), "large aggregate verified before reactivation")


def inconsistent(oracle, audit):
    label = "corrupt-fixture"
    world, proposal, _ = create_world(label, oracle)
    select_mode(world.store, "projection")
    open_rollout(world, proposal, label, slots=1, budget=100)
    world.task("corrupt-root")
    corruptions = {
        "missing-summary": "DELETE FROM rollout_capacity_closed",
        "bad-checksum": "UPDATE rollout_capacity_closed SET charge_microusd = '490'",
        "missing-membership": "DELETE FROM rollout_capacity_open",
    }
    for fault, statement in corruptions.items():
        with world.store.write() as conn:
            conn.execute("SAVEPOINT corrupt")
            conn.execute(statement)
            expected = (1, 0, 100)
            observed = rollout.capacity(conn, label)
            require(observed == expected, f"{fault} weakened the conservative fallback")
            with rejects(StoreError):
                rollout.projected_capacity(conn, label)
            with rejects(StoreError):
                rollout.set_capacity_mode(conn, "compare")
            alias = world.control.get_alias(OWNER)
            allocation = rollout.plan_root(
                conn, scope=world.base.document["scope"], root_id="corrupt-contender",
                source="direct", generation=alias["generation"], stable=alias["revision"], now=now(),
            )
            require(allocation is not None and allocation.reason == "concurrency_limit", "Corruption admitted extra canary work")
            audit.events.append({"fault": fault, "canonical_fallback": observed, "reason": allocation.reason})
            conn.execute("ROLLBACK TO corrupt")
            conn.execute("RELEASE corrupt")
    audit.totals(world.store, label, (1, 0, 100), "pristine state after corruption drills")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    lab = os.environ.get("FORGE_VAST_LAB_ID", "")
    if sys.platform != "linux" or not lab.isdigit():
        parser.error("Run on the designated Vast instance")
    evidence = args.output_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    cases = []
    with TemporaryDirectory() as scratch, chdir(scratch), Oracle() as oracle:
        shutil.copytree(SUPPORT, "support")
        trials = [
            ("deterministic", deterministic, {}),
            ("last-slot", thresholds, {"metric": "concurrency", "slots": 1, "budget": 200}),
            ("last-unit", thresholds, {"metric": "spend", "slots": 2, "budget": 100}),
            *((f"generated-{seed}", seeded, {"seed": seed}) for seed in SEEDS),
            ("integer-precision", precision, {}),
            ("fail-closed-projection", inconsistent, {}),
        ]
        for name, trial, kwargs in trials:
            audit = Audit()
            case = {"case": name, "events": audit.events, "status": "pass"}
            cases.append(case)
            try:
                trial(oracle, audit, **kwargs)
            except Exception:
                case.update(status="fail", traceback=traceback.format_exc())
                print(case["traceback"], file=sys.stderr)
        with (evidence / "equivalence-destination.jsonl").open("w", encoding="utf-8") as journal:
            for event in oracle.journal:
                journal.write(json.dumps(event) + "\n")
    passed = all(case["status"] == "pass" for case in cases)
    dump(evidence / "equivalence.json", {
        "status": "pass" if passed else "fail",
        "vast_lab_id": lab,
        "version": version("forge-os-check"),
        "seeds": SEEDS,
        "cases": cases,
    })
    return 0 if passed else 1


if __name__ == "__main__":
    if sys.argv[1:] and sys.argv[1] == "--worker":
        worker()
    else:
        raise SystemExit(main())
