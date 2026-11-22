"""Vast E2E adoption proxy for the public Forge Check wheel. Python stdlib."""

import argparse
import hashlib
import json
import os
import platform
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The native client runs in a separate process. Its only inputs are raw domain
# requests and its own destination database. The expected results stay with CI.
CLIENT = """import json, sqlite3, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

FAULT = False
RELEASE = "baseline"
def dispatch(conn, item):
    cid = item["id"]
    data = item["data"]
{domain}

with sqlite3.connect(sys.argv[2]) as conn:
    for item in json.loads(Path(sys.argv[1]).read_text()):
        dispatch(conn, item)
print(json.dumps({{"application_status":"completed","release": RELEASE}}))
"""

# Each tuple carries a raw input plus a golden label set by the corpus designer.
# The simulator never sees that label. The read-only oracle interprets concrete
# destination records (rows, amounts, routes, UTC instants or publication flags).
PROFILES = {
    "support": {
        "source": "support-queue",
        "table": "queue",
        "writer": """    urgent, tier = data
    if urgent or tier == "priority":
        if not (FAULT and tier == "priority" and not urgent):
            conn.execute("INSERT INTO queue VALUES (?, ?, ?)", (cid, "human", 1))""",
        "rows": [
            ([True, "normal"], "human"),
            ([False, "priority"], "human"),
            ([False, "normal"], "self_service"),
            ([True, "priority"], "human"),
        ],
        "fault": 1,
    },
    "accounts-payable": {
        "source": "settlement-journal",
        "table": "settlements",
        "writer": """    cents, duplicate = data
    if not duplicate or FAULT:
        conn.execute("INSERT INTO settlements VALUES (?, ?, ?)", (cid, "USD", cents))""",
        "rows": [
            ([3750, False], "posted.3750"),
            ([520, True], "held"),
            ([9999, False], "posted.9999"),
            ([800, False], "posted.800"),
        ],
        "fault": 1,
    },
    "returns": {
        "source": "carrier-manifest",
        "table": "shipments",
        "writer": """    hazard, pickup = data
    lane = "ground" if hazard else ("pickup" if pickup else "standard")
    if FAULT and hazard:
        lane = "air"
    conn.execute("INSERT INTO shipments VALUES (?, ?, ?)", (cid, lane, 1))""",
        "rows": [
            ([False, False], "carrier.standard"),
            ([False, True], "carrier.pickup"),
            ([True, False], "carrier.ground"),
            ([False, False], "carrier.standard"),
        ],
        "fault": 2,
    },
    "calendar": {
        "source": "calendar-bookings",
        "table": "bookings",
        "writer": """    naive, offset, available = data
    if not available:
        return
    local = datetime.fromisoformat(naive)
    zone = timezone(timedelta(hours=offset))
    when = local.replace(tzinfo=zone).astimezone(timezone.utc)
    if FAULT and when.date() != local.date():
        when = when.replace(year=local.year, month=local.month, day=local.day)
    conn.execute("INSERT INTO bookings VALUES (?, ?, ?)", (cid, when.strftime("%Y%m%dT%H%MZ"), 1))""",
        "rows": [
            (["2026-11-20T23:15", -3, True], "utc.20261121T0215Z"),
            (["2026-11-20T15:45", 2, True], "utc.20261120T1345Z"),
            (["2026-11-21T09:00", 0, False], "unavailable"),
            (["2026-11-21T09:00", 0, True], "utc.20261121T0900Z"),
        ],
        "fault": 0,
    },
    "editorial-catalog": {
        "source": "published-catalog",
        "table": "listings",
        "writer": """    licensed, allowed_region, embargo = data
    if licensed and allowed_region and (not embargo or FAULT):
        conn.execute("INSERT INTO listings VALUES (?, ?, ?)", (cid, "public", 1))""",
        "rows": [
            ([True, True, False], "published"),
            ([False, True, False], "withheld"),
            ([True, False, False], "withheld"),
            ([True, True, True], "withheld"),
        ],
        "fault": 3,
    },
}

