"""Pair the canary capacity projection with the reference wheel on a leased Vast host."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir, closing, contextmanager
from importlib.metadata import version
from itertools import product
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import TYPE_CHECKING, Any, Literal

from forge_check.governed import rollout
from forge_check.governed.control import Control
from forge_check.governed.db import Store
from forge_check.governed.ledger import Ledger, Observation, _balance

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# The isolated interpreter loads the wheel, with only the reusable fixture scripts added.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from capacity import CAP, CHECKOUT, OPEN_CHARGES, SEED, add_history, seed
from governed_oracle import Oracle
from governed_trials import (
    FINGERPRINT,
    MODEL,
    OPERATOR,
    OWNER,
    SCOPE,
    World,
    manifest,
    now,
    request,
    require,
)

CURRENT = "current"
PRIMARY = "history-10000-current"
HISTORIES = (0, 1000, 10_000)
OPEN_ROOTS = (1, 4, 32)
ATTEMPTS_PER_ROOT = (1, 8, 64)
WARMUPS = 3
HOLDS_MS = 15
ARMS = ("reference", "projection")
Arm = Literal["reference", "projection"]
Totals = tuple[int, int, int]


def save(path: Path, value: object) -> None:
    """Replace a receipt atomically after serializing it in the same directory."""
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        staging = Path(stream.name)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    staging.replace(path)


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def clone(source: Path, target: Path) -> None:
    require(not target.exists(), f"Clone target already exists: {target}")
    with (
        closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as reader,
        closing(sqlite3.connect(target)) as writer,
    ):
        reader.backup(writer)
        require(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal", "Clone needs WAL")


def integrity(conn: sqlite3.Connection) -> None:
    require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Invalid database")
    require(conn.execute("PRAGMA foreign_key_check").fetchone() is None, "Invalid foreign keys")


def expected_totals(charges: tuple[int, ...], closed: int) -> Totals:
    return len(charges), closed + sum(charges), closed + sum(max(CAP, charge) for charge in charges)


def independent_totals(conn: sqlite3.Connection) -> Totals:
    """Reduce atomic attempt rows separately from the reference and projection SQL."""
    allocations = dict(
        conn.execute(
            "SELECT root_id, allocated_microusd FROM rollout_allocations "
            "WHERE rollout_id = ? AND cohort = 'canary'",
            (CURRENT,),
        )
    )
    finished = {root for (root,) in conn.execute("SELECT root_id FROM finished_roots")}
    counts = dict.fromkeys(allocations, 0)
    for root, amount in conn.execute("SELECT root_id, charge_microusd FROM attempts"):
        if root in counts and amount is not None:
            counts[root] += amount
    open_roots = set(allocations) - finished
    return (
        len(open_roots),
        sum(counts.values()),
        sum(
            max(allocations[root], amount) if root in open_roots else amount
            for root, amount in counts.items()
        ),
    )


def add_fanout(conn: sqlite3.Connection, roots: int, fanout: int) -> tuple[int, ...]:
    """Vary open shape on the reference schema before any projection exists."""
    conn.execute(
        "UPDATE rollout_allocations SET rollout_id = 'previous' WHERE root_id GLOB 'open-*'"
    )
    conn.execute(
        "INSERT INTO finished_roots SELECT id, ?, ? FROM tasks WHERE id GLOB 'open-*'",
        (OPERATOR, now()),
    )
    # Leave one free slot for admissions sampled on this fixture.
    conn.execute("UPDATE rollouts SET max_concurrent = ? WHERE id = ?", (roots + 1, CURRENT))
    conn.execute(
        "CREATE TEMP TABLE fanout("
        "root TEXT, bucket INTEGER, number INTEGER, attempt TEXT, digest TEXT, charge INTEGER)"
    )
    rows = []
    charges = []
    for index in range(roots):
        root = f"fan-{index}"
        assigned = rollout.bucket(SCOPE, SEED, root)
        total = 0
        for number in range(fanout):
            attempt = f"{root}-{number}"
            charge = 17 + (index + number) % 7
            digest = hashlib.sha256(attempt.encode()).hexdigest()
            rows.append((root, assigned, number, attempt, digest, charge))
            total += charge
        charges.append(total)
    conn.executemany("INSERT INTO fanout VALUES (?, ?, ?, ?, ?, ?)", rows)
    for table, key, overrides in (
        ("tasks", "id", {"id": "f.root", "root_id": "f.root"}),
        (
            "rollout_allocations",
            "root_id",
            {"root_id": "f.root", "bucket": "f.bucket", "rollout_id": "'current'"},
        ),
        (
            "attempts",
            "id",
            {
                "id": "f.attempt",
                "task_id": "f.root",
                "root_id": "f.root",
                "operation_id": "f.attempt",
                "request_fingerprint": "f.digest",
                "operation_key": "f.digest",
                "provider_ref": "f.attempt",
                "charge_microusd": "f.charge",
                "cache_key": "NULL",
            },
        ),
    ):
        template = conn.execute(f"SELECT * FROM {table} WHERE {key} = 'template'").fetchone()
        require(template is not None, f"Missing fan-out template in {table}")
        columns = template.keys()
        expressions = ", ".join(overrides.get(column, f"t.{column}") for column in columns)
        where = "" if table == "attempts" else " AND f.number = 0"
        result = conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"SELECT {expressions} FROM {table} t CROSS JOIN fanout f "
            f"WHERE t.{key} = 'template'{where}"
        )
        require(result.rowcount == (len(rows) if table == "attempts" else roots), "Wrong fan-out")
    return tuple(charges)


def prepare(directory: Path) -> list[dict[str, Any]]:
    """Build one independent fixture reconstruction in the installed reference wheel."""
    with chdir(directory):
        shutil.copytree(CHECKOUT / "examples" / "check-support", "support")
        cells = []
        for count in HISTORIES:
            with Oracle() as destination:
                world = World(
                    f"source-{count}", destination, manifest(budget=CAP, model_reserve=40_000)
                )
                seed(world, destination)
                require(len(destination.dispatches()) == 4, "Wrong number of SDK template posts")
            history_charge = add_history(world, count)
            for placement in ("previous", CURRENT):
                name = f"history-{count}-{placement}"
                path = directory / f"{name}.db"
                clone(world.db, path)
                store = Store(path, create=False)
                if placement == CURRENT:
                    with store.write() as conn:
                        result = conn.execute(
                            "UPDATE rollout_allocations SET rollout_id = ? "
                            "WHERE rollout_id = 'previous' AND root_id GLOB 'hist-*'",
                            (CURRENT,),
                        )
                        require(result.rowcount == count, "Wrong history membership")
                closed = history_charge if placement == CURRENT else 0
                charges = tuple(map(sum, OPEN_CHARGES))
                cells.append(fixture(store, name, count, placement, charges, None, closed))
        for roots, fanout in product(OPEN_ROOTS, ATTEMPTS_PER_ROOT):
            name = f"open-{roots}-fanout-{fanout}"
            path = directory / f"{name}.db"
            clone(directory / f"{PRIMARY}.db", path)
            store = Store(path, create=False)
            with store.write() as conn:
                charges = add_fanout(conn, roots, fanout)
            cells.append(
                fixture(store, name, HISTORIES[-1], CURRENT, charges, fanout, history_charge)
            )
    return cells


def fixture(
    store: Store,
    name: str,
    history: int,
    placement: str,
    open_charges: tuple[int, ...],
    fanout: int | None,
    closed_charge: int,
) -> dict[str, Any]:
    expected = expected_totals(open_charges, closed_charge)
    with store.read() as conn:
        integrity(conn)
        require(independent_totals(conn) == expected, "Independent fixture oracle mismatch")
        require(rollout.capacity(conn, CURRENT) == expected, "Installed control totals differ")
        rows = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "rollout_allocations", "finished_roots", "attempts", "receipts")
        }
    return {
        "cell": name,
        "database": str(store.path),
        "database_sha256": sha(store.path),
        "bytes_v2": store.path.stat().st_size,
        "history": history,
        "placement": placement,
        "closed_charge_microusd": closed_charge,
        "open_charges_microusd": open_charges,
        "attempts_per_open_root": fanout,
        "totals": expected,
        "row_counts": rows,
    }


def describe(samples: list[int]) -> dict[str, float | int | list[int]]:
    ordered = sorted(samples)
    return {
        "count": len(samples),
        "median_ns": statistics.median(samples),
        "p95_ns": ordered[math.ceil(0.95 * len(samples)) - 1],
        "p99_ns": ordered[math.ceil(0.99 * len(samples)) - 1],
        "max_ns": ordered[-1],
        "samples_ns": samples,
    }


def availability_sql(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Capture the statements of the selected calculator, including any fallback."""
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        rollout.capacity(conn, CURRENT)
    finally:
        conn.set_trace_callback(None)
    return [
        {
            "sql": sql,
            "plan": [tuple(row) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}")],
        }
        for raw_sql in statements
        if (sql := raw_sql.strip()).upper().startswith(("SELECT", "WITH"))
    ]


