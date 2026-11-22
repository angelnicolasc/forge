"""Check a warm ledger created by the published wheel across a cache freshness upgrade."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from forge_check.governed.control import Control
from forge_check.governed.db import SCHEMA_VERSION, Store
from forge_check.governed.host import Host, HttpProvider
from forge_check.governed.ledger import Request
from forge_check.governed.recovery import migrate
from forge_check.governed.revision import Revision
from preview_bindings import (
    CASES,
    KEY,
    OWNER,
    PRINCIPALS,
    READER,
    STOCK,
    TOKEN,
    Destination,
    require,
    revision_document,
)

TTL = 2
SECOND = 1_000_000
# Keep the microsecond boundary deterministic for both interpreters.
T0 = 1_800_000_000_000_000


def read(db: Path, origin: str, branch: str, sku: str, action: str, clock_at: int | None) -> dict:
    seed = action == "origin"
    invocation = f"{branch}-{sku.lower()}-{action}"
    store = Store(db, create=seed)
    control = Control(store)
    if seed:
        rev = Revision.parse(revision_document(ttl=TTL))
        control.bootstrap("inventory-preview", rev, CASES, PRINCIPALS)
    clock = (
        nullcontext()
        if clock_at is None
        else patch("forge_check.governed.ledger._utc", return_value=clock_at)
    )
    with clock as tick:

        class ObservedProvider(HttpProvider):
            def dispatch(self, body: dict) -> dict:
                reply = super().dispatch(body)
                if seed and tick is not None:
                    tick.return_value = clock_at + SECOND
                return reply

        control.start_task(READER, invocation, worker="monitor")
        host = Host(store, fingerprint_key=KEY, provider=ObservedProvider(origin, token=TOKEN))
        value = host.invoke(Request(invocation, READER, "read", invocation, STOCK, {"sku": sku}))
        receipts = host.ledger.receipts(invocation)
        return {
            "attempt_id": invocation,
            "version": version("forge-os-check"),
            "disposition": value.disposition,
            "value": value.result["on_hand"] if value.result is not None else None,
            "valid_until": next(
                (r["evidence"]["valid_until"] for r in receipts if r["phase"] == "cache_hit"),
                None,
            ),
        }


def snapshot(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as copy:
        original.backup(copy)


def upgrade_snapshot(path: Path) -> dict[str, object] | None:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
    if schema == SCHEMA_VERSION:
        return None
    return migrate(path, OWNER, path.with_suffix(".before-migration.db"))


def check(old_python: Path, root: Path) -> dict:
    ready, allow = threading.Event(), threading.Event()

    class ObservedDestination(Destination):
        def respond(self, envelope: dict) -> tuple[dict, bool]:
            reply, stall = super().respond(envelope)
            if envelope["attempt_id"] == "published-wall-origin":
                ready.set()
                require(allow.wait(10), "Held response timed out")
            return reply, stall

    with ObservedDestination(root / "destination.db") as dest:

        def run(py: Path, db: Path, branch: str, sku: str, action: str, tick: int | None) -> dict:
            cmd = [
                str(py),
                str(Path(__file__).resolve()),
                "--operation",
                action,
                "--db",
                str(db),
                "--origin",
                dest.origin,
                "--branch",
                branch,
                "--sku",
                sku,
            ]
            if tick is not None:
                cmd.extend(["--at", str(tick)])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            require(result.returncode == 0, result.stderr)
            return json.loads(result.stdout)

        candidate = Path(sys.executable)
        old_db = root / "old.db"
        dest.adjust("MICRO", 11)
        seed = run(old_python, old_db, "published", "MICRO", "origin", T0)
        require(seed["version"] == "0.4.0" and seed["value"] == 11, "Expected original 0.4.0")
        current_db = root / "current.db"
        seed_current = run(candidate, current_db, "fresh", "MICRO", "origin", T0)
        dest.adjust("MICRO", -8)
        old_clone, upgraded = root / "baseline.db", root / "upgraded.db"
        snapshot(old_db, old_clone)
        snapshot(old_db, upgraded)
        exact_upgrade = upgrade_snapshot(upgraded)
        cases = {}
        for name, py, db in (
            ("published", old_python, old_clone),
            ("upgraded", candidate, upgraded),
            ("fresh", candidate, current_db),
        ):
            before = run(py, db, name, "MICRO", "before", T0 + TTL * SECOND - 1)
            boundary = run(py, db, name, "MICRO", "boundary", T0 + TTL * SECOND)
            historical = name == "published"
            require(
                (before["disposition"], before["value"]) == ("cached", 11),
                f"{name}: warm cache lost before its expiry",
            )
            expected_end = T0 + TTL * SECOND + (SECOND if historical else 0)
            require(before["valid_until"] == expected_end, f"{name}: incorrect expiry in receipt")
            expected = ("cached", 11) if historical else ("dispatch", 3)
            require((boundary["disposition"], boundary["value"]) == expected, f"{name}: {boundary}")
            cases[name] = {"before": before, "boundary": boundary}
        dest.adjust("WALL", 11)
        warm = root / "warm.db"
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(run, old_python, warm, "published", "WALL", "origin", None)
            try:
                require(ready.wait(5), "No source snapshot for warm upgrade trial")
                start = time.monotonic_ns()
                dest.adjust("WALL", -8)
                time.sleep(TTL + 0.15)
                held = (time.monotonic_ns() - start) / 1_000_000_000
            finally:
                allow.set()
            require(task.result()["value"] == 11, "The provider snapshot was not retained")
        wall_old, wall_new = root / "wall-old.db", root / "wall-new.db"
        snapshot(warm, wall_old)
        snapshot(warm, wall_new)
        wall_upgrade = upgrade_snapshot(wall_new)
        old = run(old_python, wall_old, "published", "WALL", "after", None)
        new = run(candidate, wall_new, "upgraded", "WALL", "after", None)
        require(
            (old["disposition"], old["value"]) == ("cached", 11), "Warm control missed its window"
        )
        require((new["disposition"], new["value"]) == ("dispatch", 3), "Upgraded warm entry reused")
        expected_dispatches = Counter(
            {
                ("published-micro-origin", "MICRO"): 1,
                ("fresh-micro-origin", "MICRO"): 1,
                ("upgraded-micro-boundary", "MICRO"): 1,
                ("fresh-micro-boundary", "MICRO"): 1,
                ("published-wall-origin", "WALL"): 1,
                ("upgraded-wall-after", "WALL"): 1,
            }
        )
        observed = Counter((r["attempt_id"], r["input"]["sku"]) for r in dest.journal)
        require(observed == expected_dispatches, f"Unexpected destination journal: {observed}")
        keys = {r["operation_key"] for r in dest.journal}
        require(len(keys) == len(expected_dispatches), "Dispatches share an operation key")
        return {
            "status": "pass",
            "vast_lab_id": os.environ["FORGE_VAST_LAB_ID"],
            "source_version": seed["version"],
            "candidate_version": seed_current["version"],
            "migration_backups": {"exact_boundary": exact_upgrade, "wall_clock": wall_upgrade},
            "exact_boundary": cases,
            "wall_clock": {"held_seconds": held, "old": old, "new": new},
            "destination_journal": dest.journal,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-python", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--operation")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--origin")
    parser.add_argument("--branch")
    parser.add_argument("--sku")
    parser.add_argument("--at", type=int)
    args = parser.parse_args()
    if sys.platform != "linux" or not os.environ.get("FORGE_VAST_LAB_ID", "").isdigit():
        parser.error("Requires Linux and a numeric FORGE_VAST_LAB_ID.")
    if args.operation:
        outcome = read(args.db, args.origin, args.branch, args.sku, args.operation, args.at)
        print(json.dumps(outcome))
        return
    if not args.previous_python or not args.output:
        parser.error("Pass --previous-python and --output")
    with TemporaryDirectory() as tmp:
        report = check(args.previous_python, Path(tmp))
    with args.output.open("x", encoding="utf-8") as out:
        out.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