CI = """#!/bin/sh
set -eu
exec "$FORGE_BIN" check --cases cases.json --baseline baseline.json --candidate candidate.json --output result.json
"""
EXPECT = {"good": ("pass", 0), "fault": ("fail", 1), "unavailable": ("insufficient", 2)}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(args, cwd, env, timeout=120, ensure=True):
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if ensure and result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} exit {result.returncode}: {result.stderr[-500:]}")
    return result


def read_destination(profile_name, rows):
    if len(rows) > 1:
        return "duplicate"
    if profile_name == "support":
        return ("human" if rows[0][0] == "human" else "wrong_queue") if rows else "self_service"
    if profile_name == "accounts-payable":
        if not rows:
            return "held"
        currency, amount = rows[0]
        return f"posted.{amount}" if currency == "USD" else "wrong_currency"
    if profile_name == "returns":
        return "carrier." + rows[0][0] if rows else "missing_shipment"
    if profile_name == "calendar":
        return "utc." + rows[0][0] if rows else "unavailable"
    return "published" if rows and rows[0][0] == "public" else "withheld"


def simulate(repo, root, profile_name, profile, revision, raw_inputs, unavailable, env, fault):
    root.mkdir(parents=True)
    source = CLIENT.format(domain=profile["writer"]).replace(
        'RELEASE = "baseline"', f'RELEASE = "{revision}"'
    )
    if fault:
        source = source.replace("FAULT = False", "FAULT = True")
    program = repo / "agent.py"
    program.write_text(source, encoding="utf-8")
    raw = root / "requests.json"
    dump(raw, raw_inputs)
    table = profile["table"]  # Fixed SQL identifiers come only from the profile table above.
    db = root / "destination.sqlite3"
    with sqlite3.connect(db) as dest:
        dest.execute(f"CREATE TABLE {table} (case_id TEXT, target TEXT, quantity INTEGER)")
    t0 = time.monotonic()
    call = command([sys.executable, "-I", str(program), str(raw), str(db)], repo, env)
    native_seconds = time.monotonic() - t0
    acknowledged = json.loads(call.stdout)["application_status"] == "completed"
    seen = []
    # The evidence collector opens its own read-only database connection after
    # the agent process has terminated. A missing snapshot is not a negative read.
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as oracle:
        oracle.execute("PRAGMA query_only = ON")
        for item in raw_inputs:
            cid = item["id"]
            observed = None
            if cid != unavailable:
                records = oracle.execute(
                    f"SELECT target, quantity FROM {table} WHERE case_id=? ORDER BY rowid", (cid,)
                ).fetchall()
                observed = read_destination(profile_name, records)
            seen.append(
                {
                    "id": cid,
                    "status": "completed" if acknowledged else "error",
                    "observed": observed,
                }
            )
    return (
        {
            "schema_version": 1,
            "rule_id": profile_name,
            "source": profile["source"],
            "revision": revision,
            "cases": seen,
        },
        {
            "native_seconds": round(native_seconds, 6),
            "client_sha256": hash_file(program),
            "destination_sha256": hash_file(db),
            "requests_sha256": hash_file(raw),
        },
    )


