"""Installed-wheel rollout cases with independent HTTP dispatch evidence."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from governed_oracle import Behavior, Oracle
from governed_trials import (
    MODEL,
    OPERATOR,
    OTHER,
    OWNER,
    SCOPE,
    World,
    candidate,
    dump,
    evaluate,
    manifest,
    now,
    rejects,
    request,
    require,
    state_matches,
)

_SEED = "support-p17-cohort-v1"
_BASIS_POINTS = 5000


def expected_bucket(task: str) -> int:
    payload = json.dumps([SCOPE, _SEED, task], separators=(",", ":")).encode()
    digest = hashlib.sha256(b"forge.rollout.v1\0" + payload).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def matching_ids() -> list[str]:
    eligible = [f"P17-root-{suffix}" for suffix in range(120)]
    return [key for key in eligible if expected_bucket(key) < _BASIS_POINTS][:5]


def stage(world: World, *, effect: str) -> tuple[str, str]:
    proposal, revision = candidate(
        world, "rollout-candidate", effect=effect, budget=100_000, model_reserve=40_000
    )
    evaluate(world, "candidate-evidence", proposal, revision)
    return proposal, revision


def open_rollout(world: World, proposal: str, name: str, *, mode: str) -> dict:
    return world.control.start_rollout(
        OWNER,
        name,
        proposal,
        "candidate-evidence",
        1,
        mode=mode,
        seed=_SEED,
        basis_points=_BASIS_POINTS,
        ends_at=now() + 90_000_000,
        max_concurrent=1,
        budget_microusd=130_000,
    )


def verify_rollout(oracle: Oracle) -> dict:
    world = World("P17", oracle, manifest(effect="write", budget=100_000, model_reserve=40_000))
    proposal, revision = stage(world, effect="write")
    shadow_id, first_id, second_id, third_id, window_id = matching_ids()
    shadow_open = open_rollout(world, proposal, "shadow-trial", mode="shadow")
    shadow = world.task(shadow_id)
    require(shadow["allocation"]["cohort"] == "shadow", "Shadow did not record a proposal")
    require(shadow["revision"] == world.base.digest, "Shadow changed the effective revision")
    state_matches(
        world.submit(request(shadow_id, "p17-baseline", "P17-shadow-write")),
        "dispatch",
        "succeeded",
    )
    shadow_rows = oracle.dispatches("P17-shadow-write")
    require(len(shadow_rows) == 1 and shadow_rows[0]["effect_created"], "Shadow duplicated a write")
    require(shadow_rows[0]["revision"] == world.base.digest, "Shadow sent candidate configuration")
    preview = world.ledger.receipts(shadow_id)[0]["evidence"]["shadow"]
    require(
        preview
        == {"revision": revision, "preflight": "preflight_passed", "provider_dispatched": False},
        "Shadow preview lacked a scoped decision",
    )
    world.control.finish_task(OPERATOR, shadow_id)
    shadow_status = world.control.stop_rollout(OWNER, "shadow-trial", 1)
    require(shadow_status["exposure_microusd"] == 0, "Shadow reserved candidate spend")

    canary = open_rollout(world, proposal, "canary-trial", mode="canary")
    start = threading.Barrier(2)

    def admit(task_id: str) -> dict:
        start.wait(timeout=5)
        return world.task(task_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(admit, key) for key in (first_id, second_id)]
        rows = [future.result(timeout=8) for future in futures]
    winners = [row for row in rows if row["allocation"]["cohort"] == "canary"]
    fallbacks = [row for row in rows if row["allocation"]["reason"] == "concurrency_limit"]
    require(len(winners) == len(fallbacks) == 1, "Concurrent roots exceeded the one-slot cap")
    require(
        all(row["allocation"]["bucket"] == expected_bucket(row["id"]) for row in rows),
        "Buckets differ from the pinned cohort rule",
    )
    pinned = winners[0]
    require(pinned["revision"] == revision and pinned["generation"] == 1, "Wrong candidate pin")
    child = world.task("P17-child", parent_id=pinned["id"])
    require(child["revision"] == revision, "The child did not inherit canary revision")
    oracle.arrange("P17-canary-write", Behavior(mode="drop", charge=43_000))
    work = request(child["id"], "p17-drop", "P17-canary-write")
    state_matches(world.submit(work), "dispatch", "uncertain")
    with rejects(ValueError):
        world.control.finish_task(OPERATOR, pinned["id"])
    held = world.control.get_rollout(OWNER, "canary-trial")
    require(held["exposure_microusd"] == 100_000 and held["active_roots"] == 1, "Lost unknown cap")
    require(
        world.host.reconcile(OWNER, "p17-drop").attempt["confirmation"] == "verified",
        "Effect unconfirmed",
    )
    world.control.finish_task(OPERATOR, pinned["id"])
    refused = world.task(third_id)
    require(
        refused["allocation"]["reason"] == "spend_limit"
        and refused["revision"] == world.base.digest,
        "Settled cost failed to cap future candidate spend",
    )
    state_matches(
        world.submit(request(child["id"], "p17-after-close", "P17-closed")), "denied", "denied"
    )
    require(not oracle.dispatches("P17-closed"), "Finished root dispatched a new write")
    resumed = world.task(fallbacks[0]["id"])
    require(resumed == fallbacks[0], "Reopening a fallback changed its cohort")
    final_budget = world.control.get_rollout(OWNER, "canary-trial")
    require(
        final_budget["exposure_microusd"] == 43_000, "The finished root did not release headroom"
    )
    world.control.stop_rollout(OWNER, "canary-trial", 1)

    second = open_rollout(world, proposal, "expiry-trial", mode="canary")
    active = world.task(window_id)
    require(active["revision"] == revision, "Controlled candidate was not assigned")
    state_matches(
        world.submit(request(active["id"], "p17-window-model", "P17-model-before", binding=MODEL)),
        "dispatch",
        "succeeded",
    )
    require(
        world.control.promote(OWNER, "p17-promotion", proposal, "candidate-evidence", 1)["status"]
        == "applied",
        "Promotion failed to take over the canary generation",
    )
    require(world.control.rollback(OWNER, "p17-return", 2)["generation"] == 3, "Rollback failed")
    back = world.task("P17-after-rollback")
    require(back["revision"] == world.base.digest, "New tasks ignored rollback")
    state_matches(
        world.submit(request(back["id"], "p17-stable-model", "P17-model-after", binding=MODEL)),
        "dispatch",
        "succeeded",
    )
    require(world.task(window_id)["revision"] == revision, "Rollback rewrote an in-flight task")
    pending = request(window_id, "p17-before-expiry", "P17-window-expired")
    require(world.ledger.admit(pending).disposition == "dispatch", "Expected a pre-window claim")
    with patch("forge_check.governed.ledger._utc", return_value=second["ends_at"]):
        require(
            not world.ledger.mark_dispatched(pending.attempt_id), "Expired canary claim escaped"
        )
    require(
        world.ledger.attempt(pending.attempt_id)["reason"] == "canary_window_expired",
        "Expiry was not recorded",
    )
    seen = list(oracle.dispatches())
    expected = {"P17-model-before": revision, "P17-model-after": world.base.digest}
    require(
        all(oracle.dispatches(key)[0]["revision"] == rev for key, rev in expected.items()),
        "The destination received the wrong effective revision",
    )
    require(len(oracle.dispatches("P17-canary-write")) == 1, "A canary write was duplicated")
    return {
        "shadow": shadow_open,
        "shadow_after": shadow_status,
        "shadow_preview": preview,
        "canary": canary,
        "concurrent_allocations": rows,
        "allocation_held": held,
        "budget_after_finish": final_budget,
        "admitted_after_rollback": back,
        "dispatches": seen,
    }


def verify_lease(oracle: Oracle) -> dict:
    world = World("P18", oracle)
    lease = world.control.issue_alias_lease(OPERATOR, "P18-lease", ttl_seconds=60)
    with patch("forge_check.governed.control._alias", side_effect=OSError("alias reader offline")):
        with rejects(OSError):
            world.task("P18-cold")
        with rejects(PermissionError):
            world.control.start_task(OPERATOR, "P18-missing", worker="A", alias_lease="missing")
        with rejects(PermissionError):
            world.control.start_task(OTHER, "P18-foreign", worker="A", alias_lease=lease["id"])
        pinned = world.control.start_task(OPERATOR, "P18-warm", worker="A", alias_lease=lease["id"])
        state_matches(
            world.submit(request(pinned["id"], "p18-dispatch", "P18-lease-read")),
            "dispatch",
            "succeeded",
        )
        with patch("forge_check.governed.control.now_micros", return_value=lease["expires_at"]):
            with rejects(PermissionError):
                world.control.start_task(
                    OPERATOR, "P18-expired", worker="A", alias_lease=lease["id"]
                )
            require(
                world.control.start_task(OPERATOR, pinned["id"], worker="A")["revision"]
                == world.base.digest,
                "Expiry changed a preexisting pin",
            )
    with world.store.read() as connection:
        task_ids = [row[0] for row in connection.execute("SELECT id FROM tasks ORDER BY id")]
    require(task_ids == [pinned["id"]], "Alias-reader failure left unauthorized tasks")

    other_store = World("P18-isolated-ledger", oracle)
    with rejects(PermissionError):
        other_store.control.start_task(OPERATOR, "P18-copy", worker="A", alias_lease=lease["id"])
    proposal, replacement = candidate(other_store, "P18-replacement")
    evaluate(other_store, "P18-replacement-evidence", proposal, replacement)
    last_lease = other_store.control.issue_alias_lease(
        OPERATOR, "P18-generation-one", ttl_seconds=60
    )
    applied = other_store.control.promote(
        OWNER, "P18-change", proposal, "P18-replacement-evidence", 1
    )
    require(applied["status"] == "applied", "Fixture promotion failed")
    with (
        patch("forge_check.governed.control._alias", side_effect=OSError("alias reader offline")),
        rejects(PermissionError),
    ):
        other_store.control.start_task(
            OPERATOR, "P18-old-generation", worker="A", alias_lease=last_lease["id"]
        )
    operation = request(pinned["id"], "p18-claimed", "P18-revoked-attempt")
    require(
        world.ledger.admit(operation).disposition == "dispatch", "Expected to reserve before revoke"
    )
    revocation = world.control.revoke_revision(OWNER, "P18-revocation", world.base.digest)
    with (
        patch("forge_check.governed.control._alias", side_effect=OSError("alias reader offline")),
        rejects(PermissionError),
    ):
        world.control.start_task(OPERATOR, "P18-revoked", worker="A", alias_lease=lease["id"])
    require(not world.ledger.mark_dispatched(operation.attempt_id), "Revoked revision dispatched")
    require(not oracle.dispatches("P18-revoked-attempt"), "Revoked attempt reached destination")
    state = world.ledger.attempt(operation.attempt_id)
    require(
        state["reason"] == "revision_revoked" and state["reserved_microusd"] == 0,
        "Wrong revocation",
    )
    evidence = oracle.dispatches("P18-lease-read")
    require(
        len(evidence) == 1 and evidence[0]["revision"] == world.base.digest, "Warm lease mismatched"
    )
    return {
        "lease": lease,
        "warm_task": pinned,
        "age_micros": pinned["started_at"] - lease["issued_at"],
        "cold_admitted": False,
        "expired_admitted": False,
        "previous_generation_admitted": False,
        "revocation": revocation,
        "attempt_after_revocation": state,
        "scope": "single_writer_local_disk",
        "lease_accepted_by_unrelated_ledger": False,
        "provider": evidence,
    }


def main(output: Path) -> int:
    tests: dict[str, str] = {}
    failures: dict[str, str] = {}
    with Oracle() as oracle:
        for name, check in (("P17", verify_rollout), ("P18", verify_lease)):
            try:
                dump(output / f"{name}.json", check(oracle))
                tests[name] = "pass"
            except Exception as error:
                tests[name] = "fail"
                failures[name] = str(error)
                (output / f"{name}.traceback").write_text(traceback.format_exc(), encoding="utf-8")
        dump(output / "destination-journal.json", oracle.journal)
    report: dict[str, object] = {"tests": tests, "failures": failures}
    if failures:
        report["failure"] = "Rollout acceptance failed"
    dump(output / "trials.json", report)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
