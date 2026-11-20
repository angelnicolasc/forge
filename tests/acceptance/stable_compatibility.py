"""Verify stable standalone installs and the same-ledger transition from RC3 on Vast."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from preview_release import (
    WINDOW,
    Trial,
    check_cli,
    destination,
    fixtures,
    public_call,
    require,
    sha,
    wheel_version,
)

RC3_WHEEL_SHA256 = "866abed30badeddecbd655aa402264269b81c879ff6d6cc75acafc2bd63e2ae0"
VERDICTS = ("pass", "fail", "insufficient")
DEMO = {
    "direct": {"on_hand": 7, "sku": "A-18"},
    "dispatches": ["A-18", "A-18", "B-42"],
    "program": {"total_on_hand": 9},
}


def venv(trial, name, interpreter):
    directory = trial.cwd / name
    trial.run(name + "-venv", [interpreter, "-I", "-m", "venv", directory])
    return directory / "bin"


def install_profiles(trial, minor, interpreter, args, version):
    prefix = "python-" + minor
    runtime = trial.run(
        prefix + "-version",
        [interpreter, "-Ic", "import platform; print(platform.python_version())"],
    ).strip()
    require(re.fullmatch(rf"{re.escape(minor)}\.[0-9]+", runtime), f"Wrong Python for {minor}")
    base_name = prefix + "-base"
    base = trial.install(base_name, venv(trial, base_name, interpreter), version, args.wheel)
    check_cli(trial, base, base_name)
    extras = venv(trial, prefix + "-extras", interpreter)
    trial.run(
        prefix + "-extras-install",
        [
            extras / "python",
            "-Im",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            args.wheelhouse,
            f"{args.wheel}[code-mode]",
        ],
    )
    trial.run(prefix + "-extras-pip-check", [extras / "python", "-Im", "pip", "check"])
    example = Path(__file__).resolve().parents[2] / "examples/governed_inventory.py"
    output = json.loads(trial.run(prefix + "-inventory", [extras / "python", "-I", example]))
    require(output == DEMO, f"Wrong installed inventory demo behavior on Python {minor}")
    return {
        "python": runtime,
        "base_dependency_free": True,
        "base_verdicts": {
            verdict: json.loads(
                (trial.evidence / f"{base_name}-{verdict}.json").read_text(encoding="utf-8")
            )["verdict"]
            for verdict in VERDICTS
        },
        "extra_inventory": output,
    }


def live_state(db):
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
        return {
            "schema": conn.execute("PRAGMA user_version").fetchone()[0],
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "receipts": conn.execute(
                """SELECT seq, event_id, task_id, attempt_id, phase, recorded_at, body
                   FROM receipts ORDER BY seq"""
            ).fetchall(),
        }


def receipt_digest(snapshot):
    receipts = json.dumps(snapshot["receipts"], separators=(",", ":")).encode()
    return hashlib.sha256(receipts).hexdigest()


def upgrade(trial, args, version):
    bin_dir = venv(trial, "rc3-transition", args.python312)
    cli = trial.install("rc3", bin_dir, "0.4.0rc3", args.previous_wheel)
    db = trial.cwd / "inventory.db"
    backup = trial.cwd / "before-stable.db"
    trial.run("rc3-bootstrap", [cli, "bootstrap", "--db", db, "--config", "inventory.json"])
    inspect = ["verify", "--db", db, "--actor", "owner", *WINDOW]
    alias_before = json.loads(trial.run("inspect-rc3", [cli, *inspect]))["active_alias"]
    journal = []
    with destination(journal) as origin:
        result = public_call(trial, cli, db, origin, "before-stable", "inventory.stock")
        require(result["result"] == {"on_hand": 17}, "Wrong inventory result on RC3")
        before = live_state(db)
        require(before["integrity"] == "ok" and before["receipts"], "Invalid RC3 ledger")
        backup_info = json.loads(
            trial.run(
                "backup-rc3", [cli, "backup", "--db", db, "--actor", "owner", "--output", backup]
            )
        )
        require(backup_info["sha256"] == sha(backup), "RC3 backup digest differs")
        cli = trial.install("stable-upgrade", bin_dir, version, args.wheel)
        alias_after = json.loads(trial.run("inspect-stable", [cli, *inspect]))["active_alias"]
        installed = live_state(db)
        require(installed == before and alias_after == alias_before, "Upgrade changed live state")
        result = public_call(trial, cli, db, origin, "after-stable", "inventory.stock")
        require(result["result"] == {"on_hand": 17}, "Wrong inventory result after upgrade")
        after = live_state(db)
        require(
            after["integrity"] == "ok"
            and after["receipts"][: len(before["receipts"])] == before["receipts"]
            and len(after["receipts"]) > len(before["receipts"]),
            "Upgrade did not preserve and extend receipt history",
        )
        cli = trial.install("rollback-rc3", bin_dir, "0.4.0rc3", args.previous_wheel)
        alias_rolled_back = json.loads(trial.run("inspect-rollback", [cli, *inspect]))[
            "active_alias"
        ]
        require(
            live_state(db) == after and alias_rolled_back == alias_before,
            "Rollback changed the live ledger or active revision",
        )
        result = public_call(trial, cli, db, origin, "after-rollback", "inventory.stock")
        require(result["result"] == {"on_hand": 17}, "Wrong inventory result after rollback")
    rolled_back = live_state(db)
    require(
        rolled_back["integrity"] == "ok"
        and rolled_back["receipts"][: len(after["receipts"])] == after["receipts"]
        and sha(backup) == backup_info["sha256"],
        "Rollback failed to preserve the live receipts or recovery snapshot",
    )
    require(
        [row["attempt"] for row in journal] == ["before-stable", "after-stable", "after-rollback"]
        and all(
            row["revision"] == alias_before["revision"]
            and row["binding"] == "inventory.stock"
            and row["input"] == {"sku": "RELEASE-SKU"}
            for row in journal
        ),
        "Destination did not observe the three reads under the original revision",
    )
    return {
        "schema_before": before["schema"],
        "schema_after": after["schema"],
        "backup_sha256": backup_info["sha256"],
        "historical_receipts_sha256": receipt_digest(before),
        "post_install_receipts_sha256": receipt_digest(installed),
        "receipt_count_before": len(before["receipts"]),
        "receipt_count_after": len(after["receipts"]),
        "alias_before": alias_before,
        "alias_after": alias_after,
        "alias_after_rollback": alias_rolled_back,
        "rollback_live_receipts_preserved": True,
        "destination": journal,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("wheel", "previous-wheel", "python311", "python312", "wheelhouse", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    args = parser.parse_args()
    lab_id = os.environ.get("FORGE_VAST_LAB_ID", "")
    if (
        sys.platform != "linux"
        or os.uname().machine != "x86_64"
        or not re.fullmatch(r"[1-9][0-9]*", lab_id)
    ):
        parser.error("Run in the approved Vast Linux x86_64 instance")
    require(re.fullmatch(r"[0-9a-f]{40}", args.commit), "Require the exact candidate commit")
    for name in ("wheel", "previous_wheel", "python311", "python312", "wheelhouse"):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    version = wheel_version(args.wheel)
    require(
        re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}", version),
        "Expected a final wheel",
    )
    require(
        re.fullmatch(r"[0-9a-f]{64}", args.wheel_sha256) and sha(args.wheel) == args.wheel_sha256,
        "Candidate wheel digest differs from the approved bytes",
    )
    require(
        wheel_version(args.previous_wheel) == "0.4.0rc3"
        and sha(args.previous_wheel) == RC3_WHEEL_SHA256,
        "Require the approved RC3 recovery wheel",
    )
    output = args.output.resolve()
    require(not output.exists(), "Use a new compatibility receipt path")
    evidence = output.parent / "stable-compatibility-steps"
    evidence.mkdir(parents=True)
    steps = []
    with tempfile.TemporaryDirectory(prefix="forge-stable-compatibility-") as folder:
        work = Path(folder)
        fixtures(work)
        trial = Trial(work, evidence, steps)
        installs = {
            "3.11": install_profiles(trial, "3.11", args.python311, args, version),
            "3.12": install_profiles(trial, "3.12", args.python312, args, version),
        }
        transition = upgrade(trial, args, version)
    require(
        sha(args.wheel) == args.wheel_sha256 and sha(args.previous_wheel) == RC3_WHEEL_SHA256,
        "Wheel bytes changed during compatibility rehearsal",
    )
    with output.open("x", encoding="utf-8") as receipt:
        receipt.write(
            json.dumps(
                {
                    "schema": 1,
                    "status": "pass",
                    "commit": args.commit,
                    "version": version,
                    "wheel_sha256": args.wheel_sha256,
                    "vast_lab_id": lab_id,
                    "platform": "linux-x86_64",
                    "previous_version": "0.4.0rc3",
                    "previous_wheel_sha256": RC3_WHEEL_SHA256,
                    "installs": installs,
                    "upgrade": transition,
                    "steps": steps,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