def run_profile(name, profile, number, wheel, workspace, evidence, lab_env):
    t0 = time.monotonic()
    repo = workspace / name
    repo.mkdir()
    archive = evidence / name
    archive.mkdir()
    result = {"name": name, "onboarding": {}, "changes": [], "scope": "public_cli_saved_results"}
    offline = {
        **lab_env,
        "HOME": str(repo),
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    command(["git", "init", "--quiet", "."], repo, offline)
    env_dir = workspace / ("venv-" + name)
    install_start = time.monotonic()
    command([sys.executable, "-I", "-m", "venv", str(env_dir)], repo, offline)
    command(
        [
            str(env_dir / "bin/python"),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-cache-dir",
            str(wheel),
        ],
        repo,
        offline,
    )
    result["venv_and_install_seconds"] = round(time.monotonic() - install_start, 6)
    cli = env_dir / "bin/forge"
    env = {**offline, "FORGE_BIN": str(cli)}
    (repo / "ci-check.sh").write_text(CI, encoding="utf-8")
    initial_ci_hash = hash_file(repo / "ci-check.sh")
    initial_cli_hash = hash_file(cli)
    result["ci_sha256"] = initial_ci_hash
    result["forge_version"] = command([str(cli), "--version"], repo, env).stdout.strip()
    turns = ["good", "fault", "unavailable"]
    rotations = turns[number % 3 :] + turns[: number % 3]
    work = [
        ("onboarding", "fault"),
        *[(f"pr-{idx}", mode) for idx, mode in enumerate(rotations, 1)],
    ]
    for step_no, (step, mode) in enumerate(work):
        trial = {"step": step, "change": mode, "passed": False}
        if step == "onboarding":
            result["onboarding"] = trial
        else:
            result["changes"].append(trial)
        try:
            step_start = time.monotonic()
            seed = 91327 + number * 100 + step_no
            rng = random.Random(seed)
            ids = [f"{name[:3]}-{n}" for n in rng.sample(range(1000, 9999), len(profile["rows"]))]
            raw = [
                {"id": key, "data": row[0]} for key, row in zip(ids, profile["rows"], strict=True)
            ]
            rng.shuffle(raw)
            cases = [
                {"id": key, "expected": row[1]}
                for key, row in zip(ids, profile["rows"], strict=True)
            ]
            rule = {
                "schema_version": 1,
                "rule_id": name,
                "source": profile["source"],
                "cases": cases,
            }
            dump(repo / "cases.json", rule)
            target = ids[profile["fault"]]
            directory = repo / "private-lab" / step
            evidence_dir = archive / step
            evidence_dir.mkdir()
            trial["seed"] = seed
            trial["expected_rule_sha256"] = hash_file(repo / "cases.json")
            baseline, base_info = simulate(
                repo, directory / "base", name, profile, "stable-" + step, raw, None, env, False
            )
            unavailable = target if mode == "unavailable" else None
            candidate, candidate_info = simulate(
                repo,
                directory / "next",
                name,
                profile,
                "next-" + step,
                raw,
                unavailable,
                env,
                mode == "fault",
            )
            dump(repo / "baseline.json", baseline)
            dump(repo / "candidate.json", candidate)
            report_path = repo / "result.json"
            report_path.unlink(missing_ok=True)
            trial.update({"baseline_native": base_info, "candidate_native": candidate_info})
            before_check = time.monotonic()
            job = command(["sh", "ci-check.sh"], repo, env, ensure=False)
            check_seconds = time.monotonic() - before_check
            report = json.loads(report_path.read_text(encoding="utf-8"))
            dump(evidence_dir / "report.json", report)
            dump(
                evidence_dir / "expected-and-inputs.json",
                {"sealed_rule": rule, "native_input": raw},
            )
            dump(evidence_dir / "observations.json", {"baseline": baseline, "candidate": candidate})
            expected_verdict, expected_exit = EXPECT[mode]
            expected_issues = {target} if mode != "good" else set()
            issues = {row["id"] for row in report["cases"] if row["result"] != "pass"}
            labels = {entry["id"]: entry["expected"] for entry in cases}
            rows = report["cases"]
            outcomes_match = all(
                row["id"] in labels
                and row["expected"] == labels[row["id"]]
                and row["result"] == (expected_verdict if row["id"] in expected_issues else "pass")
                for row in rows
            )
            eligible = len(cases)
            observed = eligible - int(mode == "unavailable")
            expected_coverage = {
                "eligible": eligible,
                "baseline_observed": eligible,
                "candidate_observed": observed,
                "comparable": observed,
            }
            unchanged = (
                hash_file(repo / "ci-check.sh") == initial_ci_hash
                and hash_file(cli) == initial_cli_hash
            )
            checks = {
                "exit": job.returncode == report.get("exit_code") == expected_exit,
                "verdict": report["verdict"] == expected_verdict,
                "case_results": outcomes_match
                and len(rows) == len(ids)
                and {r["id"] for r in rows} == set(ids),
                "affected_ids": issues == expected_issues,
                "coverage": report["coverage"] == expected_coverage,
                "next_action": bool(report["next_action"].strip()),
                "reusable_ci": unchanged,
                "revisions": report.get("baseline_revision") == baseline["revision"]
                and report.get("candidate_revision") == candidate["revision"],
                "source_and_rule": report.get("source") == profile["source"]
                and report.get("rule_id") == name,
                "terminal_cases": all(issue in job.stdout for issue in issues),
            }
            trial.update(
                {
                    "expected_verdict": expected_verdict,
                    "actual_verdict": report["verdict"],
                    "actual_exit": job.returncode,
                    "expected_affected_ids": sorted(expected_issues),
                    "actual_affected_ids": sorted(issues),
                    "coverage": report["coverage"],
                    "checks": checks,
                    "passed": all(checks.values()),
                    "ci_seconds": round(check_seconds, 6),
                    "trial_seconds": round(time.monotonic() - step_start, 6),
                }
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            sqlite3.Error,
            subprocess.TimeoutExpired,
        ) as exc:
            trial["error"] = f"{type(exc).__name__}: {exc}"
        if step == "onboarding":
            result["cold_machine_seconds"] = round(time.monotonic() - t0, 6)
    result["three_changes_with_unchanged_ci"] = all(
        trial["passed"] and trial["checks"]["reusable_ci"] for trial in result["changes"]
    )
    result["passed"] = result["onboarding"]["passed"] and result["three_changes_with_unchanged_ci"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    opts = parser.parse_args()
    lab_id = os.getenv("FORGE_VAST_LAB_ID", "").strip()
    if not lab_id or sys.platform != "linux":
        raise SystemExit("Requires Linux and a nonempty FORGE_VAST_LAB_ID.")
    if not opts.wheel.is_absolute() or not opts.wheel.is_file() or opts.wheel.suffix != ".whl":
        raise SystemExit("Supply the exact installed candidate wheel by absolute path")
    out = opts.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    evidence = out / "proxy-evidence"
    if evidence.exists() or (out / "proxy.json").exists():
        raise SystemExit("Use a fresh evidence destination for each run")
    evidence.mkdir()
    env = {
        "PATH": os.defpath + ":/usr/local/bin",
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "TZ": "UTC",
    }
    summary = {
        "schema": 1,
        "instance_id": lab_id,
        "wheel_sha256": hash_file(opts.wheel),
        "runner_sha256": hash_file(Path(__file__)),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "profiles": [],
        "status": "running",
        "method": "Five cold per-repo venvs on one already booted host, offline wheel, four distinct raw cases per repository, one activation and three rotated CI changes.",
        "claim_limits": [
            "No human participants",
            "No human retention metric",
            "Does not execute governed custom bindings",
            "Host OS cache and wheel acquisition time excluded",
            "Independent synthetic destination read; no vendor ecosystem or production baseline",
        ],
    }
    with tempfile.TemporaryDirectory(prefix="forge-adoption-lab-") as tmp:
        for number, (name, profile) in enumerate(PROFILES.items()):
            try:
                run = run_profile(name, profile, number, opts.wheel, Path(tmp), evidence, env)
            except (
                OSError,
                RuntimeError,
                sqlite3.Error,
                ValueError,
                subprocess.TimeoutExpired,
            ) as exc:
                run = {
                    "name": name,
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "changes": [],
                }
            summary["profiles"].append(run)
            dump(out / "proxy.json", summary)
    runs = summary["profiles"]
    changes = [trial for run in runs for trial in run["changes"]]
    summary["totals"] = {
        "assigned_repos": len(PROFILES),
        "onboarding_correct_under_600s": sum(
            run.get("onboarding", {}).get("passed", False)
            and run.get("cold_machine_seconds", 601) <= 600
            for run in runs
        ),
        "assigned_changes": 15,
        "executed_changes": len(changes),
        "correct_changes": sum(trial["passed"] for trial in changes),
        "repos_three_changes_stable": sum(
            run.get("three_changes_with_unchanged_ci", False) for run in runs
        ),
        "false_greens": sum(
            trial.get("actual_verdict") == "pass" and trial.get("expected_verdict") != "pass"
            for run in runs
            for trial in [run.get("onboarding", {}), *run["changes"]]
        ),
    }
    totals = summary["totals"]
    success = (
        all(run["passed"] for run in runs)
        and totals["onboarding_correct_under_600s"] >= 4
        and totals["correct_changes"] == 15
        and totals["false_greens"] == 0
    )
    summary["status"] = "pass" if success else "fail"
    dump(out / "proxy.json", summary)
    print(json.dumps({"status": summary["status"], "totals": totals}))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
