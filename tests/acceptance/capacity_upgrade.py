"""Verify rollout capacity upgrades, writer fencing, crash atomicity and rollback in Vast."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir, closing, contextmanager
from importlib.metadata import version
from pathlib import Path
from select import select
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from capacity_equivalence import SUPPORT, Audit, create_world, open_rollout, select_mode
from forge_check.governed import recovery, rollout
from forge_check.governed.control import Control
from forge_check.governed.db import Store, StoreError, ledger_identity, open_ledger
from forge_check.governed.host import Host, HttpProvider
from forge_check.governed.ledger import Ledger, Observation
from governed_oracle import TOKEN, Behavior, Oracle
from governed_trials import (
    FINGERPRINT,
    OPERATOR,
    OWNER,
    collect,
    dump,
    now,
    rejects,
    request,
    require,
    running,
    state_matches,
)

ROOT = "upgrade-canary"
CAPACITY = (2, 48, 231)
WRITER_ERRORS = (sqlite3.DatabaseError, StoreError)


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def canonical(path):
    """Read exact business rows independently of migration metadata and triggers."""
    with closing(open_ledger(path)) as conn, conn:
        conn.execute("BEGIN")
        identity = ledger_identity(conn)
        checks = conn.execute("PRAGMA integrity_check").fetchall()
        require([row[0] for row in checks] == ["ok"], "SQLite integrity check failed")
        require(not conn.execute("PRAGMA foreign_key_check").fetchall(), "Invalid foreign keys")
        tables = [
            "tasks", "attempts", "receipts", "rollout_allocations", "finished_roots",
            "charge_claims", "binding_suspensions", "aliases",
        ]
        schema = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        rows = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables
            if table in schema
        }
    encoded = json.dumps(rows, sort_keys=True, default=repr).encode("utf-8")
    return {"schema": identity[1], "canonical_sha256": hashlib.sha256(encoded).hexdigest()}


def host(store, oracle):
    return Host(
        store, fingerprint_key=FINGERPRINT,
        provider=HttpProvider(oracle.origin, token=TOKEN, timeout=5),
    )


def seed_v2(name):
    with Oracle() as oracle:
        world, proposal, revision = create_world(name, oracle)
        open_rollout(world, proposal, ROOT, budget=1_000)
        keys = ("upgrade-billed", "upgrade-open", "upgrade-pending", "upgrade-empty")
        for root in keys:
            world.task(root)
        for root, charge in zip(keys, (31, 17, 29), strict=False):
            if root == "upgrade-pending":
                oracle.arrange(root, Behavior(mode="drop", charge=charge))
                expected = "uncertain"
            else:
                oracle.arrange(root, Behavior(charge=charge))
                expected = "succeeded"
            state_matches(world.submit(request(root, root, root)), "dispatch", expected)
        for root in (keys[0], keys[-1]):
            world.control.finish_task(OPERATOR, root)
        with world.store.read() as conn:
            require(rollout.capacity(conn, ROOT) == CAPACITY, "Legacy fixture counted the wrong funds")
        return {
            "ledger": str(world.db), "balances": {root: world.ledger.balance(root) for root in keys},
            "revision": revision, "history": canonical(world.db), "version": version("forge-os-check"),
            "journal": oracle.journal, "outcomes": list(oracle.outcomes.values()),
        }


class CrashStore(Store):
    """Stop the process at a precise product write boundary on a disposable ledger."""

    def __init__(self, path, when):
        self.when = when
        super().__init__(path, create=False)

    @contextmanager
    def write(self):
        with super().write() as conn:
            yield conn
            if self.when == "before":
                sigkill()
        sigkill()


def sigkill():
    print("transaction boundary", flush=True)
    os.kill(os.getpid(), signal.SIGKILL)


def legacy_live(path, origin):
    store = Store(path, create=False)
    control = Control(store)
    old_host = Host(store, fingerprint_key=FINGERPRINT, provider=HttpProvider(origin, token=TOKEN))
    print("ready", flush=True)
    require(sys.stdin.readline() == "go\n", "Missing old-writer barrier")
    blocked = []
    for operation in (
        lambda: control.start_task(OPERATOR, "legacy-after-backfill", worker="legacy"),
        lambda: old_host.invoke(request("upgrade-open", "legacy-after-backfill", "legacy-ticket")),
        lambda: old_host.ledger.settle("upgrade-pending", Observation("legacy-charge", "succeeded", "verified", 29)),
    ):
        try:
            operation()
        except WRITER_ERRORS as exc:
            blocked.append(str(exc))
        else:
            raise AssertionError("An initialized legacy writer bypassed the migrated ledger")
    return {"blocked": blocked, "version": version("forge-os-check")}


def worker():
    action, *args = sys.argv[2:]
    if action == "seed-v2":
        value = seed_v2(args[0])
    elif action == "v1-to-v2":
        path, backup = map(Path, args)
        value = recovery.migrate(path, OWNER, backup)
    elif action == "open-old":
        with rejects(StoreError):
            Store(args[0], create=False)
        value = {"open": "rejected"}
    elif action == "legacy-live":
        value = legacy_live(*args)
    elif action == "migration-crash":
        path, backup = map(Path, args)
        install = recovery.install_v3_schema

        def interrupt(conn):
            install(conn)
            sigkill()

        with patch.object(recovery, "install_v3_schema", interrupt):
            recovery.migrate(path, OWNER, backup)
        raise RuntimeError("Migration did not reach the crash checkpoint")
    elif action == "write-crash":
        path, when, operation, root = args
        store = CrashStore(path, when)
        if operation == "allocation":
            Control(store).start_task(OPERATOR, root, worker="crash")
        elif operation == "charge":
            Ledger(store, fingerprint_key=FINGERPRINT).settle(
                root, Observation(f"external-{root}", "succeeded", "verified", 37, charge_ref=f"fee-{root}")
            )
        elif operation == "close":
            Control(store).finish_task(OPERATOR, root)
        raise RuntimeError("Write did not reach the crash checkpoint")
    else:
        raise ValueError(f"Unknown child action {action}")
    print(json.dumps(value))


def command(interpreter, *args):
    return [interpreter, __file__, "--worker", *map(str, args)]


def run(interpreter, *args):
    return collect(subprocess.Popen(command(interpreter, *args), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))


def interrupted(*args):
    process = subprocess.run(command(sys.executable, *args), capture_output=True, text=True, timeout=30, check=False)
    require(
        process.returncode == -signal.SIGKILL and process.stdout == "transaction boundary\n",
        f"Expected SIGKILL at {args}: {process.returncode} {process.stderr}",
    )


def measure_migration(path, backup):
    entered = Event()
    waiting = Event()
    wait_ms = []
    backfill_ms = []
    install = recovery.install_v3_schema

    def contender():
        require(entered.wait(timeout=10), "Migration never entered backfill")
        with closing(open_ledger(path)) as conn:
            started = time.perf_counter_ns()
            waiting.set()
            conn.execute("BEGIN IMMEDIATE")
            wait_ms.append((time.perf_counter_ns() - started) / 1_000_000)
            conn.rollback()

    def measured(conn):
        entered.set()
        require(waiting.wait(timeout=5), "Contender never reached writer acquisition")
        started = time.perf_counter_ns()
        install(conn)
        backfill_ms.append((time.perf_counter_ns() - started) / 1_000_000)

    wal = Path(f"{path}-wal")
    def wal_bytes():
        return wal.stat().st_size if wal.exists() else 0
    with closing(open_ledger(path)), ThreadPoolExecutor(max_workers=1) as workers:
        before = wal_bytes()
        contender_result = workers.submit(contender)
        start = time.perf_counter_ns()
        with patch.object(recovery, "install_v3_schema", measured):
            manifest = recovery.migrate(path, OWNER, backup)
        elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
        after = wal_bytes()
        contender_result.result(timeout=10)
    require(wait_ms and backfill_ms, "Missing migration timings")
    return manifest, {
        "elapsed_ms": elapsed_ms,
        "backfill_ms": backfill_ms[0],
        "max_observed_writer_wait_ms": max(wait_ms),
        "wal_bytes_before": before,
        "wal_bytes_at_commit": after,
    }


def verify_v2(interpreter, name, oracle, report):
    original = run(interpreter, "seed-v2", name)
    path = Path(original["ledger"])
    with oracle.changed:
        oracle.journal.extend(original["journal"])
        for observation in original["outcomes"]:
            oracle.outcomes[observation["operation_key"], observation["attempt_id"]] = observation
    # The old interpreter and its already-initialized Store survive the transaction.
    process = subprocess.Popen(
        command(interpreter, "legacy-live", path, oracle.origin),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    initial = canonical(path)
    report["pre_upgrade"] = initial
    with running(process):
        readable, _, _ = select([process.stdout], [], [], 10)
        require(bool(readable) and process.stdout.readline() == "ready\n", "Legacy writer failed to initialize")
        abandoned = Path(f"{name}-interrupted.sqlite3")
        interrupted("migration-crash", path, abandoned)
        require(canonical(path) == initial, "A killed migration changed the authoritative ledger")
        require(abandoned.is_file(), "Killed migration lost the pre-migration snapshot")
        report["interrupted_backup_sha256"] = digest(abandoned)
        clean = Path(f"{name}-backup-v2.sqlite3")
        source_manifest, metrics = measure_migration(path, clean)
        require(source_manifest["sha256"] == digest(clean), "Migration backup digest mismatch")
        report.update(snapshot=source_manifest, migration=metrics, post_upgrade=canonical(path))
        require(report["post_upgrade"]["schema"] > initial["schema"], "Upgrade did not bump the schema")
        require(report["post_upgrade"]["canonical_sha256"] == initial["canonical_sha256"], "Migration changed canonical rows")
        process.stdin.write("go\n")
        process.stdin.flush()
        report["old_live"] = collect(process)
    report["old_new"] = run(interpreter, "open-old", path)
    require(not oracle.dispatches("legacy-ticket"), "The active legacy process reached the destination")
    store = Store(path, create=False)
    runtime = host(store, oracle)
    audit = Audit()
    for root, balance in original["balances"].items():
        require(runtime.ledger.balance(root) == balance, f"Upgrade changed {root} balance")
    select_mode(store, "projection")
    audit.totals(store, ROOT, CAPACITY, "v2 populated backfill")
    pending = runtime.ledger.attempt("upgrade-pending")
    require(pending["status"] == "uncertain" and pending["reserved_microusd"] == 100, "Upgrade lost pending exposure")

    # A sealed snapshot restored while the source is quiesced starts fenced.
    restore_path = Path(f"{name}-restored.sqlite3")
    restored = recovery.restore(clean, restore_path, source_manifest)
    restored_store = Store(restore_path, create=False)
    restored_host = host(restored_store, oracle)
    restored_control = Control(restored_store)
    with rejects(StoreError):
        restored_control.start_task(OPERATOR, "restore-blocked", worker="restore")
    require(restored["fenced"] and restored["unresolved_attempts"] == 1, "Restore dropped the gate")
    audit.totals(restored_store, ROOT, CAPACITY, "v2 backup restore backfill")
    result = restored_host.reconcile(OWNER, "upgrade-pending")
    require(result.attempt["charge_microusd"] == 29, "Restore lost a destination charge")
    resumed = recovery.resume(
        restored_store, OWNER, restored["recovery_id"], audit_ref="capacity-external-audit",
        covered_through_us=now(), source_quiesced=True, post_snapshot_effects=0,
    )
    require(not resumed["fenced"], "Reconciled restore did not resume")
    restored_control.finish_task(OPERATOR, "upgrade-pending")
    audit.totals(restored_store, ROOT, (1, 77, 160), "restored pending charge and close")
    disk = Path(f"{name}-backup-v3.sqlite3")
    current_manifest = recovery.backup(restored_store, OWNER, disk)
    current_path = Path(f"{name}-restored-v3.sqlite3")
    current = recovery.restore(disk, current_path, current_manifest)
    audit.totals(Store(current_path, create=False), ROOT, (1, 77, 160), "v3 backup and fenced restore")
    report.update(restored_v2=restored, restored_v3=current, v3_snapshot=current_manifest)

    # Revert the calculator over the same current authoritative file and reconcile by ID.
    select_mode(store, "reference")
    runtime = host(Store(path, create=False), oracle)
    settled = runtime.reconcile(OWNER, "upgrade-pending")
    require(settled.attempt["charge_microusd"] == 29, "Rollback lost pending charge")
    replay = runtime.invoke(request("upgrade-pending", "upgrade-pending", "upgrade-pending"))
    require(replay.disposition == "replay", "Rollback changed original attempt identity")
    Control(store).finish_task(OPERATOR, "upgrade-pending")
    audit.totals(store, ROOT, (1, 77, 160), "reference rollback on same ledger")
    select_mode(store, "projection")
    audit.totals(store, ROOT, (1, 77, 160), "projection restored after rollback")
    journal = count_by_attempt(oracle, {"upgrade-billed", "upgrade-open", "upgrade-pending"})
    require(set(journal.values()) == {1} and len(journal) == 3, "Migration or rollback repeated an effect")
    report["physical_dispatches_by_attempt"] = journal
    report["capacity"] = audit.events


def count_by_attempt(oracle, attempts):
    return {
        attempt: sum(row["attempt_id"] == attempt for row in oracle.dispatches())
        for attempt in sorted(attempts)
    }


def verify_v1(v1, baseline, oracle, report):
    path = Path("seed-v1.sqlite3").resolve()
    oracle.arrange("P20-v1-pending", Behavior(mode="drop"))
    legacy_runner = Path(__file__).with_name("recovery_trials.py")
    state = collect(subprocess.Popen(
        [v1, str(legacy_runner), "--worker", "legacy", str(path), oracle.origin],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ))
    initial = canonical(path)
    old = Path("v1-before-v2.sqlite3")
    first = run(baseline, "v1-to-v2", path, old)
    require(first["sha256"] == digest(old), "v1 migration snapshot digest mismatch")
    middle = canonical(path)
    second_path = Path("v2-before-v3.sqlite3")
    second, metrics = measure_migration(path, second_path)
    final = canonical(path)
    require(initial["schema"] == 1 and middle["schema"] == 2 and final["schema"] > 2, "Missing schema hop")
    runtime = host(Store(path, create=False), oracle)
    control = Control(runtime.store)
    require(control.get_alias(OWNER) == state["alias"], "Schema hops changed the alias")
    require(control.get_decision(OWNER, "P20-v1-promotion") == state["decision"], "Schema hops changed the Gate")
    for key, balance in state["balances"].items():
        require(runtime.ledger.balance(key) == balance, "Schema hops lost a reservation")
    from_v1 = Path("restored-v1.sqlite3")
    restored = recovery.restore(old, from_v1, first)
    require(restored["fenced"] and restored["unresolved_attempts"] == 1, "v1 restore lost its fence")
    recovered = host(Store(from_v1, create=False), oracle).reconcile(OWNER, "P20-v1-pending")
    require(recovered.attempt["charge_microusd"] == 13_000, "v1 restore lost the terminal charge")
    require(count_by_attempt(oracle, {"P20-v1-known", "P20-v1-pending"}) == {"P20-v1-known": 1, "P20-v1-pending": 1}, "Legacy recovery repeated a dispatch")
    report.update(schemas=[initial, middle, final], v1_snapshot=first, v2_snapshot=second, migration=metrics, restored_v1=restored)


def verify_crashes(oracle, report):
    world, proposal, revision = create_world("crash-boundaries", oracle)
    select_mode(world.store, "projection")
    open_rollout(world, proposal, "crash-boundaries")
    audit = Audit()
    key = "crash-root"
    for operation, totals in (
        ("allocation", ((0, 0, 0), (1, 0, 100))),
        ("charge", ((1, 0, 100), (1, 37, 100))),
        ("close", ((1, 37, 100), (0, 37, 37))),
    ):
        if operation == "charge":
            # Record the side effect independently, then kill the process settling it.
            call = request(key, key, key)
            admission = world.ledger.admit(call)
            require(admission.disposition == "dispatch" and world.ledger.mark_dispatched(key), "Missing durable intent")
            attempt = world.ledger.attempt(key)
            oracle.arrange(key, Behavior(charge=37))
            observation = HttpProvider(oracle.origin, token=TOKEN).dispatch({
                "configuration": {"effect": "write"}, "attempt_id": key,
                "operation_id": key, "operation_key": attempt["operation_key"],
                "binding_id": call.binding_id, "revision": revision, "input": {"ticket_id": key},
                "stream": False,
            })
            require(observation["charge_microusd"] == 37, "Missing independent effect")
        for when, expected in zip(("before", "after"), totals, strict=True):
            interrupted("write-crash", world.db, when, operation, key)
            reopened = Store(world.db, create=False)
            audit.totals(reopened, "crash-boundaries", expected, f"{operation}: {when} commit")
            if operation == "charge" and when == "before":
                require(world.ledger.attempt(key)["charge_microusd"] is None, "Lost atomic settlement")
        if operation == "charge":
            known = world.host.reconcile(OWNER, key).attempt
            require(known["charge_microusd"] == 37, "Reconcile changed the already-booked amount")
    require(count_by_attempt(oracle, {key}) == {key: 1}, "Crash replay repeated a destination effect")
    report["capacity"] = audit.events
    report["independent_dispatches"] = oracle.dispatches(key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", required=True)
    parser.add_argument("--public-python", required=True)
    parser.add_argument("--v1-python", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    lab = os.environ.get("FORGE_VAST_LAB_ID", "")
    if sys.platform != "linux" or not lab.isdigit():
        parser.error("Run on the designated Vast instance")
    evidence = args.output_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    cases = []
    interpreters = {key: str(Path(value).absolute()) for key, value in (
        ("baseline", args.baseline_python), ("public", args.public_python), ("v1", args.v1_python)
    )}
    with TemporaryDirectory() as scratch, chdir(scratch):
        shutil.copytree(SUPPORT, "support")
        for name, trial in (
            ("v2-reference", lambda oracle, report: verify_v2(interpreters["baseline"], "reference-v2", oracle, report)),
            ("v2-public", lambda oracle, report: verify_v2(interpreters["public"], "public-v2", oracle, report)),
            ("v1-v2-v3", lambda oracle, report: verify_v1(interpreters["v1"], interpreters["baseline"], oracle, report)),
            ("allocation-charge-close-crashes", verify_crashes),
        ):
            report = {"case": name, "status": "pass"}
            cases.append(report)
            with Oracle() as oracle:
                try:
                    trial(oracle, report)
                except Exception:
                    report.update(status="fail", traceback=traceback.format_exc())
                    print(report["traceback"], file=sys.stderr)
                report["destination_journal"] = oracle.journal
    passed = all(case["status"] == "pass" for case in cases)
    dump(evidence / "upgrade-recovery.json", {
        "status": "pass" if passed else "fail", "cases": cases, "vast_lab_id": lab,
        "version": version("forge-os-check"), "v1_transform": "installed control wheel",
    })
    return 0 if passed else 1


if __name__ == "__main__":
    if sys.argv[1:] and sys.argv[1] == "--worker":
        worker()
    else:
        raise SystemExit(main())
