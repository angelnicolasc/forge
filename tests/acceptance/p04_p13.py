"""Exercise governed binding and release behavior against an installed wheel."""

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
import time
from pathlib import Path
from typing import Any

CHECKOUT = Path(__file__).resolve().parents[2]
TRIALS = tuple(f"P{number:02}" for number in range(4, 14))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8")


def record_process(output: Path, name: str, process: subprocess.CompletedProcess[str]) -> None:
    (output / f"{name}.stdout").write_text(process.stdout, encoding="utf-8")
    (output / f"{name}.stderr").write_text(process.stderr, encoding="utf-8")
    (output / f"{name}.status").write_text(f"{process.returncode}\n", encoding="ascii")


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    output: Path,
    name: str,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )
    record_process(output, name, result)
    return result


def install_and_probe(
    wheel: Path, output: Path, lab_id: str, *, driver: str = "governed_trials.py"
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="forge-p04-p13-") as temporary:
        work = Path(temporary)
        virtualenv = work / "venv"
        environment = {
            "FORGE_VAST_LAB_ID": lab_id,
            "HOME": str(work / "home"),
            "LANG": "C.UTF-8",
            "NO_PROXY": "127.0.0.1",
            "PATH": f"{virtualenv / 'bin'}:/usr/bin:/bin",
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONNOUSERSITE": "1",
        }
        (work / "home").mkdir()
        python = str(virtualenv / "bin/python")
        steps = (
            ("venv", [sys.executable, "-m", "venv", str(virtualenv)]),
            (
                "wheel-install",
                [
                    python,
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
        installation_seconds = 0.0
        for name, command in steps:
            started = time.monotonic()
            result = run(command, cwd=work, env=environment, output=output, name=name)
            if result.returncode != 0:
                raise RuntimeError(f"{name} returned {result.returncode}; see the captured stderr")
            if name == "wheel-install":
                installation_seconds = time.monotonic() - started

        for filename in dict.fromkeys(("governed_oracle.py", "governed_trials.py", driver)):
            shutil.copyfile(Path(__file__).with_name(filename), work / filename)
        shutil.copytree(CHECKOUT / "examples/check-support", work / "support")
        result = run(
            [python, str(work / driver), str(output)],
            cwd=work,
            env=environment,
            output=output,
            name="governed-trials",
            timeout=180,
        )
        details = output / "trials.json"
        if not details.is_file():
            raise RuntimeError(
                f"Governed trial process returned {result.returncode} without evidence"
            )
        data = json.loads(details.read_text(encoding="utf-8"))
        if result.returncode != 0 and "failure" not in data:
            data["failure"] = f"Governed trial process returned {result.returncode}"
        return {"installation_seconds": round(installation_seconds, 4), **data}


def acceptance_main(trials: tuple[str, ...], driver: str, description: str | None) -> int:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    lab_id = os.environ.get("FORGE_VAST_LAB_ID", "")
    if sys.platform != "linux" or not re.fullmatch(r"[0-9]+", lab_id):
        parser.error("Requires Linux and a numeric FORGE_VAST_LAB_ID.")
    destination = args.output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    wheel = args.wheel.resolve()
    evidence: dict[str, Any] = {
        "lab_id": lab_id,
        "python": sys.version,
        "wheel": wheel.name,
        "tests": dict.fromkeys(trials, "pending"),
    }
    try:
        evidence["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
        evidence["wheel_bytes"] = wheel.stat().st_size
        evidence.update(install_and_probe(wheel, destination, lab_id, driver=driver))
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
        evidence["failure"] = str(error)
    write_json(destination / "acceptance.json", evidence)
    print(json.dumps({"tests": evidence["tests"], "failure": evidence.get("failure")}))
    return int(
        "failure" in evidence or any(evidence["tests"].get(trial) != "pass" for trial in trials)
    )


def main() -> int:
    return acceptance_main(TRIALS, "governed_trials.py", __doc__)


if __name__ == "__main__":
    raise SystemExit(main())
