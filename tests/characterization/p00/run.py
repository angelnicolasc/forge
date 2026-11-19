"""Characterize the Forge 0.2.0 LangGraph entry points on an identified Vast VM."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

AUDITED_SHA = "7b8ea1e4a081cb41ead499029492e2ee5b286be3"
FORGE_MODULES = {
    "forge-os": "forge_os",
    "forge-os-adapters": "forge_adapters",
    "forge-os-cli": "forge_cli",
    "forge-os-core": "forge_core",
    "forge-os-memory": "forge_memory",
    "forge-os-observe": "forge_observe",
    "forge-review": "forge_review",
}
TICKETS = {"P00-S01": True, "P00-S02": False}
ESCALATION_PATH = "/tickets/P00-S01/escalation"
EVIDENCE_FILES = ("flow.py", "probe.py", "run.py")


class TicketDestination(HTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), TicketHandler)
        self.observations: list[dict[str, Any]] = []


class TicketHandler(BaseHTTPRequestHandler):
    server: TicketDestination

    def do_GET(self) -> None:
        status = 204 if self.path == ESCALATION_PATH else 404
        self.server.observations.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "method": "GET",
                "path": self.path,
                "status": status,
            }
        )
        self.send_response(status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # The destination keeps its own structured journal in the parent process.
        pass


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def child_environment(work_dir: Path, lab_id: str) -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": str(work_dir),
        "LANG": "C.UTF-8",
        "COLUMNS": "160",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "PYTHONNOUSERSITE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OTEL_SDK_DISABLED": "true",
        "FORGE_VAST_LAB_ID": lab_id,
    }


def inventory(profile: str, checkout: Path | None) -> tuple[dict[str, Any], list[str]]:
    issues = []
    checkout_sha = None
    if checkout is not None:
        checkout_sha = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True, timeout=10
        ).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"],
            text=True,
            timeout=10,
        ).strip()
        if checkout_sha != AUDITED_SHA or dirty:
            issues.append("Checkout must be the clean, audited baseline SHA")

    site_packages = Path(sysconfig.get_path("purelib")).resolve()
    package_locations = {}
    packages = {dist.metadata["Name"]: dist for dist in importlib.metadata.distributions()}
    for name, module in FORGE_MODULES.items():
        spec = importlib.util.find_spec(module)
        dist = packages.get(name)
        if spec is None or spec.origin is None or dist is None:
            issues.append(f"Missing {name} distribution or {module} import")
            continue
        origin = Path(spec.origin).resolve()
        direct_url = dist.read_text("direct_url.json")
        wheel = dist.read_text("WHEEL")
        package_locations[name] = {"module": str(origin), "direct_url": direct_url, "wheel": wheel}
        if dist.version != "0.2.0":
            issues.append(f"Expected {name} 0.2.0, found {dist.version}")
        if profile == "public-wheel" and (
            not origin.is_relative_to(site_packages)
            or not wheel
            or (direct_url and json.loads(direct_url).get("dir_info"))
        ):
            issues.append(f"Expected {name} packaged under the public wheel environment")
        if checkout is not None and not origin.is_relative_to(checkout):
            issues.append(f"Expected {name} import under the audited checkout")

    metadata = {
        "profile": profile,
        "checkout_sha": checkout_sha,
        "python": sys.version,
        "platform": platform.platform(),
        "executable": sys.executable,
        "locations": package_locations,
        "dependencies": {
            name: {"version": dist.version, "requires_dist": list(dist.requires or [])}
            for name, dist in packages.items()
        },
        "adapter_entry_points": [
            f"{ep.name}={ep.value}"
            for ep in importlib.metadata.entry_points(group="forge.adapters")
        ],
    }
    return metadata, issues


def run_route(
    name: str,
    command: list[str],
    work_dir: Path,
    environment: dict[str, str],
    output: Path,
    destination: TicketDestination,
) -> tuple[dict[str, Any], str]:
    start = time.monotonic()
    first_receipt = len(destination.observations)
    failure = None
    code = None
    try:
        process = subprocess.run(
            command, cwd=work_dir, env=environment, capture_output=True, timeout=30
        )
        stdout, stderr, code = process.stdout, process.stderr, process.returncode
    except subprocess.TimeoutExpired as error:
        stdout, stderr = error.stdout or b"", error.stderr or b""
        failure = "timeout after 30 seconds"
    except OSError as error:
        stdout, stderr = b"", b""
        failure = str(error)
    (output / f"{name}.stdout").write_bytes(stdout)
    (output / f"{name}.stderr").write_bytes(stderr)
    result_path = output / f"{name}.json"
    observation = {
        "command": command,
        "exit_code": code,
        "duration_seconds": round(time.monotonic() - start, 3),
        "invocation_error": failure,
        "stdout": f"{name}.stdout",
        "stderr": f"{name}.stderr",
        "structured_output": result_path.name if result_path.exists() else None,
        "destination_observations": destination.observations[first_receipt:],
    }
    return observation, (stdout + stderr).decode("utf-8", errors="replace")


def result_for(output: Path, observation: dict[str, Any]) -> dict[str, Any]:
    name = observation["structured_output"]
    return json.loads((output / name).read_text(encoding="utf-8")) if name else {}


def destination_matches(observation: dict[str, Any], should_escalate: bool) -> bool:
    expected = [("GET", ESCALATION_PATH, 204)] if should_escalate else []
    return [
        (row["method"], row["path"], row["status"])
        for row in observation["destination_observations"]
    ] == expected


def sdk_matches(result: dict[str, Any], ticket: str, should_escalate: bool) -> bool:
    output = result.get("output")
    return (
        result.get("status") == "completed"
        and isinstance(output, dict)
        and output.get("ticket_id") == ticket
        and output.get("requires_escalation") is should_escalate
        and output.get("observed") is should_escalate
    )


def characterize(
    work_dir: Path, output: Path, lab_id: str, destination: TicketDestination
) -> tuple[dict[str, dict[str, Any]], dict[str, bool]]:
    environment = child_environment(work_dir, lab_id)
    routes = {}
    checks = {}
    endpoint = f"http://127.0.0.1:{destination.server_port}"
    payloads = {
        ticket: json.dumps({"ticket_id": ticket, "destination": endpoint}) for ticket in TICKETS
    }
    for ticket, should_escalate in TICKETS.items():
        for route in ("native", "adapter"):
            name = f"{route}-{ticket}"
            observation, _ = run_route(
                name,
                [sys.executable, "probe.py", route, payloads[ticket], str(output / f"{name}.json")],
                work_dir,
                environment,
                output,
                destination,
            )
            routes[name] = observation
            checks[name] = (
                observation["exit_code"] == 0
                and sdk_matches(result_for(output, observation), ticket, should_escalate)
                and destination_matches(observation, should_escalate)
            )

    forge = str(Path(sys.executable).parent / "forge")
    cli_options = ["--input", payloads["P00-S01"], "--no-memory", "--verbose"]
    cli_routes = (
        ("cli-source-first", ["flow.py", *cli_options]),
        ("cli-options-first", [*cli_options, "flow.py"]),
    )
    for name, args in cli_routes:
        observation, logs = run_route(
            name, [forge, "wrap", *args], work_dir, environment, output, destination
        )
        cli_completed = observation["exit_code"] == 0 and destination_matches(observation, True)
        parser_gap = (
            name == "cli-source-first"
            and observation["exit_code"] == 2
            and "Missing argument 'SOURCE'" in logs
            and destination_matches(observation, False)
        )
        discovery_gap = (
            observation["exit_code"] == 1
            and "No adapter found" in logs
            and destination_matches(observation, False)
        )
        if cli_completed:
            disposition = "completed"
        elif parser_gap:
            disposition = "source_argument_rejected"
        elif discovery_gap:
            disposition = "adapter_not_discovered"
        else:
            disposition = "unexpected"
        observation["disposition"] = disposition
        routes[name] = observation
        checks[name] = disposition != "unexpected"

    name = "mutation"
    mutation, _ = run_route(
        name,
        [sys.executable, "probe.py", name, payloads["P00-S01"], str(output / "mutation.json")],
        work_dir,
        environment,
        output,
        destination,
    )
    routes[name] = mutation
    result = result_for(output, mutation)
    applied = result.get("applied_mutation") or {}
    checks["review_separate_from_evolution"] = (
        mutation["exit_code"] == 0
        and result.get("control_statuses") == ["completed", "completed"]
        and result.get("standalone_review", {}).get("allowed") is False
        and result.get("loop_pre_apply_calls") == 0
        and applied.get("kind") == "agent_cull"
    )
    before, after = result.get("topology_versions", [None, None])
    checks["topology_differs_from_dispatch"] = (
        mutation["exit_code"] == 0
        and result.get("topology_before") == ["classify", "escalation_lookup"]
        and result.get("topology_after") == ["classify"]
        and isinstance(before, int)
        and after == before + 1
        and sdk_matches(result.get("subsequent_run", {}), "P00-S01", True)
        and destination_matches(mutation, True)
    )
    return routes, checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("public-wheel", "checkout"), required=True)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lab_id = os.environ.get("FORGE_VAST_LAB_ID", "")
    if sys.platform != "linux" or not re.fullmatch(r"[0-9]+", lab_id):
        parser.error("Use the verified Vast Linux VM and its FORGE_VAST_LAB_ID")
    if (args.profile == "checkout") != (args.checkout is not None):
        parser.error("--profile checkout requires --checkout, which is forbidden for public-wheel")
    if os.environ.get("PYTHONPATH") or sys.prefix == sys.base_prefix:
        parser.error("Use a fresh virtual environment without PYTHONPATH")

    output = args.output.resolve()
    output.mkdir(parents=True)
    checkout = args.checkout.resolve() if args.checkout else None
    fixture = Path(__file__).resolve().parent
    started_at = datetime.now(UTC).isoformat()
    packages, issues = inventory(args.profile, checkout)
    write_json(output / "environment.json", packages)
    routes = {}
    checks = {}
    with tempfile.TemporaryDirectory(prefix="forge-p00-") as directory:
        work_dir = Path(directory).resolve()
        if work_dir.is_relative_to(fixture.parents[2]) or (
            checkout and work_dir.is_relative_to(checkout)
        ):
            issues.append("Temporary working directory must be outside both checkouts")
        if not issues:
            for file in ("flow.py", "probe.py"):
                shutil.copyfile(fixture / file, work_dir / file)
            with TicketDestination() as destination:
                worker = threading.Thread(
                    target=destination.serve_forever, kwargs={"poll_interval": 0.1}
                )
                worker.start()
                try:
                    routes, checks = characterize(work_dir, output, lab_id, destination)
                finally:
                    destination.shutdown()
                    worker.join()
    passed = not issues and bool(checks) and all(checks.values())
    write_json(
        output / "p00.json",
        {
            "trial": "P00",
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "vast_instance_id": lab_id,
            "profile": args.profile,
            "status": "characterized" if passed else "unexpected",
            "synthetic_expected_lookup": TICKETS,
            "files_sha256": {
                name: hashlib.sha256((fixture / name).read_bytes()).hexdigest()
                for name in EVIDENCE_FILES
            },
            "environment": "environment.json",
            "provenance_errors": issues,
            "routes": routes,
            "checks": checks,
        },
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
