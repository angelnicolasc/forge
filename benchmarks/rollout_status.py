"""Compare complete rollout-status reads from two independently installed Forge wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
from contextlib import chdir, closing
from importlib.metadata import distribution
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns

from forge_check.governed import rollout_projection
from forge_check.governed.control import Control
from forge_check.governed.db import Store

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capacity import CAP, CHECKOUT, OWNER, Oracle, World, add_history, manifest, seed

HISTORY = 10_000
SAMPLES_PER_BLOCK = 8
ORDER = ("reference", "candidate", "candidate", "reference") * 2


def build_snapshot(directory: Path) -> Path:
    snapshot = directory / "snapshot.sqlite3"
    with chdir(directory), Oracle() as destination:
        shutil.copytree(CHECKOUT / "examples" / "check-support", "support")
        world = World("source", destination, manifest(budget=CAP, model_reserve=40_000))
        seed(world, destination)
        if len(destination.dispatches()) != 4:
            raise ValueError("Fixture requires four physical template dispatches")
        world.control.stop_rollout(OWNER, "current", 1)
        add_history(world, HISTORY)
        with world.store.write() as conn:
            moved = conn.execute(
                "UPDATE rollout_allocations SET rollout_id = 'current' "
                "WHERE rollout_id = 'previous' AND root_id GLOB 'hist-*'"
            )
            if moved.rowcount != HISTORY:
                raise ValueError("Incomplete synthetic history")
            # Bulk history bypasses the SDK. Rebuild its derived totals on this fixture.
            conn.execute("DELETE FROM rollout_capacity_open")
            conn.execute("DELETE FROM rollout_capacity_closed")
            rollout_projection.backfill(conn)
            rollout_projection.set_capacity_mode(conn, "projection")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Invalid ledger fixture")
        with world.store.read() as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
    return snapshot


def block(database: Path) -> dict:
    control = Control(Store(database, create=False))
    expected = control.get_rollout(OWNER, "current")
    traffic = [
        {
            "cohort": "canary",
            "allocated_roots": HISTORY + 3,
            "roots_with_dispatch_intent": HISTORY + 2,
            "dispatch_intents": HISTORY + 3,
            "roots_pending_confirmation": 0,
        }
    ]
    if expected["state"] != "stopped" or expected["traffic_by_cohort"] != traffic:
        raise ValueError("Unexpected rollout state or cohort counts")
    samples = []
    for _ in range(SAMPLES_PER_BLOCK):
        start = perf_counter_ns()
        observed = control.get_rollout(OWNER, "current")
        samples.append(perf_counter_ns() - start)
        if observed != expected:
            raise ValueError("Operator response changed between reads")
    package = distribution("forge-os-check")
    origin = json.loads(package.read_text("direct_url.json") or "{}")
    try:
        wheel_sha = origin["archive_info"]["hashes"]["sha256"]
    except KeyError as error:
        raise ValueError("Install each interpreter from a local wheel file") from error
    return {
        "wheel_sha256": wheel_sha,
        "package_version": package.version,
        "response": expected,
        "samples_ns": samples,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
    }


def compare(directory: Path, reference_python: Path) -> dict:
    snapshot = build_snapshot(directory)
    interpreters = {"reference": reference_python, "candidate": Path(sys.executable)}
    arms: dict[str, dict] = {}
    expected = None
    for name in interpreters:
        shutil.copyfile(snapshot, directory / f"{name}.sqlite3")
    for name in ORDER:
        path = directory / f"{name}.sqlite3"
        result = subprocess.run(
            [str(interpreters[name]), "-I", str(Path(__file__).resolve()), "--sample", str(path)],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        data = json.loads(result.stdout)
        response = data.pop("response")
        if expected is None:
            expected = response
        if response != expected:
            raise ValueError("The wheels returned different operator responses")
        samples = data.pop("samples_ns")
        arm = arms.setdefault(name, {"runtime": data, "samples_ns": []})
        if arm["runtime"] != data:
            raise ValueError("Interpreter or installed distribution changed during measurement")
        arm["samples_ns"].extend(samples)
    if arms["reference"]["runtime"]["wheel_sha256"] == arms["candidate"]["runtime"]["wheel_sha256"]:
        raise ValueError("Reference and candidate must be distinct wheels")
    for field in ("python", "sqlite"):
        if arms["reference"]["runtime"][field] != arms["candidate"]["runtime"][field]:
            raise ValueError(f"Both wheels must use the same {field} version")
    for arm in arms.values():
        values = sorted(arm["samples_ns"])
        arm["median_ms"] = statistics.median(values) / 1e6
        arm["p95_ms"] = values[math.ceil(len(values) * 0.95) - 1] / 1e6
    return {
        "benchmark": "rollout-status",
        "measurement": "Full Control.get_rollout read with fresh transactions on an idle ledger",
        "history": HISTORY,
        "open_roots": 3,
        "rollout_state": "stopped",
        "capacity_mode": "projection",
        "block_order": ORDER,
        "warmups_per_block": 1,
        "samples_per_block": SAMPLES_PER_BLOCK,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "full_response_equal": True,
        "full_response_sha256": hashlib.sha256(
            json.dumps(expected, sort_keys=True).encode()
        ).hexdigest(),
        "arms": arms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-python", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sample", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.sample:
        print(json.dumps(block(args.sample)))
        return
    if not args.reference_python or not args.output:
        parser.error("Supply --reference-python and --output")
    if args.output.exists():
        parser.error("Use a new output file")
    with TemporaryDirectory() as tmp:
        data = compare(Path(tmp).resolve(), args.reference_python.absolute())
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
