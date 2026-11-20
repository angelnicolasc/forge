"""Installed-wheel recovery cases. The HTTP destination owns its effect journal."""

from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from contextlib import closing
from pathlib import Path

from forge_check.governed.control import Control
from forge_check.governed.db import Store, StoreError
from forge_check.governed.host import Host, HttpProvider
from forge_check.governed.ledger import Ledger, LedgerError, Observation
from forge_check.governed.revision import Revision
from governed_oracle import TOKEN, Behavior, Oracle
from governed_trials import (
    ACTORS,
    FINGERPRINT,
    OPERATOR,
    OWNER,
    SCOPE,
    collect,
    dump,
    manifest,
    now,
    read,
    rejects,
    request,
    require,
    running,
)


def _host(store: Store, origin: str, *, provider: HttpProvider | None = None) -> Host:
    return Host(
        store,
        fingerprint_key=FINGERPRINT,
        provider=provider or HttpProvider(origin, token=TOKEN, timeout=12),
    )


def _process(interpreter: str, *arguments: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [interpreter, __file__, "--worker", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _checkpoint(marker: Path) -> None:
    marker.touch()
    while True:
        signal.pause()


class AckCheckpointProvider(HttpProvider):
    def __init__(self, origin: str, marker: Path) -> None:
        super().__init__(origin, token=TOKEN, timeout=12)
        self.marker = marker

    def dispatch(self, body: dict[str, object]) -> dict[str, object]:
        observation = super().dispatch(body)
        _checkpoint(self.marker)
        return observation


class FaultStore(Store):
    """Inject real SQLite limits on the trial connection only."""

    def __init__(self, path: str, fault: str) -> None:
        self.fault = fault
        super().__init__(path, create=False)

    def _connection(self) -> sqlite3.Connection:
        connection = super()._connection()
        connection.execute("PRAGMA busy_timeout=120")
        if self.fault == "full":
            pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
            connection.execute(f"PRAGMA max_page_count={pages}")
        return connection


def _legacy_seed(path: str, origin: str) -> dict:
    store = Store(path)
    control = Control(store)
    stable = Revision.parse(manifest(effect="write"))
    candidate = Revision.parse(manifest("lab-model-p20", effect="write"))
    control.bootstrap(SCOPE, stable, read(Path("support/cases.json")), ACTORS)
    control.register(OWNER, candidate)
    control.propose(OWNER, "P20-v1-proposal", candidate.digest)
    baseline = {**read(Path("support/baseline.json")), "revision": stable.digest}
    current = {**read(Path("support/candidate-pass.json")), "revision": candidate.digest}
    control.evaluate(
        OWNER,
        "P20-v1-evidence",
        "P20-v1-proposal",
        baseline,
        current,
        observed_at=now(),
        generation=1,
    )
    control.promote(OWNER, "P20-v1-promotion", "P20-v1-proposal", "P20-v1-evidence", 1)
    host = _host(store, origin)
    for key in ("P20-v1-known", "P20-v1-pending"):
        control.start_task(OPERATOR, key, worker="legacy-worker")
        reply = host.invoke(request(key, key, key))
        expected = "uncertain" if key.endswith("pending") else "succeeded"
        require(reply.attempt["status"] == expected, "Unexpected legacy fixture result")
    return {
        "alias": control.get_alias(OWNER),
        "decision": control.get_decision(OWNER, "P20-v1-promotion"),
        "balances": {
            name: host.ledger.balance(name) for name in ("P20-v1-known", "P20-v1-pending")
        },
    }


def worker(arguments: list[str]) -> int:
    action, db, origin, *arguments = arguments
    if action == "legacy":
        value = _legacy_seed(db, origin)
    elif action == "legacy-inspect":
        try:
            value = {"alias": Control(Store(db, create=False)).get_alias(OWNER)}
        except StoreError:
            value = {"unsupported_schema": True}
    elif action == "storage-fault":
        fault, operation = arguments
        store = FaultStore(db, fault)
        try:
            if operation == "release":
                Control(store).promote(OWNER, f"P20-{fault}", "P20-proposal", "P20-evidence", 2)
            else:
                _host(store, origin).invoke(
                    request("P20-full-task", "P20-full-call", "P20-full-call")
                )
        except sqlite3.Error as error:
            value = {"name": error.sqlite_errorname, "code": error.sqlite_errorcode}
        else:
            raise AssertionError("Storage fault did not reach the product operation")
    elif action == "crash":
        phase, task, marker_name = arguments
        marker = Path(marker_name)
        store = Store(db, create=False)
        operation = request(task, "ticket", task, attempt=task)
        if phase == "before_admit":
            _checkpoint(marker)
        if phase == "after_reserve":
            admission = Ledger(store, fingerprint_key=FINGERPRINT).admit(operation)
            require(admission.disposition == "dispatch", "Crash fixture did not reserve")
            _checkpoint(marker)
        provider = AckCheckpointProvider(origin, marker) if phase == "after_provider_ack" else None
        result = _host(store, origin, provider=provider).invoke(operation)
        if phase == "before_client_ack":
            require(result.attempt["status"] == "succeeded", "Crash fixture did not settle")
            _checkpoint(marker)
        raise AssertionError("Crash fixture reached its unexpected return")
    else:
        raise ValueError(f"Unknown worker action {action}")
    print(json.dumps(value))
    return 0


def _stop_at_point(oracle: Oracle, store: Store, phase: str, task: str) -> dict:
    marker = Path.cwd() / f"{task}.ready"
    if phase == "after_dispatch":
        oracle.arrange(task, Behavior(mode="hold"))
    child = _process(
        sys.executable, "crash", str(store.path), oracle.origin, phase, task, str(marker)
    )
    started = time.monotonic()
    try:
        with running(child):
            if phase == "after_dispatch":
                oracle.await_dispatch(task)
            else:
                deadline = time.monotonic() + 10
                while not marker.is_file() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                require(marker.is_file(), "Worker never reached its lifecycle boundary")
            child.kill()
            child.communicate(timeout=5)
            require(child.returncode == -signal.SIGKILL, "Worker did not receive SIGKILL")
    finally:
        if phase == "after_dispatch":
            oracle.release(task)
    return {"signal": "SIGKILL", "seconds_until_exit": round(time.monotonic() - started, 6)}


def verify_p19(oracle: Oracle) -> dict:
    store = Store("P19.sqlite3")
    control = Control(store)
    control.bootstrap(
        SCOPE, Revision.parse(manifest(effect="write")), read(Path("support/cases.json")), ACTORS
    )
    cases = {}
    for phase, initial_status in (
        ("before_admit", None),
        ("after_reserve", "reserved"),
        ("after_dispatch", "dispatching"),
        ("after_provider_ack", "dispatching"),
        ("before_client_ack", "succeeded"),
    ):
        # Hyphenated identifiers are safe public IDs. Each task can reuse its own logical operation.
        task = f"P19-{phase.replace('_', '-')}"
        control.start_task(OPERATOR, task, worker="crash-worker")
        operation = request(task, "ticket", task, attempt=task)
        stopped = _stop_at_point(oracle, store, phase, task)
        recovered = _host(Store(store.path, create=False), oracle.origin)
        ledger = recovered.ledger
        before = ledger.balance(task)
        receipts = ledger.receipts(task)
        initial_dispatches = len(oracle.dispatches(task))
        if initial_status is None:
            with rejects(LedgerError):
                ledger.attempt(task)
            require(
                not receipts and not initial_dispatches, "Crash before admission left an effect"
            )
            final = recovered.invoke(operation)
        else:
            attempt = ledger.attempt(task)
            require(attempt["status"] == initial_status, f"Wrong recovered state for {phase}")
            replay = recovered.invoke(operation)
            require(replay.disposition == "replay", "Worker restart dispatched a prior attempt")
            require(
                len(oracle.dispatches(task)) == initial_dispatches,
                "Physical effect was duplicated on restart",
            )
            require(ledger.balance(task) == before, "Replay changed the recovered budget")
            if initial_status == "reserved":
                require(
                    initial_dispatches == before["consumed_microusd"] == 0
                    and before["reserved_microusd"] == 50_000,
                    "Unclaimed reservation was lost or created an effect",
                )
                ledger.settle(task, Observation(f"host.{task}", "not_sent", "verified", 0))
                final = recovered.invoke(
                    request(task, "ticket", task, attempt=f"{task}-retry", retry_of=task)
                )
            elif initial_status == "dispatching":
                require(
                    initial_dispatches == 1
                    and before["consumed_microusd"] == 0
                    and before["reserved_microusd"] == 50_000
                    and not any(row["status"] == "succeeded" for row in receipts),
                    "Unacknowledged dispatch released exposure or fabricated success",
                )
                final = recovered.reconcile(OWNER, task)
            else:
                require(before["consumed_microusd"] == 13_000, "Settled charge was lost on crash")
                final = replay
        dispatches = oracle.dispatches(task)
        after = ledger.balance(task)
        require(
            final.attempt["status"] == "succeeded"
            and after["consumed_microusd"] == 13_000
            and after["reserved_microusd"] == after["pending_microusd"] == 0
            and len(dispatches) == 1
            and dispatches[0]["effect_created"],
            f"Recovered effect and ledger diverged for {phase}",
        )
        cases[phase] = {
            "stopped": stopped,
            "initial_attempt_state": initial_status,
            "initial_budget": before,
            "initial_receipts": [row["phase"] for row in receipts],
            "initial_dispatches": initial_dispatches,
            "verified_effect": dispatches[0]["effect_id"],
            "recovered_budget": after,
        }
    return {"lifecycle_boundaries": cases}


def _fault_evidence(store: Store, oracle: Oracle, promotion: int) -> dict:
    db = str(store.path)
    with closing(sqlite3.connect(db, isolation_level=None)) as outside:
        outside.execute("BEGIN IMMEDIATE")
        try:
            busy = collect(
                _process(sys.executable, "storage-fault", db, oracle.origin, "busy", "release")
            )
        finally:
            outside.rollback()
    require(busy["code"] == sqlite3.SQLITE_BUSY, "SQLite writer contention did not return BUSY")
    control = Control(store)
    require(control.get_alias(OWNER)["generation"] == promotion, "Busy client changed the alias")
    with rejects(ValueError):
        control.get_decision(OWNER, "P20-busy")

    control.start_task(OPERATOR, "P20-full-task", worker="disk-worker")
    with store.write() as connection:
        connection.execute("CREATE TABLE trial_disk_pressure(data BLOB)")
        connection.execute(
            """CREATE TRIGGER trial_full_alias BEFORE UPDATE ON aliases
               BEGIN INSERT INTO trial_disk_pressure VALUES (zeroblob(262144)); END"""
        )
        connection.execute(
            """CREATE TRIGGER trial_full_receipt BEFORE INSERT ON receipts
               BEGIN INSERT INTO trial_disk_pressure VALUES (zeroblob(262144)); END"""
        )
    try:
        full_release = collect(
            _process(sys.executable, "storage-fault", db, oracle.origin, "full", "release")
        )
        full_invoke = collect(
            _process(sys.executable, "storage-fault", db, oracle.origin, "full", "invoke")
        )
        require(
            full_release["code"] == full_invoke["code"] == sqlite3.SQLITE_FULL,
            "Page exhaustion did not return SQLITE_FULL at both durability boundaries",
        )
        require(control.get_alias(OWNER)["generation"] == promotion, "Full disk changed the alias")
        with rejects(ValueError):
            control.get_decision(OWNER, "P20-full")
        with rejects(LedgerError):
            Ledger(store, fingerprint_key=FINGERPRINT).attempt("P20-full-call")
        require(not oracle.dispatches("P20-full-call"), "Failed reservation still dispatched")
    finally:
        with store.write() as connection:
            connection.execute("DROP TRIGGER trial_full_alias")
            connection.execute("DROP TRIGGER trial_full_receipt")
            connection.execute("DROP TABLE trial_disk_pressure")
    return {
        "busy": busy,
        "full_release": full_release,
        "full_admission": full_invoke,
        "full_mechanism": "SQLite max_page_count and a transaction-local page allocation",
    }


def verify_p20(oracle: Oracle, previous_python: str) -> dict:
    from forge_check.governed.recovery import (
        backup,
        export_evidence,
        migrate,
        recovery_status,
        restore,
        resume,
    )

    path = Path("P20-ledger.sqlite3")
    old_snapshot = Path("P20-before-migration.sqlite3")
    oracle.arrange("P20-v1-pending", Behavior(mode="drop"))
    original = collect(_process(previous_python, "legacy", str(path), oracle.origin))
    with rejects(StoreError):
        Store(path, create=False)
    occupied = Path("P20-preserve-existing")
    occupied.write_text("preserve", encoding="utf-8")
    with rejects(FileExistsError):
        migrate(path, OWNER, occupied)
    after_failed_migration = collect(
        _process(previous_python, "legacy-inspect", str(path), oracle.origin)
    )
    require(
        occupied.read_text(encoding="utf-8") == "preserve"
        and after_failed_migration["alias"] == original["alias"],
        "A failed backup replaced a file or half-migrated the ledger",
    )
    source_manifest = migrate(path, OWNER, old_snapshot)
    store = Store(path, create=False)
    control = Control(store)
    host = _host(store, oracle.origin)
    require(source_manifest["schema_version"] == 1, "Migration backup identity is undocumented")
    require(control.get_alias(OWNER) == original["alias"], "Migration changed an existing alias")
    require(
        control.get_decision(OWNER, "P20-v1-promotion") == original["decision"]
        and all(
            host.ledger.balance(task) == balance for task, balance in original["balances"].items()
        ),
        "Migration lost release decisions or unsettled exposure",
    )
    require(
        collect(_process(previous_python, "legacy-inspect", str(path), oracle.origin))
        == {"unsupported_schema": True},
        "Previous client accepted a v2 store",
    )
    retained = collect(
        _process(previous_python, "legacy-inspect", str(old_snapshot), oracle.origin)
    )
    require(retained["alias"] == original["alias"], "Retained v1 artifact lost readable state")

    later = Revision.parse(manifest("lab-model-p20-later", effect="write"))
    control.register(OWNER, later)
    control.propose(OWNER, "P20-proposal", later.digest)
    stable = {**read(Path("support/baseline.json")), "revision": original["alias"]["revision"]}
    proposed = {**read(Path("support/candidate-pass.json")), "revision": later.digest}
    control.evaluate(
        OWNER, "P20-evidence", "P20-proposal", stable, proposed, observed_at=now(), generation=2
    )
    faults = _fault_evidence(store, oracle, 2)
    changed = control.promote(OWNER, "P20-v2-promotion", "P20-proposal", "P20-evidence", 2)
    require(changed["generation"] == 3 and changed["status"] == "applied", "Fence outlived fault")

    old = Path("P20-stale.sqlite3")
    stale_manifest = backup(store, OWNER, old)
    start_gap = len(oracle.dispatches())
    control.start_task(OPERATOR, "P20-new-exposure", worker="active")
    after_snapshot = host.invoke(request("P20-new-exposure", "P20-gap", "P20-gap"))
    gap = oracle.dispatches()[start_gap:]
    require(
        after_snapshot.attempt["status"] == "succeeded"
        and len(gap) == 1
        and gap[0]["effect_created"],
        "Provider did not observe an effect after the snapshot",
    )
    with rejects(StoreError):
        Store(old, create=False)
    stale_path = Path("P20-stale-restored.sqlite3")
    stale = restore(old, stale_path, stale_manifest)
    stale_store = Store(stale_path, create=False)
    stale_host = _host(stale_store, oracle.origin)
    stale_control = Control(stale_store)
    require(
        stale["fenced"] and stale["unresolved_attempts"] == 1, "Restore lost the original fence"
    )
    with rejects(StoreError):
        stale_control.start_task(OPERATOR, "P20-fenced-start", worker="restored")
    with rejects(StoreError):
        stale_host.invoke(request("P20-v1-known", "P20-fenced-call", "P20-fenced-call"))
    require(not oracle.dispatches("P20-fenced-call"), "A prior task bypassed the recovery fence")
    with rejects(StoreError):
        stale_control.rollback(OWNER, "P20-fenced-rollback", 3)
    with rejects(StoreError):
        resume(
            stale_store,
            OWNER,
            stale["recovery_id"],
            audit_ref="P20-gap-unresolved",
            covered_through_us=now(),
            source_quiesced=True,
            post_snapshot_effects=0,
        )
    # Reconciliation itself stays available behind the fence.
    for instance in (stale_host, host):
        recovered = instance.reconcile(OWNER, "P20-v1-pending")
        require(recovered.attempt["status"] == "succeeded", "Evidence lookup failed after restore")
    with rejects(StoreError):
        resume(
            stale_store,
            OWNER,
            stale["recovery_id"],
            audit_ref="P20-known-gap",
            covered_through_us=now(),
            source_quiesced=True,
            post_snapshot_effects=len(gap),
        )
    require(
        recovery_status(stale_store, OWNER)["fenced"], "Known external gap reopened stale ledger"
    )
    require(
        len(oracle.dispatches("P20-v1-pending")) == len(oracle.dispatches("P20-gap")) == 1,
        "Recovery duplicated a provider effect",
    )

    complete = Path("P20-authoritative.sqlite3")
    control.issue_alias_lease(OPERATOR, "P20-original-lease", ttl_seconds=30)
    current_manifest = backup(store, OWNER, complete)
    current = Path("P20-current-restored.sqlite3")
    with rejects(StoreError):
        restore(complete, current, {**current_manifest, "sha256": "0" * 64})
    require(not current.exists(), "Failed verification published an authoritative path")
    valid = restore(complete, current, current_manifest)
    restored = Store(current, create=False)
    restored_control = Control(restored)
    restored_host = _host(restored, oracle.origin)
    require(
        restored_control.get_alias(OWNER) == control.get_alias(OWNER)
        and restored_control.get_decision(OWNER, "P20-v2-promotion") == changed,
        "Current snapshot lost a release decision",
    )
    for key in ("P20-new-exposure", "P20-v1-known", "P20-v1-pending"):
        require(
            restored_host.ledger.balance(key) == host.ledger.balance(key),
            "Restored balance differs",
        )
    point = now()
    reopened = resume(
        restored,
        OWNER,
        valid["recovery_id"],
        audit_ref="P20-external-journal",
        covered_through_us=point,
        source_quiesced=True,
        post_snapshot_effects=0,
    )
    replay = resume(
        restored,
        OWNER,
        valid["recovery_id"],
        audit_ref="P20-external-journal",
        covered_through_us=point,
        source_quiesced=True,
        post_snapshot_effects=0,
    )
    require(not reopened["fenced"] and replay == reopened, "Recovery certificate is not idempotent")
    with rejects(PermissionError):
        restored_control.start_task(
            OPERATOR,
            "P20-stale-alias-lease",
            worker="replacement",
            alias_lease="P20-original-lease",
        )
    restored_control.start_task(OPERATOR, "P20-new-root", worker="replacement")
    require(
        restored_host.invoke(request("P20-new-root", "P20-resume", "P20-resume")).attempt["status"]
        == "succeeded",
        "Certified restore could not admit work",
    )
    evidence = export_evidence(restored, OWNER, limit=2)
    tail = export_evidence(restored, OWNER, after_seq=evidence["next_after_seq"])
    require(
        len(tail["receipts"]) > 0
        and all(
            "operation_key" not in row and "request_fingerprint" not in row
            for row in tail["receipts"]
        ),
        "Evidence pagination or data minimization failed",
    )
    with rejects(StoreError):
        Store("P20-absent.sqlite3", create=False)
    return {
        "faults": faults,
        "migrated_from": source_manifest,
        "retained_v1_reader": retained,
        "stale_snapshot": stale,
        "stale_external_effects": [row["effect_id"] for row in gap],
        "stale_remains_fenced": True,
        "authoritative_snapshot": current_manifest,
        "reopened": reopened,
        "first_export_page": evidence,
    }


def suite(previous: str, output: Path) -> int:
    checks = {}
    failures = {}
    with Oracle() as oracle:
        for name, trial in (
            ("P19", lambda: verify_p19(oracle)),
            ("P20", lambda: verify_p20(oracle, previous)),
        ):
            try:
                dump(output / f"{name}.json", trial())
                checks[name] = "pass"
            except Exception as error:
                checks[name] = "fail"
                failures[name] = str(error)
                (output / f"{name}.traceback").write_text(traceback.format_exc(), encoding="utf-8")
        dump(output / "destination-journal.json", oracle.journal)
    dump(output / "trials.json", {"tests": checks, "failures": failures})
    return int(bool(failures))


if __name__ == "__main__":
    if sys.argv[1] == "--worker":
        raise SystemExit(worker(sys.argv[2:]))
    raise SystemExit(suite(sys.argv[1], Path(sys.argv[2])))