def activate(store: Store, arm: Arm) -> None:
    with store.write() as conn:
        rollout.set_capacity_mode(conn, arm)


def create_candidate(source: Path, target: Path) -> tuple[Store, dict[str, Any]]:
    from forge_check.governed import recovery

    clone(source, target)
    backup = target.with_suffix(".backup")
    started = time.perf_counter_ns()
    manifest = recovery.migrate(target, OWNER, backup)
    elapsed = time.perf_counter_ns() - started
    store = Store(target, create=False)
    with store.read() as conn:
        integrity(conn)
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
    receipt = {
        "elapsed_ns_including_backup": elapsed,
        "backup": manifest,
        "schema": schema,
        "database_bytes": target.stat().st_size,
    }
    backup.unlink()
    return store, receipt


def verify(conn: sqlite3.Connection, expected: Totals) -> None:
    require(
        rollout.reference_capacity(conn, CURRENT)
        == rollout.projected_capacity(conn, CURRENT)
        == independent_totals(conn)
        == expected,
        "Exact capacity parity failed",
    )


def query_pair(
    reference: Store,
    projection: Store,
    expected: Totals,
    repetitions: int,
    reverse_first: bool,
    fanout: int | None,
) -> dict[str, Any]:
    root = "open-0" if fanout is None else "fan-0"
    orders = []
    capacity_samples: dict[str, list[int]] = {arm: [] for arm in ARMS}
    balance_samples: dict[str, list[int]] = {arm: [] for arm in ARMS}
    with reference.read() as left, projection.read() as right:
        by_name = {"reference": left, "projection": right}
        for conn in by_name.values():
            verify(conn, expected)
        plans = {arm: availability_sql(conn) for arm, conn in by_name.items()}
        for arm, statements in plans.items():
            projected = any("FROM rollout_capacity_open " in item["sql"] for item in statements)
            canonical = any(
                "FROM rollout_allocations r LEFT JOIN finished_roots" in item["sql"]
                for item in statements
            )
            require(
                (projected, canonical) == (arm == "projection", arm == "reference"),
                f"{arm} did not run the selected calculator exclusively",
            )
        balances = {arm: _balance(conn, root, CAP) for arm, conn in by_name.items()}
        require(balances["reference"] == balances["projection"], "Binding balance differs")
        for index in range(WARMUPS + repetitions):
            order = ARMS[::-1] if (index + reverse_first) % 2 else ARMS
            durations = {}
            for arm in order:
                conn = by_name[arm]
                started = time.perf_counter_ns()
                actual = rollout.capacity(conn, CURRENT)
                duration = time.perf_counter_ns() - started
                require(actual == expected, f"{arm}: {actual} != {expected}")
                started = time.perf_counter_ns()
                balance = _balance(conn, root, CAP)
                balance_duration = time.perf_counter_ns() - started
                require(balance == balances[arm], "Binding balance drifted")
                if index >= WARMUPS:
                    capacity_samples[arm].append(duration)
                    balance_samples[arm].append(balance_duration)
                    durations[arm] = duration
            if index >= WARMUPS:
                orders.append(
                    {"order": order, "gain_ns": durations["reference"] - durations["projection"]}
                )
        for conn in by_name.values():
            verify(conn, expected)
    return {
        "query_plans": plans,
        "capacity": {arm: describe(samples) for arm, samples in capacity_samples.items()},
        "binding_balance": {arm: describe(samples) for arm, samples in balance_samples.items()},
        "paired_samples": orders,
    }


