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
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from forge_check.governed.control import Control
from forge_check.governed.db import Store
from forge_check.governed.host import Host, HttpProvider
from forge_check.governed.ledger import Request
from forge_check.governed.revision import Revision
from preview_bindings import (
    CASES,
    KEY,
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


def read(db: Path, origin: str, sku: str, action: str, clock_at: int | None) -> dict:
    seed = action == "origin"
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

        control.start_task(READER, action, worker="monitor")
        host = Host(store, fingerprint_key=KEY, provider=ObservedProvider(origin, token=TOKEN))
        value = host.invoke(Request(action, READER, "read", action, STOCK, {"sku": sku}))
        receipts = host.ledger.receipts(action)
        return {
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


def check(old_python: Path, root: Path) -> dict:
    ready, allow = threading.Event(), threading.Event()

    class ObservedDestination(Destination):
        def respond(self, envelope: dict) -> tuple[dict, bool]:
            reply, stall = super().respond(envelope)
            if envelope["input"]["sku"] == "WALL" and envelope["attempt_id"] == "origin":
                ready.set()
                require(allow.wait(10), "Held response timed out")
            return reply, stall

    with ObservedDestination(root / "destination.db") as dest:

        def run(py: Path, db: Path, sku: str, action: str, tick: int | None) -> dict:
            cmd = [
                str(py),
                str(Path(__file__).resolve()),
                "--operation",
                action,
                "--db",
                str(db),
                "--origin",
                dest.origin,
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
        seed = run(old_python, old_db, "MICRO", "origin", T0)
        require(seed["version"] == "0.4.0" and seed["value"] == 11, "Expected original 0.4.0")
        current_db = root / "current.db"
        seed_current = run(candidate, current_db, "MICRO", "origin", T0)
        dest.adjust("MICRO", -8)
        old_clone, upgraded = root / "baseline.db", root / "upgraded.db"
        snapshot(old_db, old_clone)
        snapshot(old_db, upgraded)
        cases = {}
        for name, py, db in (
            ("published", old_python, old_clone),
            ("upgraded", candidate, upgraded),
            ("fresh", candidate, current_db),
        ):
            before = run(py, db, "MICRO", "before", T0 + TTL * SECOND - 1)
            boundary = run(py, db, "MICRO", "boundary", T0 + TTL * SECOND)
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
            task = pool.submit(run, old_python, warm, "WALL", "origin", None)
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
        old = run(old_python, wall_old, "WALL", "after", None)
        new = run(candidate, wall_new, "WALL", "after", None)
        require(
            (old["disposition"], old["value"]) == ("cached", 11), "Warm control missed its window"
        )
        require((new["disposition"], new["value"]) == ("dispatch", 3), "Upgraded warm entry reused")
        # Two seeds and two refreshes for MICRO; one seed and one refresh for WALL.
        require(len(dest.journal) == 6, "Destination dispatch count disagrees with client results")
        return {
            "status": "pass",
            "vast_lab_id": os.environ["FORGE_VAST_LAB_ID"],
            "source_version": seed["version"],
            "candidate_version": seed_current["version"],
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
    parser.add_argument("--sku")
    parser.add_argument("--at", type=int)
    args = parser.parse_args()
    if sys.platform != "linux" or not os.environ.get("FORGE_VAST_LAB_ID", "").isdigit():
        parser.error("Run on the assigned Vast instance")
    if args.operation:
        print(json.dumps(read(args.db, args.origin, args.sku, args.operation, args.at)))
        return
    if not args.previous_python or not args.output:
        parser.error("Pass --previous-python and --output")
    with TemporaryDirectory() as tmp:
        report = check(args.previous_python, Path(tmp))
    with args.output.open("x", encoding="utf-8") as out:
        out.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
