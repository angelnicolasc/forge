"""Measure repeated inventory reads and exact-attempt replay through an installed Forge wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import sys
from collections import Counter
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns

from forge_check.governed.ledger import Request

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "acceptance" / "preview_bindings.py"
sys.path.insert(0, str(FIXTURE.parent))
from preview_bindings import READER, STOCK, Destination, require, start_world  # noqa: E402

TTL = 300


class Inventory(Destination):
    """Record the synthetic charge at the HTTP destination alongside each physical request."""

    def respond(self, envelope: dict) -> tuple[dict, bool]:
        response, stalled = super().respond(envelope)
        self.journal[-1]["charge_microusd"] = response["charge_microusd"]
        return response, stalled


def percentile_ms(samples: list[int], quantile: float) -> float:
    ordered = sorted(samples)
    return round(ordered[math.ceil(len(ordered) * quantile) - 1] / 1_000_000, 3)


def run_cell(tasks: int, distinct_keys: int, ttl: int) -> dict:
    consumed = 0
    replay_count = 0
    times = []
    sent = {}
    answers = hashlib.sha256()
    with TemporaryDirectory() as tmp, Inventory(Path(tmp) / "destination.db") as destination:
        for index in range(distinct_keys):
            destination.adjust(f"sku-{index}", index + 10)
        control, host, revision = start_world(Path(tmp) / "forge.db", destination, ttl=ttl)
        start = perf_counter_ns()
        for index in range(tasks):
            task = f"read-{index}"
            key = index % distinct_keys
            sku = f"sku-{key}"
            control.start_task(READER, task, worker="benchmark")
            request = Request(task, READER, "stock", task, STOCK, {"sku": sku})
            before = perf_counter_ns()
            call = host.invoke(request)
            times.append(perf_counter_ns() - before)
            require(call.result == {"sku": sku, "on_hand": key + 10}, f"Wrong answer for {task}")
            answers.update(f"{sku}:{call.result['on_hand']}\n".encode())
            require(call.disposition in {"cached", "dispatch"}, f"Unexpected {call.disposition}")
            if call.disposition == "dispatch":
                sent[task] = sku
            posts_before = len(destination.journal)
            replay = host.invoke(request)
            require(replay.disposition == "replay", f"Attempt replay not recognized for {task}")
            require(len(destination.journal) == posts_before, f"Replay reached origin for {task}")
            replay_count += 1
            control.finish_task(READER, task)
            balance = host.ledger.balance(task)
            require(balance["reserved_microusd"] == balance["pending_microusd"] == 0, task)
            consumed += balance["consumed_microusd"]
        elapsed = (perf_counter_ns() - start) / 1_000_000_000
        if ttl:
            require(elapsed < ttl, "Trial exceeded its warm TTL window; reduce --tasks and rerun")
        journal = destination.journal
        observed = Counter((row["attempt_id"], row["input"]["sku"]) for row in journal)
        require(observed == Counter(sent.items()), "Dispatch identities disagree with destination")
        require(all(row["revision"] == revision.digest for row in journal), "Wrong revision")
        require(len({row["operation_key"] for row in journal}) == len(journal), "Duplicate POST")
        charged = sum(row["charge_microusd"] for row in journal)
        require(consumed == charged, "Booked charges disagree with the destination")
        expected_posts = distinct_keys if ttl else tasks
        require(
            len(journal) == expected_posts, f"Expected {expected_posts} POSTs, saw {len(journal)}"
        )
        return {
            "tasks": tasks,
            "distinct_skus": distinct_keys,
            "ttl_seconds": ttl,
            "destination_posts": len(journal),
            "cache_hits": tasks - len(sent),
            "replays": replay_count,
            "posts_from_replays": 0,
            "ledger_charge_microusd": consumed,
            "destination_charge_microusd": charged,
            "result_sequence_sha256": answers.hexdigest(),
            "invoke_p50_ms": percentile_ms(times, 0.50),
            "invoke_p95_ms": percentile_ms(times, 0.95),
            "cell_elapsed_seconds": round(elapsed, 3),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=120, help="Sequential root tasks in each cell")
    parser.add_argument("--keys", type=int, default=8, help="Distinct SKUs in the repeated cell")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True, help="New JSON file for all cells")
    args = parser.parse_args()
    if not 1 <= args.keys < args.tasks or args.rounds < 1:
        parser.error("Use 1 <= --keys < --tasks and at least one round")
    if args.output.exists() or not args.output.parent.is_dir():
        parser.error("--output must be a new file in an existing directory")

    cells = []
    for repetition in range(1, args.rounds + 1):
        policies = (0, TTL) if repetition % 2 else (TTL, 0)
        for workload, distinct in (("repeated", args.keys), ("unique", args.tasks)):
            pair = []
            for ttl in policies:
                cell = run_cell(args.tasks, distinct, ttl)
                cells.append({"round": repetition, "workload": workload, **cell})
                pair.append(cell)
            require(
                pair[0]["result_sequence_sha256"] == pair[1]["result_sequence_sha256"],
                "The two policies returned different inventory results",
            )

    report = {
        "schema_version": 1,
        "benchmark": "inventory-bindings",
        "status": "pass",
        "package": {
            "distribution": "forge-os-check",
            "version": metadata.version("forge-os-check"),
        },
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "tasks_per_cell": args.tasks,
        "keys_in_repeated_workload": args.keys,
        "rounds": args.rounds,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "destination_fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "workload": "Sequential direct reads from a stable loopback inventory with a fresh ledger "
        "per cell, one exact replay per task, one actor and one pinned revision.",
        "timing": "Nearest-rank percentiles of the first Host.invoke per task, including cache "
        "misses. Task start, exact replay, finish, and balance verification are excluded.",
        "charges": "Simulated provider tariff recorded by the destination in micro-USD.",
        "cells": cells,
    }
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    print(f"Verified {len(cells)} cells. Results written to {args.output}")


if __name__ == "__main__":
    main()
