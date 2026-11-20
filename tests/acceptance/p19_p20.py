"""Exercise crash recovery, migration and storage failure on an identified Vast VM."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from p04_p13 import CHECKOUT, run, write_json


def install(
    work: Path,
    wheel: Path,
    environment: dict[str, str],
    output: Path,
    profile: str,
) -> Path:
    virtualenv = work / profile
    interpreter = virtualenv / "bin/python"
    commands = (
        ("venv", [sys.executable, "-m", "venv", str(virtualenv)]),
        (
            "wheel",
            [
                str(interpreter),
                "-m",
                "pip",
                "--disable-pip-version-check",
                "install",
                "--no-index",
                "--no-deps",
                "--no-cache-dir",
                str(wheel),
            ],
        ),
    )
    for name, command in commands:
        result = run(command, cwd=work, env=environment, output=output, name=f"{profile}-{name}")
        if result.returncode:
            raise RuntimeError(f"{profile} {name} failed; inspect its recorded stderr")
    return interpreter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--baseline-wheel", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args()
    instance = os.environ.get("FORGE_VAST_LAB_ID", "")
    if sys.platform != "linux" or not re.fullmatch(r"[0-9]+", instance):
        parser.error("run only on the verified Linux Vast instance")
    output = arguments.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    wheel = arguments.wheel.resolve()
    baseline = arguments.baseline_wheel.resolve()
    evidence = {
        "lab_id": instance,
        "python": sys.version,
        "tests": dict.fromkeys(("P19", "P20"), "pending"),
    }
    try:
        evidence["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
        evidence["baseline_sha256"] = hashlib.sha256(baseline.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="forge-p19-p20-") as directory:
            work = Path(directory)
            home = work / "home"
            home.mkdir()
            environment = {
                "FORGE_VAST_LAB_ID": instance,
                "HOME": str(home),
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "PYTHONNOUSERSITE": "1",
                "PIP_CONFIG_FILE": os.devnull,
            }
            current = install(work, wheel, environment, output, "current")
            previous = install(work, baseline, environment, output, "previous")
            for fixture in ("governed_oracle.py", "governed_trials.py", "recovery_trials.py"):
                shutil.copyfile(Path(__file__).with_name(fixture), work / fixture)
            shutil.copytree(CHECKOUT / "examples/check-support", work / "support")
            process = run(
                [str(current), str(work / "recovery_trials.py"), str(previous), str(output)],
                cwd=work,
                env=environment,
                name="recovery-trials",
                output=output,
                timeout=180,
            )
            outcome = output / "trials.json"
            if outcome.is_file():
                evidence.update(json.loads(outcome.read_text(encoding="utf-8")))
            if process.returncode:
                evidence["failure"] = "Recovery trials failed; see phase evidence"
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        evidence["failure"] = str(error)
    write_json(output / "acceptance.json", evidence)
    print(json.dumps({"tests": evidence["tests"], "failure": evidence.get("failure")}))
    return int(
        "failure" in evidence or any(status != "pass" for status in evidence["tests"].values())
    )


if __name__ == "__main__":
    raise SystemExit(main())
