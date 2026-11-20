"""P23 technical rehearsal. Offline wheels, disposable Linux Vast VM, no publishing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from preview_index import rehearse

WINDOW = ["--since", "2020-01-01T00:00:00Z", "--until", "2090-01-01T00:00:00Z"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def wheel_version(path):
    match = re.fullmatch(r"forge_os_check-([0-9][a-zA-Z0-9.]+)-py3-none-any\.whl", path.name)
    if match is None:
        raise ValueError("Unexpected standalone wheel filename")
    return match[1]


def dump(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def state(db):
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
        return {
            "schema": conn.execute("pragma user_version").fetchone()[0],
            "integrity": conn.execute("pragma integrity_check").fetchone()[0],
            "receipts": [
                list(row)
                for row in conn.execute(
                    "SELECT event_id, attempt_id, phase FROM receipts ORDER BY seq"
                )
            ],
        }


class Trial:
    def __init__(self, work, evidence, steps):
        self.cwd = work
        self.evidence = evidence
        self.steps = steps
        self.env = {
            "HOME": str(work),
            "PATH": os.defpath,
            "LC_ALL": "C.UTF-8",
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_CACHE_DIR": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "FORGE_FINGERPRINT_KEY": os.urandom(32).hex(),
        }

    def run(self, name, args, expected=0):
        start = time.perf_counter()
        proc = subprocess.run(
            [str(value) for value in args],
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        (self.evidence / (name + ".stdout")).write_text(proc.stdout, encoding="utf-8")
        (self.evidence / (name + ".stderr")).write_text(proc.stderr, encoding="utf-8")
        self.steps.append(
            {
                "name": name,
                "expected_exit": expected,
                "exit": proc.returncode,
                "elapsed_s": round(time.perf_counter() - start, 4),
            }
        )
        require(proc.returncode == expected, f"{name}: {proc.returncode}, expected {expected}")
        return proc.stdout

    def venv(self, name):
        directory = self.cwd / name
        self.run(name + "-venv", [sys.executable, "-I", "-m", "venv", directory])
        return directory / "bin"

    def install(self, name, bin_dir, version, wheel):
        py = bin_dir / "python"
        self.run(
            name + "-install",
            [py, "-I", "-m", "pip", "install", "--no-index", "--no-deps", "--upgrade", wheel],
        )
        self.run(name + "-pip-check", [py, "-I", "-m", "pip", "check"])
        listing = json.loads(
            self.run(name + "-dists", [py, "-I", "-m", "pip", "list", "--format=json"])
        )
        installed = {d["name"].lower(): d["version"] for d in listing}
        require(installed.get("forge-os-check") == version, "unexpected installed version")
        require(
            not (set(installed) - {"forge-os-check", "pip", "setuptools", "wheel"}),
            "unexpected runtime dependency in base environment",
        )
        return bin_dir / "forge"


def fixtures(work):
    headers = {"schema_version": 1, "rule_id": "support-routing-v1", "source": "external-p23"}
    cases = dict(headers, cases=[{"id": "P23-T1", "expected": "human"}])
    dump(work / "cases.json", cases)
    for name, rev, result in [
        ("stable", "r1", "human"),
        ("good", "r2", "human"),
        ("bad", "r3", "self_service"),
    ]:
        dump(
            work / f"{name}.json",
            dict(
                headers,
                revision=rev,
                cases=[
                    {"id": "P23-T1", "status": "completed", "observed": result},
                ],
            ),
        )
    bindings = ["support.model", "support.lookup"]
    common = {"version": "1", "contract": {}, "tariff": "fixture-v1", "reserve_microusd": 50}
    initial = {
        "schema_version": 1,
        "scope": "release.test",
        "budget_microusd": 1000,
        "policy": {"allowed_bindings": bindings},
        "check": {"rule_id": headers["rule_id"], "verifier": "forge-check/1"},
        "bindings": {
            bindings[0]: dict(
                common,
                kind="model",
                provider="fixture",
                model="fixture-model",
                prompt="Route fixture tickets",
                max_output_tokens=128,
            ),
            bindings[1]: dict(
                common,
                kind="tool",
                server="tickets",
                method="lookup",
                effect="read",
                idempotency=True,
            ),
        },
    }
    config = {
        "scope": initial["scope"],
        "initial": initial,
        "cases": cases,
        "principals": {
            "owner": {"roles": ["admin", "approve", "evidence", "inspect"], "bindings": []},
            "reader": {"roles": ["invoke"], "bindings": [bindings[1]]},
        },
    }
    dump(work / "bootstrap.json", config)
    stock = "inventory.stock"
    v2 = {
        **initial,
        "schema_version": 2,
        "scope": "release.inventory",
        "policy": {"allowed_bindings": [stock]},
        "bindings": {
            stock: dict(
                common,
                kind="tool",
                server="inventory",
                method="stock",
                effect="read",
                contract={
                    "schema_version": 1,
                    "visible": ["on_hand"],
                    "input": {
                        "type": "object",
                        "required": ["sku"],
                        "properties": {"sku": {"type": "string", "format": "identifier"}},
                    },
                    "output": {
                        "type": "object",
                        "required": ["on_hand"],
                        "properties": {"on_hand": {"type": "integer", "minimum": 0}},
                    },
                },
            )
        },
    }
    config["scope"], config["initial"] = v2["scope"], v2
    config["principals"]["reader"]["bindings"] = [stock]
    dump(work / "inventory.json", config)


def check_cli(trial, cli, prefix):
    trial.run(prefix + "-help", [cli, "--help"])
    for case, expected, status in [
        ("pass", 0, "good"),
        ("fail", 1, "bad"),
        ("insufficient", 2, "good"),
    ]:
        path = trial.evidence / f"{prefix}-{case}.json"
        args = [
            cli,
            "check",
            "--cases",
            "cases.json",
            "--candidate",
            status + ".json",
            "--output",
            path,
        ]
        if case != "insufficient":
            args.extend(["--baseline", "stable.json"])
        trial.run(prefix + "-" + case, args, expected)
        require(json.loads(path.read_text())["verdict"] == case, "CLI/JSON disagree")


@contextmanager
def destination(journal):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/dispatch":
                self.send_error(404)
                return
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = req["input"]
            stock = req["binding_id"] == "inventory.stock"
            journal.append(
                {
                    "attempt": req["attempt_id"],
                    "operation": req["operation_key"],
                    "revision": req["revision"],
                    "binding": req["binding_id"],
                    "input": data,
                }
            )
            observation = {
                "attempt_id": req["attempt_id"],
                "operation_key": req["operation_key"],
                "observation_id": "obs." + req["attempt_id"],
                "outcome": "succeeded",
                "confirmation": "verified",
                "charge_microusd": 10,
                "charge_ref": "charge." + req["attempt_id"],
                "upstream_status": 200,
                "result": {"on_hand": 17}
                if stock
                else {"ticket_id": data["ticket_id"], "label": "human"},
            }
            body = json.dumps(observation).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def public_call(trial, cli, db, origin, name, binding="support.lookup", expected=0):
    payload = {"sku": "RELEASE-SKU"} if binding == "inventory.stock" else {"ticket_id": "P23-T1"}
    if binding.endswith(".model"):
        payload["text"] = "synthetic"
    response = trial.run(
        name,
        [
            cli,
            "wrap",
            "--db",
            db,
            "--actor",
            "reader",
            "--task",
            "task." + name,
            "--worker",
            "p23-worker",
            "--case",
            "P23-T1",
            "--provider",
            origin,
            "--binding",
            binding,
            "--operation",
            "op." + name,
            "--attempt",
            name,
            "--input",
            json.dumps(payload),
        ],
        expected,
    )
    if expected == 3:
        return None
    result = json.loads(response)
    require(
        result["attempt"]["status"] == ("denied" if expected else "succeeded"),
        "unexpected governed result",
    )
    return result


def exercise(args, report, out):
    wheel = args.wheel.resolve(strict=True)
    previous = args.previous.resolve(strict=True)
    candidate_version = wheel_version(wheel)
    previous_version = wheel_version(previous)
    require(
        previous_version == "0.3.0" and previous != wheel, "Expected supported standalone 0.3.0"
    )
    for path, expected in ((wheel, args.sha256), (previous, args.previous_sha256)):
        require(
            re.fullmatch(r"[0-9a-f]{64}", expected) and sha(path) == expected, "Wheel pin mismatch"
        )
        report["wheel_sha256"][wheel_version(path)] = expected
    evidence = out / "release-steps"
    evidence.mkdir(exist_ok=False)
    journal = report["destination"]
    with tempfile.TemporaryDirectory(prefix="p23-", dir=out) as tmp:
        work = Path(tmp)
        fixtures(work)
        trial = Trial(work, evidence, report["steps"])
        clean = trial.install("clean", trial.venv("fresh"), candidate_version, wheel)
        check_cli(trial, clean, "clean")
        extra_env = trial.venv("with-executor")
        trial.run(
            "extra-install",
            [
                extra_env / "python",
                "-Im",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                args.wheelhouse,
                f"{wheel}[code-mode]",
            ],
        )
        trial.run("extra-pip-check", [extra_env / "python", "-Im", "pip", "check"])
        trial.run("extra-import", [extra_env / "python", "-Ic", "import quickjs_rs, wasmtime"])
        bin_dir = trial.venv("lifecycle")
        cli = trial.install("previous", bin_dir, previous_version, previous)
        db = work / "live.db"
        backup = work / "previous.db"
        bootstrap = json.loads(
            trial.run(
                "bootstrap-legacy-contract",
                [cli, "bootstrap", "--db", db, "--config", "bootstrap.json"],
            )
        )
        with destination(journal) as origin:
            public_call(trial, cli, db, origin, "before-upgrade")
            before = state(db)
            require(
                before["schema"] == 2 and before["integrity"] == "ok" and before["receipts"],
                "invalid baseline ledger",
            )
            backup_report = json.loads(
                trial.run(
                    "backup-before-upgrade",
                    [cli, "backup", "--db", db, "--actor", "owner", "--output", backup],
                )
            )
            require(backup_report["sha256"] == sha(backup), "Recovery snapshot hash mismatch")
            cli = trial.install("upgrade-in-place", bin_dir, candidate_version, wheel)
            check_cli(trial, cli, "upgraded")
            inspect = ["verify", "--db", db, "--actor", "owner", *WINDOW]
            require(state(db) == before, "Installing the preview altered receipt history")
            alias = json.loads(trial.run("verify-after-upgrade", [cli, *inspect]))["active_alias"]
            require(alias["revision"] == bootstrap["revision"], "active revision changed")
            public_call(trial, cli, db, origin, "after-upgrade")
            public_call(trial, cli, db, origin, "forbidden-model", "support.model", 1)
            exported = json.loads(
                trial.run("receipts-v2", [cli, "export", "--db", db, "--actor", "owner"])
            )
            require(
                {row["attempt_id"] for row in exported["receipts"]}
                >= {"before-upgrade", "after-upgrade", "forbidden-model"},
                "missing retained receipts",
            )
            require(
                [row["attempt"] for row in journal] == ["before-upgrade", "after-upgrade"],
                "destination journal differs from allowed dispatches",
            )
            require(
                all(row["revision"] == alias["revision"] for row in journal),
                "provider and active revision disagree",
            )
            current = state(db)
            modern = work / "new-contract.db"
            trial.run(
                "bootstrap-v2-contract",
                [cli, "bootstrap", "--db", modern, "--config", "inventory.json"],
            )
            result = public_call(trial, cli, modern, origin, "stock-on-preview", "inventory.stock")
            require(result["result"] == {"on_hand": 17}, "Preview stock contract failed")
            snapshot_v2 = state(modern)
            rollback = trial.install("rollback-reader", bin_dir, previous_version, previous)
            old_alias = json.loads(trial.run("rollback-inspect-v1-contract", [rollback, *inspect]))[
                "active_alias"
            ]
            require(
                old_alias == alias and state(db) == current,
                "Old reader altered the compatible ledger",
            )
            public_call(
                trial, rollback, modern, origin, "stock-on-old-client", "inventory.stock", 3
            )
            require(
                journal[-1]["attempt"] == "stock-on-preview" and len(journal) == 3,
                "Incompatible client dispatched to the new binding",
            )
            require(
                state(modern) == snapshot_v2 and sha(backup) == backup_report["sha256"],
                "Downgrade probe altered receipts or recovery snapshot",
            )
            report["rollback"] = {
                "live_schema": current["schema"],
                "pre_upgrade_schema": before["schema"],
                "receipts_before": len(before["receipts"]),
                "receipts_after": len(current["receipts"]),
                "backup_sha256": backup_report["sha256"],
                "old_reader_inspected_compatible_ledger": True,
                "old_client_refused_v2_dispatch": True,
                "resumed_writes_from_old_snapshot": False,
                "reason": "Retain live state after effects. Drain v2 revision traffic before a client rollback.",
            }
    with tempfile.TemporaryDirectory(prefix="preview-index-") as index:
        report["temporary_index"] = rehearse(wheel, Path(index), candidate_version, args.sha256)
    require(
        sha(wheel) == args.sha256 and sha(previous) == args.previous_sha256,
        "wheel bytes changed during rehearsal",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--previous-sha256", required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    lab = os.environ.get("FORGE_VAST_LAB_ID", "")
    if (
        sys.platform != "linux"
        or sys.version_info[:2] != (3, 12)
        or not re.fullmatch(r"[1-9][0-9]*", lab)
    ):
        return 2
    out = args.output_dir.resolve()
    args.wheelhouse = args.wheelhouse.resolve(strict=True)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "release.json").exists() or (out / "release-steps").exists():
        print("Use an unused output directory for every attempt.", file=sys.stderr)
        return 2
    report = {
        "scope": "P23 technical rehearsal, x86_64 claim only if platform matches",
        "platform": os.uname().machine,
        "python": sys.version.split()[0],
        "vast_lab_id": lab,
        "wheel_sha256": {},
        "steps": [],
        "destination": [],
        "published": False,
        "release_signature_policy_verified_externally": "must be checked by caller",
    }
    try:
        exercise(args, report, out)
        report["status"] = "pass"
    except Exception as error:
        report["status"] = "fail"
        report["reason"] = f"{type(error).__name__}: {error}"
    dump(out / "release.json", report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(out / "release.json"),
                "reason": report.get("reason"),
            }
        )
    )
    return int(report["status"] != "pass")


if __name__ == "__main__":
    sys.exit(main())
