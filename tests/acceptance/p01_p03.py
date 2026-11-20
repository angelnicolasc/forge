"""Verify the standalone wheel and the saved-result check on a Linux Vast lab."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CHECKOUT = Path(__file__).resolve().parents[2]
EXAMPLE = Path("examples/check-support")
NETWORK_SOCKET = re.compile(r"\bsocket\((?:AF_INET6?|AF_PACKET)\b")
MARKERS = ("FORGE_SYNTHETIC_CREDENTIAL", "FORGE_SYNTHETIC_PRIVATE_TICKET", "19004721")
CI_STEP = """\
mkdir -p artifacts
"${RUNNER_TEMP}/forge-check/bin/forge" check \\
  --cases evaluation/cases.json \\
  --baseline evaluation/baseline.json \\
  --candidate evaluation/candidate.json \\
  --output artifacts/forge-check.json
"""


class VerificationError(Exception):
    """A lab prerequisite or acceptance criterion was not satisfied."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify_minimized(*documents: str) -> None:
    for marker in MARKERS:
        require(all(marker not in value for value in documents), "Synthetic marker was exported")


class Lab:
    def __init__(self, work: Path, output: Path, strace: str) -> None:
        self.work = work
        self.output = output
        self.strace = strace
        self.venv = work / "forge-check"
        self.python = str(self.venv / "bin/python")
        self.forge = str(self.venv / "bin/forge")
        self.env = {
            "HOME": str(work / "home"),
            "PATH": f"{self.venv / 'bin'}:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONNOUSERSITE": "1",
            "RUNNER_TEMP": str(work),
        }
        (work / "home").mkdir()
        shutil.copytree(CHECKOUT / EXAMPLE, work / EXAMPLE)

    def run(
        self, name: str, command: list[str], *, trace: bool = False, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        if trace:
            command = [
                self.strace,
                "-f",
                "-qq",
                "-s",
                "0",
                "-e",
                "trace=%network",
                "-e",
                "inject=socket:error=EACCES",
                "-o",
                str(self.output / f"{name}.network.log"),
                *command,
            ]
        with subprocess.Popen(
            command,
            cwd=self.work,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                (self.output / f"{name}.stdout").write_text(stdout, encoding="utf-8")
                (self.output / f"{name}.stderr").write_text(stderr, encoding="utf-8")
                raise VerificationError(f"{name} timed out") from None
        (self.output / f"{name}.stdout").write_text(stdout, encoding="utf-8")
        (self.output / f"{name}.stderr").write_text(stderr, encoding="utf-8")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def traced_network(self, name: str) -> str:
        return (self.output / f"{name}.network.log").read_text(encoding="utf-8")

    def install(self, wheel: Path) -> dict[str, Any]:
        require(
            self.run("venv", [sys.executable, "-m", "venv", str(self.venv)]).returncode == 0,
            "Could not create an isolated Python environment",
        )
        start = time.monotonic()
        installed = self.run(
            "install",
            [
                self.python,
                "-m",
                "pip",
                "--disable-pip-version-check",
                "install",
                "--no-index",
                "--no-deps",
                "--no-cache-dir",
                str(wheel),
            ],
            timeout=60,
        )
        install_seconds = time.monotonic() - start
        require(installed.returncode == 0, "Offline candidate wheel install failed")
        inspected = self.run(
            "dependencies",
            [
                self.python,
                "-c",
                "import importlib.metadata as m, json; "
                "d = m.distribution('forge-os-check'); "
                "print(json.dumps({'installed': {i.metadata['Name']: i.version "
                "for i in m.distributions()}, 'runtime_requires': d.requires or [], "
                "'location': str(d.locate_file(''))}))",
            ],
        )
        require(inspected.returncode == 0, "Could not inspect installed wheel metadata")
        metadata = json.loads(inspected.stdout)
        extras = {'extra == "code-mode"', "extra == 'code-mode'"}
        metadata["base_requires"] = [
            requirement
            for requirement in metadata["runtime_requires"]
            if requirement.partition(";")[2].strip() not in extras
        ]
        require(not metadata["base_requires"], "Base wheel declares runtime requirements")
        require(
            set(metadata["installed"]) <= {"forge-os-check", "pip", "setuptools"},
            "Unexpected distribution in the clean environment",
        )
        require(
            Path(metadata["location"]).is_relative_to(self.venv), "Wheel import is not isolated"
        )
        return {"install_seconds": round(install_seconds, 4), **metadata}

    def verify_observer(self) -> str:
        version = self.run("strace-version", [self.strace, "--version"])
        require(version.returncode == 0, "Could not identify the network observer")
        probe = self.run(
            "observer-probe", [self.python, "-c", "import socket; socket.socket()"], trace=True
        )
        trace = self.traced_network("observer-probe")
        require(
            probe.returncode != 0
            and "PermissionError" in probe.stderr
            and "INJECTED" in trace
            and len(NETWORK_SOCKET.findall(trace)) == 1,
            "strace did not observe and block the positive-control socket",
        )
        return version.stdout.splitlines()[0]

    def check(
        self,
        name: str,
        *,
        candidate: Path,
        cases: Path = EXAMPLE / "cases.json",
        baseline: Path | None = EXAMPLE / "baseline.json",
        reference: Path | None = None,
        verdict: str,
        exit_code: int,
        ci_step: bool = False,
    ) -> tuple[dict[str, Any], str]:
        output = self.output / f"{name}.json"
        command = [
            self.forge,
            "check",
            "--cases",
            str(cases),
            "--candidate",
            str(candidate),
            "--output",
            str(output),
        ]
        if baseline is not None:
            command.extend(["--baseline", str(baseline)])
        if reference is not None:
            command.extend(["--save-reference", str(reference)])
        if ci_step:
            require(baseline is not None, "The documented CI step needs a baseline")
            (self.work / "evaluation").mkdir(exist_ok=True)
            (self.work / "artifacts/forge-check.json").unlink(missing_ok=True)
            shutil.copyfile(self.work / cases, self.work / "evaluation/cases.json")
            shutil.copyfile(self.work / baseline, self.work / "evaluation/baseline.json")
            shutil.copyfile(self.work / candidate, self.work / "evaluation/candidate.json")
            command = ["/bin/bash", "-e", "-o", "pipefail", "-c", CI_STEP]
        result = self.run(name, command, trace=True)
        require(
            not NETWORK_SOCKET.search(self.traced_network(name)), f"{name} attempted networking"
        )
        require(
            result.returncode == exit_code,
            f"{name} returned {result.returncode}, expected {exit_code}",
        )
        if ci_step:
            shutil.copyfile(self.work / "artifacts/forge-check.json", output)
        verify_minimized(result.stdout, result.stderr, output.read_text(encoding="utf-8"))
        report = read_json(output)
        require(report["verdict"] == verdict, f"{name} has the wrong report verdict")
        require(report["exit_code"] == exit_code, f"{name} has the wrong report exit code")
        summary = result.stdout + result.stderr
        require(
            bool(re.search(rf"\b{verdict}\b", summary, re.IGNORECASE)),
            f"{name} summary has no verdict",
        )
        return report, summary


def verify_pass_fail(report: dict[str, Any], fail_ids: set[str]) -> None:
    rows = report["cases"]
    expected_ids = {f"S{number:02d}" for number in range(1, 25)}
    require(
        report["coverage"]["eligible"] == 24
        and len(rows) == 24
        and {row["id"] for row in rows} == expected_ids,
        "Report must account for each of the 24 assigned tickets once",
    )
    require(
        {row["id"] for row in rows if row["result"] == "fail"} == fail_ids, "Wrong failing tickets"
    )


def verify_demo(lab: Lab) -> None:
    report, summary = lab.check(
        "demo-fail", candidate=EXAMPLE / "candidate-fail.json", verdict="fail", exit_code=1
    )
    verify_pass_fail(report, {"S07", "S08"})
    require(
        all(value in summary for value in ("24", "S07", "S08")),
        "Demo summary omits ticket IDs or coverage",
    )
    for row in report["cases"]:
        if row["id"] in {"S07", "S08"}:
            require(
                row["expected"] == "human"
                and row["candidate"] == {"observed": "self_service", "status": "completed"},
                f"{row['id']} lacks evidence from the ticket queue",
            )


def verify_outcomes(lab: Lab) -> None:
    candidate = EXAMPLE / "candidate-pass.json"
    report, _ = lab.check("candidate-pass", candidate=candidate, verdict="pass", exit_code=0)
    verify_pass_fail(report, set())
    require(report["coverage"]["comparable"] == 24, "The passing comparison lacks evidence")
    ci_report, _ = lab.check(
        "candidate-pass-ci", candidate=candidate, verdict="pass", exit_code=0, ci_step=True
    )
    require(ci_report == report, "The documented CI step changed the passing result")
    reference = lab.output / "pending-reference.json"
    candidate_with_metadata = read_json(lab.work / candidate)
    candidate_with_metadata["credential"] = MARKERS[0]
    candidate_with_metadata["cases"][0]["ticket_body"] = MARKERS[1]
    write_json(lab.work / "first-candidate.json", candidate_with_metadata)
    initial, _ = lab.check(
        "first-run",
        candidate=Path("first-candidate.json"),
        baseline=None,
        reference=reference,
        verdict="insufficient",
        exit_code=2,
    )
    verify_pass_fail(initial, set())
    require(
        initial["coverage"]["baseline_observed"] == 0 and initial["coverage"]["comparable"] == 0,
        "A first run must lack baseline coverage",
    )
    verify_minimized(reference.read_text(encoding="utf-8"))
    require(
        read_json(reference)["reference_status"] == "pending_review", "Missing pending reference"
    )
    lab.check(
        "first-run-violation",
        candidate=EXAMPLE / "candidate-fail.json",
        baseline=None,
        verdict="fail",
        exit_code=1,
    )
    compared, _ = lab.check(
        "reviewed-reference",
        candidate=EXAMPLE / "baseline.json",
        baseline=reference,
        verdict="pass",
        exit_code=0,
    )
    require(compared["coverage"]["comparable"] == 24, "The reviewed reference cannot be compared")
    recorded = reference.read_bytes()
    lab.check(
        "existing-reference",
        candidate=Path("first-candidate.json"),
        baseline=None,
        reference=reference,
        verdict="invalid",
        exit_code=3,
    )
    require(reference.read_bytes() == recorded, "A repeated first run replaced the reference")


def verify_case_integrity(lab: Lab) -> None:
    candidate = read_json(lab.work / EXAMPLE / "candidate-fail.json")
    candidate["cases"][7] = {
        "id": "S08",
        "status": "error",
        "observed": None,
        "error_message": MARKERS[1],
    }
    candidate["cases"].pop(8)
    candidate["usage"] = {"total_tokens": 19004721}
    candidate["credential"] = MARKERS[0]
    write_json(lab.work / "partial-candidate.json", candidate)
    direct, _ = lab.check(
        "partial-direct", candidate=Path("partial-candidate.json"), verdict="fail", exit_code=1
    )
    verify_pass_fail(direct, {"S07"})
    by_id = {row["id"]: row for row in direct["cases"]}
    require(by_id["S08"]["candidate"]["status"] == "error", "Error status was lost")
    require(by_id["S09"]["candidate"]["status"] == "missing", "Omission was lost")
    require(
        direct["coverage"]["candidate_observed"] == 22
        and by_id["S08"]["result"] == by_id["S09"]["result"] == "insufficient",
        "Unavailable evidence changed coverage or was treated as a correct result",
    )
    cases = read_json(lab.work / EXAMPLE / "cases.json")
    baseline = read_json(lab.work / EXAMPLE / "baseline.json")
    for filename, document in (
        ("reordered-cases.json", cases),
        ("reordered-baseline.json", baseline),
        ("reordered-candidate.json", candidate),
    ):
        document["cases"].reverse()
        write_json(lab.work / filename, document)
    ci, _ = lab.check(
        "partial-reordered-ci",
        cases=Path("reordered-cases.json"),
        baseline=Path("reordered-baseline.json"),
        candidate=Path("reordered-candidate.json"),
        verdict="fail",
        exit_code=1,
        ci_step=True,
    )
    require(ci == direct, "Case order or the CI entry point changed the exported report")
    effects = read_json(lab.work / EXAMPLE / "candidate-pass.json")
    effects["cases"][0]["status"] = "error"
    write_json(lab.work / "lost-ack.json", effects)
    uncertain, _ = lab.check(
        "lost-ack",
        candidate=Path("lost-ack.json"),
        verdict="insufficient",
        exit_code=2,
        ci_step=True,
    )
    require(
        uncertain["coverage"]["candidate_observed"] == 24
        and uncertain["coverage"]["comparable"] == 23,
        "Independent evidence was lost or the errored run counted as completed",
    )
    effects["cases"][1]["observed"] = "duplicate_human"
    effects["cases"][9]["observed"] = "human"
    write_json(lab.work / "unintended-effects.json", effects)
    violations, _ = lab.check(
        "unintended-effects", candidate=Path("unintended-effects.json"), verdict="fail", exit_code=1
    )
    verify_pass_fail(violations, {"S02", "S10"})
    candidate["cases"][0]["observed"] = f"private text {MARKERS[1]}"
    write_json(lab.work / "invalid-candidate.json", candidate)
    lab.check("invalid", candidate=Path("invalid-candidate.json"), verdict="invalid", exit_code=3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    lab_id = os.environ.get("FORGE_VAST_LAB_ID", "")
    if sys.platform != "linux" or not re.fullmatch(r"[0-9]+", lab_id):
        parser.error("run on the verified Linux Vast instance with FORGE_VAST_LAB_ID set")
    strace = shutil.which("strace")
    if strace is None:
        parser.error("install strace in the lab for independent network observation")
    wheel = args.wheel.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    evidence: dict[str, Any] = {
        "lab_id": lab_id,
        "python": sys.version,
        "wheel": wheel.name,
        "tests": dict.fromkeys(("P01", "P02", "P03"), "pending"),
    }
    attempt = "P01"
    try:
        evidence["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
        evidence["wheel_bytes"] = wheel.stat().st_size
        with tempfile.TemporaryDirectory(prefix="forge-p01-p03-") as work:
            lab = Lab(Path(work), output, strace)
            evidence["installation"] = lab.install(wheel)
            evidence["network_observer"] = lab.verify_observer()
            verify_demo(lab)
            evidence["network_socket_attempts"] = 0
            evidence["tests"]["P01"] = "pass"
            attempt = "P02"
            verify_outcomes(lab)
            evidence["tests"]["P02"] = "pass"
            attempt = "P03"
            verify_case_integrity(lab)
            evidence["tests"]["P03"] = "pass"
    except (VerificationError, OSError, ValueError, KeyError, TypeError) as error:
        evidence["tests"][attempt] = "fail"
        evidence["failure"] = str(error)
    write_json(output / "acceptance.json", evidence)
    print(json.dumps({"tests": evidence["tests"], "failure": evidence.get("failure")}))
    return int("failure" in evidence)


if __name__ == "__main__":
    raise SystemExit(main())
