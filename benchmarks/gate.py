"""Time fresh Forge Check CLI processes and verify verdicts on the public support fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import shutil
import statistics
import subprocess
import sysconfig
import time
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "check-support"
EXITS = {"pass": 0, "fail": 1, "insufficient": 2}
SCENARIOS = {
    "routing_preserved": ("candidate-pass.json", "pass"),
    "escalations_missed": ("candidate-fail.json", "fail"),
    "observations_missing": ("candidate-pass.json", "insufficient"),
}
AFFECTED = {"S07", "S08"}


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare(work: Path, name: str) -> Path:
    filename, _ = SCENARIOS[name]
    candidate = read(FIXTURE / filename)
    if name == "observations_missing":
        for row in candidate["cases"]:
            if row["id"] in AFFECTED:
                row["observed"] = None
    destination = work / f"{name}.json"
    write(destination, candidate)
    return destination


def check_report(report: dict, code: int, verdict: str, expected_routes: dict) -> None:
    outcomes = {case_id: verdict if case_id in AFFECTED else "pass" for case_id in expected_routes}
    actual = {row["id"]: row["result"] for row in report["cases"]}
    routes = {row["id"]: row["expected"] for row in report["cases"]}
    count_ok = len(report["cases"]) == len(expected_routes)
    if not count_ok or actual != outcomes or routes != expected_routes:
        raise ValueError("CLI ticket outcomes differ from the fixture oracle")
    if report["verdict"] != verdict or code != EXITS[verdict] or report["exit_code"] != code:
        raise ValueError(f"Expected {verdict}/{EXITS[verdict]}, got {report['verdict']}/{code}")


def benchmark(forge: str, work: Path, rounds: int) -> dict:
    cases = FIXTURE / "cases.json"
    baseline = FIXTURE / "baseline.json"
    expected_routes = {row["id"]: row["expected"] for row in read(cases)["cases"]}
    if len(expected_routes) != 24 or any(expected_routes[case] != "human" for case in AFFECTED):
        raise ValueError("Expected the 24-ticket support fixture with escalations S07 and S08")

    candidates = {name: prepare(work, name) for name in SCENARIOS}
    summaries: dict = {}
    for name, path in candidates.items():
        rows = read(path)["cases"]
        complete = len(rows) == 24 and {row["id"] for row in rows} == set(expected_routes)
        completed = sum(r["status"] == "completed" for r in rows)
        status_only = "pass" if complete and completed == len(rows) else "fail"
        if status_only != "pass":
            raise ValueError("All three controls must contain 24 completed tickets")
        summaries[name] = {
            "candidate_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "completion_only_verdict": status_only,
            "oracle_verdict": SCENARIOS[name][1],
            "completed_tickets": completed,
            "duration_ms": [],
        }

    report_path = work / "report.json"
    names = list(SCENARIOS)
    for iteration in range(rounds):
        # Rotate scenario order so that a single fixture does not always pay first-run effects.
        offset = iteration % len(names)
        for name in names[offset:] + names[:offset]:
            report_path.unlink(missing_ok=True)
            command = [
                forge,
                "check",
                "--cases",
                str(cases),
                "--baseline",
                str(baseline),
                "--candidate",
                str(candidates[name]),
                "--output",
                str(report_path),
            ]
            start = time.perf_counter_ns()
            result = subprocess.run(command, cwd=work, capture_output=True, timeout=30, check=False)
            elapsed = (time.perf_counter_ns() - start) / 1_000_000
            if not report_path.is_file():
                raise RuntimeError(f"{name} returned exit {result.returncode} without a report")
            report = read(report_path)
            check_report(report, result.returncode, SCENARIOS[name][1], expected_routes)
            summary = summaries[name]
            summary["duration_ms"].append(round(elapsed, 4))
            summary.update(
                forge_verdict=report["verdict"],
                forge_exit_code=result.returncode,
                observed_tickets=report["coverage"]["candidate_observed"],
                exceptions={r["id"]: r["result"] for r in report["cases"] if r["result"] != "pass"},
            )

    for summary in summaries.values():
        samples = sorted(summary["duration_ms"])
        summary["median_ms"] = round(statistics.median(samples), 4)
        summary["p95_ms"] = samples[math.ceil(0.95 * len(samples)) - 1]
    return {
        "schema_version": 1,
        "benchmark": "support-routing-gate",
        "package_version": version("forge-os-check"),
        "python": platform.python_version(),
        "system": platform.system(),
        "architecture": platform.machine(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256(cases.read_bytes()).hexdigest(),
        "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "tickets_per_scenario": len(expected_routes),
        "rounds_per_scenario": rounds,
        "invocations": rounds * len(names),
        "method": "Fresh CLI process per sample including startup and report write. "
        "Page cache may be warm. Nearest-rank p95. Repeated executions of three fixed exports.",
        "verified": True,
        "scenarios": summaries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    if options.rounds < 1:
        parser.error("--rounds must be positive")
    if options.output.exists() or not options.output.parent.is_dir():
        parser.error("--output must be a new file in an existing directory")
    forge = shutil.which("forge", path=sysconfig.get_path("scripts"))
    if forge is None:
        parser.error("Install forge-os-check into the Python environment running this file")
    with TemporaryDirectory() as temporary:
        summary = benchmark(forge, Path(temporary), options.rounds)
    write(options.output, summary)
    print(f"Verified {len(SCENARIOS)} routing scenarios across {summary['invocations']} CLI runs.")


if __name__ == "__main__":
    main()
