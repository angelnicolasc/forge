"""Compare installed rollout-status behavior with an independent synthetic HTTP destination."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from contextlib import chdir
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from forge_check.governed.ledger import Observation
from governed_oracle import Behavior, Oracle
from governed_trials import (
    OWNER,
    World,
    candidate,
    evaluate,
    manifest,
    request,
    require,
    state_matches,
)
from rollout_trials import matching_ids, open_rollout

SUPPORT = Path(__file__).resolve().parents[2] / "examples" / "check-support"


def trial(oracle: Oracle, *, baseline: bool) -> dict:
    world = World("traffic", oracle, manifest(budget=100_000, model_reserve=40_000))
    proposal, digest = candidate(
        world, "traffic", budget=100_000, model_reserve=40_000, ticket_cache=30
    )
    evaluate(world, "candidate-evidence", proposal, digest)
    snapshots = {}
    start = len(oracle.dispatches())
    shadow_id, canary_id, stable_id, *_ = matching_ids()
    columns = (
        "allocated_roots",
        "roots_with_dispatch_intent",
        "dispatch_intents",
        "roots_pending_confirmation",
    )

    def check(phase: str, rollout: str, **expected: tuple[int, int, int, int]) -> None:
        journal = len(oracle.journal)
        report = world.cli(
            "rollout-status", "--db", str(world.db), "--actor", OWNER, "--rollout", rollout
        )
        require(len(oracle.journal) == journal, "Reading status contacted the destination")
        allocated = sum(row[0] for row in expected.values())
        require(report["eligible_observed_roots"] == allocated, phase)
        observed = report.get("traffic_by_cohort")
        if baseline:
            require("traffic_by_cohort" not in report, "Baseline already supports this contract")
        else:
            rows = [
                {"cohort": cohort, **dict(zip(columns, expected[cohort], strict=True))}
                for cohort in sorted(expected)
            ]
            require(observed == rows, f"{phase}: expected {rows}, observed {observed}")
        snapshots[phase] = {
            "eligible_roots": report["eligible_observed_roots"],
            "traffic_by_cohort": observed,
            "destination_dispatches": len(oracle.dispatches()) - start,
        }

    open_rollout(world, proposal, "shadow", mode="shadow")
    check("empty", "shadow")
    world.task(shadow_id)
    world.submit(request(shadow_id, "shadow-call", "SHADOW"))
    check("shadow", "shadow", shadow=(1, 1, 1, 0))
    require(oracle.dispatches("SHADOW")[0]["revision"] == world.base.digest, "Wrong shadow target")
    world.control.stop_rollout(OWNER, "shadow", 1)

    open_rollout(world, proposal, "canary", mode="canary")
    world.task(canary_id)
    fallback = world.task(stable_id)
    require(fallback["allocation"]["reason"] == "concurrency_limit", "Expected a stable fallback")
    idle = (1, 0, 0, 0)
    check("assigned", "canary", canary=idle, stable=idle)
    parked = request(stable_id, "reserve-only", "RESERVED")
    require(world.ledger.admit(parked).disposition == "dispatch", "Expected an unclaimed reserve")
    check("reserved_without_intent", "canary", canary=idle, stable=idle)
    unsent = Observation("release-reserve", "not_sent", "verified", 0)
    world.ledger.settle(parked.attempt_id, unsent)
    world.task("child", parent_id=canary_id)
    read = request("child", "child-first", "CANDIDATE")
    state_matches(world.submit(read), "dispatch", "succeeded")
    require(world.submit(read)["disposition"] == "replay", "Expected an idempotent replay")
    require(
        world.submit(request("child", "child-cache", "CANDIDATE"))["disposition"] == "cached",
        "Expected a cache hit",
    )
    require(len(oracle.dispatches("CANDIDATE")) == 1, "Expected exactly one physical child read")
    check("child_replay_cache", "canary", canary=(1, 1, 1, 0), stable=idle)

    oracle.arrange("LOST", Behavior(mode="drop", charge=1_000))
    lost = request(canary_id, "canary-lost", "LOST")
    state_matches(world.submit(lost), "dispatch", "uncertain")
    require(len(oracle.dispatches("LOST")) == 1, "Destination did not record the timed-out read")
    stable_read = request(stable_id, "stable-first", "STABLE")
    state_matches(world.submit(stable_read), "dispatch", "succeeded")
    actual_stable = (1, 1, 1, 0)
    check("uncertain", "canary", canary=(1, 1, 2, 1), stable=actual_stable)
    world.control.stop_rollout(OWNER, "canary", 1)
    world.control.revoke_revision(OWNER, "cut", digest)
    state_matches(world.submit(request(canary_id, "blocked", "DENIED")), "denied", "denied")
    require(not oracle.dispatches("DENIED"), "Revoked intent reached the destination")
    check("stopped", "canary", canary=(1, 1, 2, 1), stable=actual_stable)
    world.host.reconcile(OWNER, lost.attempt_id)
    check("reconciled", "canary", canary=(1, 1, 2, 0), stable=actual_stable)
    check("previous_rollout", "shadow", shadow=(1, 1, 1, 0))
    require(len(oracle.dispatches()) - start == 4, "Unexpected physical dispatch count")
    return snapshots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lab = os.environ.get("FORGE_VAST_LAB_ID", "")
    if sys.platform != "linux" or not lab.isdigit():
        parser.error("Run in the designated Vast instance")
    output = args.output.resolve()
    with TemporaryDirectory() as directory, chdir(directory), Oracle() as oracle:
        shutil.copytree(SUPPORT, "support")
        snapshots = trial(oracle, baseline=args.expect == "baseline")
    receipt = {
        "status": "pass",
        "expect": args.expect,
        "vast_lab_id": lab,
        "version": version("forge-os-check"),
        "stages": snapshots,
    }
    output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