class MeasuredStore(Store):
    """Expose transaction timing with the original writer and commit path."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, create=False)
        self.writer: dict[str, int] = {}

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        started = time.perf_counter_ns()
        with super().write() as conn:
            acquired = time.perf_counter_ns()
            yield conn
            body_end = time.perf_counter_ns()
        self.writer = {
            "writer_open_and_wait_ns": acquired - started,
            "writer_body_ns": body_end - acquired,
            "writer_commit_and_close_ns": time.perf_counter_ns() - body_end,
        }


class MeasuredControl(Control):
    def __init__(self, store: MeasuredStore) -> None:
        super().__init__(store)
        self.view_ns = 0

    def _rollout_view(self, conn: sqlite3.Connection, rollout_id: str, now: int) -> dict[str, Any]:
        started = time.perf_counter_ns()
        view = super()._rollout_view(conn, rollout_id, now)
        self.view_ns = time.perf_counter_ns() - started
        return view


class Actions:
    def __init__(self, path: Path) -> None:
        self.store = MeasuredStore(path)
        self.control = MeasuredControl(self.store)
        self.ledger = Ledger(self.store, fingerprint_key=FINGERPRINT)
        self.samples: dict[str, dict[str, int]] = {}

    def time(self, name: str, operation: Callable[[], Any]) -> Any:
        self.store.writer = {}
        self.control.view_ns = 0
        started = time.perf_counter_ns()
        result = operation()
        self.samples[name] = {
            "wall_ns": time.perf_counter_ns() - started,
            "rollout_view_ns": self.control.view_ns,
            **self.store.writer,
        }
        return result

    def first_cut(self) -> dict[str, Any]:
        ready = threading.Event()
        measure = threading.Event()

        def contend() -> None:
            with Store(self.store.path, create=False).write() as conn:
                conn.execute("UPDATE recovery_state SET open = open WHERE singleton = 1")
                ready.set()
                require(measure.wait(timeout=5), "Cut did not start")
                time.sleep(HOLDS_MS / 1_000)

        with ThreadPoolExecutor(1) as pool:
            competing = pool.submit(contend)
            require(ready.wait(timeout=5), "Competing writer did not start")
            measure.set()
            cut = self.time("first_cut", lambda: self.control.stop_rollout(OWNER, CURRENT, 1))
            competing.result()
        return cut


def operate(path: Path, operator_index: int, arm: Arm) -> dict[str, Any]:
    actions = Actions(path)
    control = actions.control
    phase = {}

    def reconcile(action: str) -> dict[str, Any]:
        # An independent deterministic observation supplies the same charge to both ledgers.
        return actions.time(
            action,
            lambda: actions.ledger.settle(
                "meter-attempt",
                Observation("meter-observation", "succeeded", "verified", 70, "meter-provider"),
            ),
        )

    status = actions.time("status", lambda: control.get_rollout(OWNER, CURRENT))
    require(status["state"] == "open", "Operator fixture must be active")
    allocated = actions.time(
        "admission", lambda: control.start_task(OPERATOR, "meter-task", worker="meter")
    )
    require(allocated["allocation"]["cohort"] == "canary", "Sample did not exercise canary")
    phase["allocation"] = allocated["allocation"]
    phase["revision"] = allocated["revision"]
    # The over-cap ticket in the query fixture suspends its binding. The model binding
    # remains authorized and exercises the same reservation and charge ledger lifecycle.
    no_wire_request = request("meter-task", "meter-attempt", "meter-ticket", binding=MODEL)
    reservation = actions.ledger.admit(no_wire_request)
    require(reservation.disposition == "dispatch", f"Invalid reservation: {reservation.reason}")
    require(actions.ledger.mark_dispatched("meter-attempt"), "Missing dispatch intent")
    observed = reconcile("recognize_charge")
    require(observed["charge_microusd"] == 70, "Unexpected charge")
    phase["recognized_charge_microusd"] = observed["charge_microusd"]
    require(reconcile("replay_charge")["charge_microusd"] == 70, "Replay altered charge")
    closed = actions.time("finish", lambda: control.finish_task(OPERATOR, "meter-task"))
    require(closed["root_id"] == "meter-task", "Unexpected root closed")
    cut = actions.first_cut()
    stopped_at = time.perf_counter_ns()
    require(cut["state"] == "stopped", "First cut did not stop rollout")
    admitted_after_cut = control.start_task(OPERATOR, "after-cut", worker="meter")
    require("allocation" not in admitted_after_cut, "New allocation survived first cut")
    latency_to_verify = time.perf_counter_ns() - stopped_at
    replay = actions.time("cut_replay", lambda: control.stop_rollout(OWNER, CURRENT, 1))
    require(cut == replay, "Cut replay changed rollout")
    pinned = control.start_task(OPERATOR, "open-0", worker="worker-A")
    require(pinned["allocation"]["cohort"] == "canary", "An open root lost its pin after stop")
    with actions.store.read() as conn:
        require(
            rollout.dispatch_block(
                conn, conn.execute("SELECT * FROM tasks WHERE id = 'open-0'").fetchone(), now()
            )
            is None,
            "First cut blocked a previously pinned dispatch",
        )
        totals = independent_totals(conn)
        require(rollout.capacity(conn, CURRENT) == totals, "Post-write parity failed")
        if arm == "projection":
            verify(conn, totals)
    phase.update(
        capacity=list(totals),
        settlement_binding=MODEL,
        traffic_by_cohort=cut["traffic_by_cohort"],
        allocations=cut["allocations"],
        retained_revision=pinned["revision"],
    )
    return {
        "sample": operator_index,
        "timings": actions.samples,
        "outcome": phase,
        "first_post_cut_admission_verified_ns": latency_to_verify,
        "first_cut_start_to_admission_verified_ns": (
            actions.samples["first_cut"]["wall_ns"] + latency_to_verify
        ),
        "database_bytes": path.stat().st_size,
    }


def run_actions(source: Path, count: int, arm: Arm) -> dict[str, Any]:
    with TemporaryDirectory(dir=source.parent) as path:
        scratch = Path(path).resolve()
        samples = []
        for index in range(count):
            target = scratch / f"ops-{index}.db"
            clone(source, target)
            samples.append(operate(target, index, arm))
    return {"package_version": version("forge-os-check"), "samples": samples}


def environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "package_version": version("forge-os-check"),
        "platform": platform.platform(),
        "executable": sys.executable,
    }


def child(python: Path, job: str, directory: Path, *options: str) -> Any:
    result = directory / f"{job}.json"
    require(not result.exists(), "Worker receipt already exists")
    subprocess.run(
        [str(python), "-I", __file__, f"--{job}", str(result), *options],
        check=True,
        timeout=300,
    )
    return json.loads(result.read_text(encoding="utf-8"))


def summarize_operator(runs: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {}
    for arm in ARMS:
        samples = [sample for run in runs for sample in run["operations"][arm]["samples"]]
        summary[arm] = {
            name: describe([sample["timings"][name]["wall_ns"] for sample in samples])
            for name in samples[0]["timings"]
        }
    projection = summary["projection"]
    return {
        "writer_hold_ms": HOLDS_MS,
        "duration_gates_passed": (
            projection["status"]["p95_ns"] <= 100_000_000
            and projection["first_cut"]["p95_ns"] <= 500_000_000
        ),
        "arms": summary,
    }


def reconstruct(
    directory: Path,
    reference_python: Path,
    repetitions: int,
    operator_samples: int,
    index: int,
    receipt: dict[str, Any],
    output: Path,
) -> None:
    fixtures = child(reference_python, "prepare", directory)
    require(fixtures["environment"]["package_version"] == "0.4.1.dev1", "Unexpected control wheel")
    record: dict[str, Any] = {
        "index": index,
        "reference_environment": fixtures["environment"],
        "cells": [],
    }
    receipt["reconstructions"].append(record)
    observed_populations = {}
    # Time administrative and write operations first while the synthetic certificate is fresh.
    for fixture in sorted(fixtures["fixtures"], key=lambda item: item["cell"] != PRIMARY):
        name = fixture["cell"]
        original = Path(fixture["database"])
        require(
            original.is_file() and sha(original) == fixture["database_sha256"], "Changed fixture"
        )
        expected = tuple(fixture["totals"])
        candidate, migration = create_candidate(original, directory / f"{name}-candidate.db")
        activate(candidate, "projection")
        ref_mode_path = directory / f"{name}-reference-mode.db"
        clone(candidate.path, ref_mode_path)
        reference = Store(ref_mode_path, create=False)
        activate(reference, "reference")
        if name == PRIMARY:
            pair = {}
            order = ARMS[::-1] if index % 2 else ARMS
            for arm in order:
                if arm == "reference":
                    pair[arm] = child(
                        reference_python,
                        "reference-actions",
                        directory,
                        "--source",
                        str(original),
                        "--operator-samples",
                        str(operator_samples),
                    )
                else:
                    pair[arm] = run_actions(candidate.path, operator_samples, "projection")
            for left, right in zip(
                pair["reference"]["samples"], pair["projection"]["samples"], strict=True
            ):
                expected_outcome, actual_outcome = left["outcome"], right["outcome"]
                require(
                    expected_outcome == actual_outcome,
                    "Administrative or write outcomes differ: "
                    f"{expected_outcome=} {actual_outcome=}",
                )
            record["operation_order"] = order
            record["operations"] = pair
        query = query_pair(
            reference,
            candidate,
            expected,
            repetitions,
            bool(index % 2),
            fixture["attempts_per_open_root"],
        )
        fixture.pop("database")
        cell = {**fixture, "query": query, "migration": migration}
        if fixture["attempts_per_open_root"] is None:
            old = observed_populations.setdefault(fixture["history"], fixture["row_counts"])
            require(old == fixture["row_counts"], "Placement changed global row counts")
        if name == PRIMARY:
            base, projected = (query["capacity"][arm] for arm in ARMS)
            gain = base["median_ns"] / projected["median_ns"]
            cell.update(
                median_speedup=gain,
                duration_gate_passed=gain >= 5 and projected["p95_ns"] <= 5_000_000,
            )
        record["cells"].append(cell)
        save(output, receipt)
        print(f"capacity-paired reconstruction={index + 1} cell={name}", flush=True)
    require("operations" in record, "Missing primary operator fixture")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-python", type=Path)
    parser.add_argument("--output", type=Path, help="New receipt file in an existing directory.")
    parser.add_argument(
        "--samples", type=int, default=31, help="Capacity samples per arm and cell."
    )
    parser.add_argument(
        "--reconstructions", type=int, default=3, help="Independent fixture builds."
    )
    parser.add_argument(
        "--operator-samples",
        type=int,
        default=20,
        help="Total independent copies per arm across all rebuilds.",
    )
    worker = parser.add_mutually_exclusive_group()
    worker.add_argument("--prepare", type=Path, help=argparse.SUPPRESS)
    worker.add_argument("--reference-actions", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    lab_id = os.environ.get("FORGE_VAST_LAB_ID", "")
    if platform.system() != "Linux" or not lab_id.isascii() or not lab_id.isdecimal():
        parser.error("Run this benchmark on the leased Linux Vast instance")
    if args.prepare is not None:
        save(args.prepare, {"environment": environment(), "fixtures": prepare(args.prepare.parent)})
        return
    if args.reference_actions is not None:
        save(args.reference_actions, run_actions(args.source, args.operator_samples, "reference"))
        return
    if args.reference_python is None or args.output is None:
        parser.error("--reference-python and --output are required")
    if args.reconstructions < 1 or args.samples < 1 or args.operator_samples < args.reconstructions:
        parser.error("Require at least one sample and one operator copy per reconstruction")
    output = args.output.resolve()
    if output.exists() or not output.parent.is_dir():
        parser.error("--output must be a new file in an existing directory")
    # Preserve the venv entry point instead of resolving its Python symlink to a base interpreter.
    reference_python = args.reference_python.absolute()
    if not reference_python.is_file() or reference_python == Path(sys.executable):
        parser.error("--reference-python must name the isolated reference interpreter")
    protocol_run = (args.reconstructions, args.samples, args.operator_samples) == (3, 31, 20)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "lease_id": lab_id,
        "benchmark": "paired-rollout-capacity",
        "candidate_environment": environment(),
        "runner_sha256": sha(Path(__file__)),
        "fixture_runner_sha256": sha(Path(__file__).with_name("capacity.py")),
        "protocol_run": protocol_run,
        "warmups": WARMUPS,
        "capacity_samples_per_arm_and_reconstruction": args.samples,
        "operator_samples_per_arm": args.operator_samples,
        "reconstructions": [],
        "query_control": "Candidate wheel using the canonical calculator on the migrated schema.",
        "write_control": "Reference wheel on an unmodified v2 copy of the matching fixture.",
    }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream)
    try:
        with TemporaryDirectory(dir=output.parent) as scratch:
            for index in range(args.reconstructions):
                directory = Path(scratch) / f"reconstruction-{index + 1}"
                directory.mkdir()
                each, extra = divmod(args.operator_samples, args.reconstructions)
                reconstruct(
                    directory,
                    reference_python,
                    args.samples,
                    each + int(index < extra),
                    index,
                    receipt,
                    output,
                )
        receipt["operator"] = summarize_operator(receipt["reconstructions"])
        receipt["primary_query_duration_gates_passed"] = all(
            cell["duration_gate_passed"]
            for run in receipt["reconstructions"]
            for cell in run["cells"]
            if cell["cell"] == PRIMARY
        )
        receipt["status"] = "complete"
    except BaseException as error:
        receipt["status"] = "failed" if isinstance(error, AssertionError) else "inconclusive"
        receipt["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        save(output, receipt)


if __name__ == "__main__":
    main()
