"""Measure the installed canary capacity calculator at fixed global ledger cardinality."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import shutil
import sqlite3
import statistics
import sys
from contextlib import ExitStack, chdir, closing
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns

from forge_check.governed.revision import Revision
from forge_check.governed.rollout import bucket, capacity

CHECKOUT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT / "tests" / "acceptance"))
from governed_oracle import Behavior, Oracle  # noqa: E402
from governed_trials import (  # noqa: E402
    OPERATOR,
    OWNER,
    SCOPE,
    World,
    evaluate,
    manifest,
    now,
    request,
    require,
)

CAP = 100_000
SEED = "capacity-benchmark"
OPEN_CHARGES = ((), (8000, 7000), (101_000,))


def seed(world: World, destination: Oracle) -> None:
    revision = Revision.parse(manifest(budget=CAP, model_reserve=40_000, ticket_cache=30))
    world.control.register(OWNER, revision)
    world.control.propose(OWNER, "proposal", revision.digest, source="human")
    evidence = evaluate(world, "approval", "proposal", revision.digest)
    require(evidence["report"]["verdict"] == "pass", "Expected a passing proposal")

    def start(name: str) -> None:
        world.control.start_rollout(
            OWNER,
            name,
            "proposal",
            "approval",
            1,
            mode="canary",
            seed=SEED,
            basis_points=10_000,
            max_concurrent=4,
            budget_microusd=100_000_000,
            ends_at=min(evidence["expires_at"], now() + 120_000_000),
        )

    def invoke(root: str, attempt: str, amount: int) -> None:
        destination.arrange(attempt, Behavior(charge=amount))
        call = world.host.invoke(request(root, attempt, attempt))
        require(call.attempt["charge_microusd"] == amount, "Ledger charge differs from fixture")
        wire = destination.dispatches(attempt)
        require(len(wire) == 1 and wire[0]["charge_microusd"] == amount, "Wrong origin charge")

    start("previous")
    world.task("template")
    invoke("template", "template", 17)
    world.control.finish_task(OPERATOR, "template")
    world.control.stop_rollout(OWNER, "previous", 1)
    start("current")
    for index, charges in enumerate(OPEN_CHARGES):
        root = f"open-{index}"
        allocation = world.task(root)["allocation"]
        require(
            allocation["cohort"] == "canary" and allocation["allocated_microusd"] == CAP,
            "Open root did not receive its canary budget",
        )
        for number, charge in enumerate(charges):
            invoke(root, f"open-{index}-{number}", charge)


def add_history(world: World, count: int) -> int:
    """Copy column shapes from one closed SDK root; synthetic history has no physical sends."""
    columns_by_table = {
        "tasks": {"id": "h.root", "root_id": "h.root"},
        "rollout_allocations": {"root_id": "h.root", "bucket": "h.bucket"},
        "finished_roots": {"root_id": "h.root"},
        "attempts": {
            "id": "h.root",
            "task_id": "h.root",
            "root_id": "h.root",
            "operation_id": "h.root",
            "request_fingerprint": "h.digest",
            "operation_key": "h.digest",
            "provider_ref": "h.root",
            "charge_microusd": "h.charge",
            "cache_key": "NULL",
        },
    }
    rows = []
    for i in range(count):
        root = f"hist-{i}"
        rows.append(
            (root, bucket(SCOPE, SEED, root), hashlib.sha256(root.encode()).hexdigest(), 17 + i % 7)
        )
    with world.store.write() as conn:
        conn.execute(
            "CREATE TEMP TABLE input(root TEXT, bucket INTEGER, digest TEXT, charge INTEGER)"
        )
        conn.executemany("INSERT INTO input VALUES (?, ?, ?, ?)", rows)
        for table, overrides in columns_by_table.items():
            key = "id" if table in {"tasks", "attempts"} else "root_id"
            template = conn.execute(f"SELECT * FROM {table} WHERE {key} = 'template'").fetchone()
            require(template is not None, f"Missing template in {table}")
            columns = template.keys()
            projected = ", ".join(overrides.get(column, f"t.{column}") for column in columns)
            cursor = conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"SELECT {projected} FROM {table} t CROSS JOIN input h WHERE t.{key} = 'template'"
            )
            require(cursor.rowcount == count, f"Incorrect history population in {table}")
    return sum(row[3] for row in rows)


def experiment(history: int, repetitions: int) -> dict:
    shutil.copytree(CHECKOUT / "examples" / "check-support", "support")
    with Oracle() as destination:
        world = World("source", destination, manifest(budget=CAP, model_reserve=40_000))
        seed(world, destination)
        physical = len(destination.dispatches())
    require(physical == 4, "Reference setup requires four physical calls")
    history_charge = add_history(world, history)
    groups = ("closed_previous", "closed_current")
    sampled = {group: [] for group in groups}
    states = {}
    expected = {}
    populations = {}
    with ExitStack() as stack:
        for group in groups:
            conn = stack.enter_context(closing(sqlite3.connect(f"{group}.db")))
            with world.store.read() as source:
                source.backup(conn)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if group == "closed_current":
                with conn:
                    changed = conn.execute(
                        "UPDATE rollout_allocations SET rollout_id = 'current' "
                        "WHERE rollout_id = 'previous' AND root_id GLOB 'hist-*'"
                    )
                    require(changed.rowcount == history, "Incorrect historical rollout assignment")
            require(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "Invalid database")
            require(conn.execute("PRAGMA foreign_key_check").fetchone() is None, "Invalid FK")
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            states[group] = conn
            populations[group] = {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("tasks", "rollout_allocations", "finished_roots", "attempts", "receipts")
            }
            closed = history_charge if group == "closed_current" else 0
            charges = [sum(chunks) for chunks in OPEN_CHARGES]
            expected[group] = (
                len(charges),
                closed + sum(charges),
                closed + sum(max(CAP, value) for value in charges),
            )
        require(populations[groups[0]] == populations[groups[1]], "Global row counts differ")
        for i in range(repetitions + 3):
            for group in groups if i % 2 == 0 else reversed(groups):
                start = perf_counter_ns()
                actual = capacity(states[group], "current")
                duration = perf_counter_ns() - start
                require(actual == expected[group], f"{group}: {actual} != {expected[group]}")
                if i >= 3:
                    sampled[group].append(duration)
    cells = []
    for group in groups:
        samples = sorted(sampled[group])
        active, consumed, exposure = expected[group]
        cells.append(
            {
                "placement": group,
                "current_synthetic_history_roots": history if group == groups[1] else 0,
                "active_roots": active,
                "consumed_microusd": consumed,
                "exposure_microusd": exposure,
                "capacity_median_us": round(statistics.median(samples) / 1000, 3),
                "capacity_p95_us": round(samples[math.ceil(0.95 * len(samples)) - 1] / 1000, 3),
                "sample_ns": sampled[group],
            }
        )
    return {
        "schema_version": 1,
        "benchmark": "rollout-capacity",
        "status": "pass",
        "package_version": version("forge-os-check"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "fixture": "Two copies of one SDK-created ledger. One real closed template remains in "
        "the previous rollout. Synthetic closed canary roots have one charged attempt "
        "each and no per-root receipts. Only their rollout membership changes.",
        "metric": "Read-only capacity() on a warm, open connection, measured with perf_counter_ns. "
        "Three untimed warm-up samples per placement; subsequent calls alternate AB/BA.",
        "closed_synthetic_roots": history,
        "sdk_setup_posts": physical,
        "open_root_cap_microusd": CAP,
        "open_root_charges_microusd": OPEN_CHARGES,
        "closed_synthetic_charge_microusd": history_charge,
        "global_rows": populations[groups[0]],
        "samples_per_placement": repetitions,
        "cells": cells,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=int, default=10_000, help="Synthetic closed canary roots")
    parser.add_argument("--repetitions", type=int, default=31, help="Measured calls per placement")
    parser.add_argument("--output", type=Path, required=True, help="New result JSON file")
    args = parser.parse_args()
    if not 1 <= args.history <= 100_000 or not 1 <= args.repetitions <= 1000:
        parser.error("--history must be in [1, 100000] and --repetitions in [1, 1000]")
    output = args.output.resolve()
    if output.exists() or not output.parent.is_dir():
        parser.error("--output must be a new file in an existing directory")
    with TemporaryDirectory() as temp, chdir(temp):
        report = experiment(args.history, args.repetitions)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(f"Verified both capacity totals. Results written to {output}")


if __name__ == "__main__":
    main()
