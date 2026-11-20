"""Prove isolated read-binding parity and limits on an identified Vast VM."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any

CHECKOUT = Path(__file__).resolve().parents[2]
MARKER = "FORGE_SYNTHETIC_PRIVATE_TICKET_FIELD"
_HARNESS_FILES = ("p14_p15.py", "governed_oracle.py", "governed_trials.py")
_PROGRAM = """return input.tickets.map(ticket_id => tools['support.lookup']({ticket_id}));"""


def _trial(report: dict[str, Any]) -> None:
    from forge_check.governed.code_mode import CodeLimits, CodeMode
    from forge_check.governed.control import Control
    from forge_check.governed.db import Store
    from forge_check.governed.host import Host, HttpProvider
    from forge_check.governed.revision import Revision
    from governed_oracle import TOKEN, Behavior, Oracle
    from governed_trials import (
        ACTORS,
        FINGERPRINT,
        MODEL,
        OPERATOR,
        SCOPE,
        TICKET,
        manifest,
        request,
        require,
    )

    class ToolOracle(Oracle):
        def acknowledge(self, packet: dict) -> tuple[Behavior, dict, str]:
            behavior, observation, ticket_id = super().acknowledge(packet)
            observation["result"]["reserved_detail"] = MARKER
            return behavior, observation, ticket_id

    def profile(
        name: str,
        provider: ToolOracle,
        *,
        allowed: bool = True,
        grant: bool = True,
    ) -> tuple[Control, Host, CodeMode]:
        store = Store(Path(f"{name}.sqlite3"))
        control = Control(store)
        principals = copy.deepcopy(ACTORS)
        if not grant:
            principals[OPERATOR]["bindings"] = [MODEL]
        cases = json.loads(Path("support/cases.json").read_text(encoding="utf-8"))
        revision = Revision.parse(
            manifest(budget=50_000, allowed=(MODEL, TICKET) if allowed else (MODEL,))
        )
        control.bootstrap(SCOPE, revision, cases, principals)
        host = Host(
            store, fingerprint_key=FINGERPRINT, provider=HttpProvider(provider.origin, token=TOKEN)
        )
        return control, host, CodeMode(host, enabled=True, bindings=(TICKET, MODEL))

    def task(control: Control, name: str, *, source: str = "code_mode") -> None:
        control.start_task(OPERATOR, name, worker="worker-a", source=source)

    with ToolOracle() as provider:
        controls: list[dict[str, object]] = []
        for name, allowed, grant, expected in (
            ("permitted", True, True, None),
            ("revision-policy", False, True, "policy_denied"),
            ("actor-policy", True, False, "principal_denied"),
        ):
            control, host, executor = profile(name, provider, allowed=allowed, grant=grant)
            ticket = f"test.{name}"
            task(control, "direct", source="direct")
            task(control, "program")
            direct = host.invoke(request("direct", "direct-first", ticket))
            second = host.invoke(request("direct", "direct-second", ticket))
            outcome = executor.run(
                OPERATOR, "program", "parity", _PROGRAM, inputs={"tickets": [ticket, ticket]}
            )
            require(outcome.status == "completed", f"Program did not finish: {outcome.reason}")
            require(len(outcome.attempt_ids) == 2, "Program did not record both logical subcalls")
            observed = [host.ledger.attempt(attempt) for attempt in outcome.attempt_ids]
            for left, right in zip((direct.attempt, second.attempt), observed, strict=True):
                keys = (
                    "status",
                    "reason",
                    "revision",
                    "binding_id",
                    "cap_microusd",
                    "charge_microusd",
                )
                require(all(left[key] == right[key] for key in keys), "Admission parity failed")
                require(right["source"] == "code_mode", "Program route lost its source")
            require(observed[0]["reason"] == expected, "Unexpected admission decision")
            require(
                observed[1]["reason"] == (expected or "budget_exceeded"),
                "Subcalls did not share the root balance",
            )
            require(
                host.ledger.balance("direct") == host.ledger.balance("program"),
                "Route-dependent balance",
            )
            require(
                len(provider.dispatches(ticket)) == (2 if allowed and grant else 0),
                "Wrong provider count",
            )
            if allowed and grant:
                for route in ("direct", "program"):
                    require(
                        any(
                            row["reserved_microusd"] == 50_000
                            for row in host.ledger.receipts(route)
                        ),
                        "Expected a durable pre-dispatch hold",
                    )
                require(
                    outcome.output[0]["result"] == {"ticket_id": ticket, "label": "human"},
                    "Unsafe result",
                )
            controls.append(
                {
                    "path": name,
                    "outcome": asdict(outcome),
                    "balance": host.ledger.balance("program"),
                }
            )
        require(MARKER not in json.dumps(controls), "Raw tool output escaped its data boundary")
        report["P14"] = {"controls": controls}
        report["tests"]["P14"] = "pass"

        control, host, executor = profile("limits", provider)
        task(control, "disabled")
        disabled = CodeMode(host).run(OPERATOR, "disabled", "disabled", "return 1;")
        require(disabled.reason == "disabled", "Optional path must be off by default")
        task(control, "direct-after-disable", source="direct")
        direct = host.invoke(
            request("direct-after-disable", "direct-after-disable", "test.disable")
        )
        require(direct.attempt["status"] == "succeeded", "Opt-out changed the direct path")

        def program(name: str, source: str, limits: CodeLimits | None = None) -> dict[str, Any]:
            task(control, name)
            started = time.monotonic()
            result = executor.run(OPERATOR, name, name, source, limits=limits)
            return {**asdict(result), "wall_seconds": round(time.monotonic() - started, 4)}

        os.environ["FORGE_TEST_PRIVATE"] = MARKER
        ambient = program(
            "ambient",
            """return {
                Runtime: typeof Runtime, process: typeof process, fetch: typeof fetch,
                require: typeof require, fs: typeof fs, std: typeof std, os: typeof os,
                model: typeof tools['support.model'], env: typeof __forge_rpc
            };""",
        )
        require(
            ambient["status"] == "completed" and set(ambient["output"].values()) == {"undefined"},
            "Unapproved ambient capability",
        )
        unsupported = program("unavailable", "return tools['support.model']({});")
        require(
            unsupported["status"] == "aborted" and unsupported["calls"] == 0, "Unbound method ran"
        )
        error = program("error", f"throw new Error({json.dumps(MARKER)});")
        require(error["reason"] == "program_error", "Untrusted exception was not sanitized")
        wall = program("wall", "while(true) {}", CodeLimits(wall_seconds=2, cpu_seconds=10))
        require(wall["reason"] == "wall_limit" and wall["wall_seconds"] < 4, "Wall guard failed")
        cpu = program("cpu", "while(true) {}", CodeLimits(wall_seconds=12, cpu_seconds=1))
        require(cpu["reason"] == "cpu_limit" and cpu["wall_seconds"] < 8, "CPU guard failed")
        memory = program(
            "memory",
            "const a=[]; while(true) a.push(new Array(4096).fill(a));",
            CodeLimits(memory_bytes=1024 * 1024),
        )
        require(memory["reason"] == "memory_limit", "Heap guard failed")
        output = program("output", "return 'x'.repeat(8192);", CodeLimits(output_bytes=64))
        require(
            output["reason"] == "output_limit" and output["output"] is None, "Output guard failed"
        )
        calls = program(
            "calls",
            "tools['support.lookup']({ticket_id:'test.limit.first'});"
            "return tools['support.lookup']({ticket_id:'test.limit.second'});",
            CodeLimits(max_calls=1),
        )
        require(
            calls["reason"] == "call_limit" and len(calls["attempt_ids"]) == 1, "Call cap failed"
        )
        require(len(provider.dispatches("test.limit.first")) == 1, "First call was not durable")
        require(
            not provider.dispatches("test.limit.second"), "An over-limit call reached the provider"
        )
        require(
            host.ledger.balance("calls")["consumed_microusd"] == 13_000, "Abort lost known charge"
        )
        provider.arrange("test.lost", Behavior(mode="drop"))
        lost = program("lost", "return tools['support.lookup']({ticket_id:'test.lost'});")
        require(lost["reason"] == "subcall_unresolved", "Lost response was accepted")
        balance = host.ledger.balance("lost")
        replay = executor.run(
            OPERATOR, "lost", "lost", "return tools['support.lookup']({ticket_id:'test.lost'});"
        )
        require(replay.status == "replay", "Program ID did not block retry")
        require(len(provider.dispatches("test.lost")) == 1, "Replay repeated an ambiguous effect")
        require(
            host.ledger.balance("lost") == balance and balance["pending_microusd"] == 50_000,
            "Lost hold",
        )

        report["P15"] = {
            "limits": [wall, cpu, memory, output, calls],
            "ambient": ambient,
            "unsupported": unsupported,
            "lost": lost,
            "pending": balance,
            "replay": asdict(replay),
            "program_error": error,
            "disabled": asdict(disabled),
        }
        artifact = json.dumps(report)
        require(
            MARKER not in artifact and TOKEN not in artifact, "Reserved marker leaked to evidence"
        )
        report["tests"]["P15"] = "pass"
        report["provider_dispatch_count"] = len(provider.dispatches())


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _child(destination: Path) -> int:
    report: dict[str, Any] = {
        "tests": {"P14": "pending", "P15": "pending"},
        "quickjs_rs": version("quickjs-rs"),
        "wasmtime": version("wasmtime"),
    }
    try:
        _trial(report)
    except (AssertionError, OSError, RuntimeError, ValueError) as exc:
        report["failure"] = str(exc)
    _write(destination, report)
    return int(any(state != "pass" for state in report["tests"].values()))


def _run(wheel: Path, wheelhouse: Path, target: Path) -> int:
    result: dict[str, Any] = {"wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix="forge-code-mode-") as temporary:
        working = Path(temporary)
        venv = working / "venv"
        environment = {
            "FORGE_VAST_LAB_ID": os.environ["FORGE_VAST_LAB_ID"],
            "HOME": temporary,
            "LANG": "C.UTF-8",
            "PATH": f"{venv / 'bin'}:/usr/bin:/bin",
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONNOUSERSITE": "1",
        }
        for name in _HARNESS_FILES:
            shutil.copyfile(Path(__file__).with_name(name), working / name)
        shutil.copytree(CHECKOUT / "examples/check-support", working / "support")
        python = str(venv / "bin/python")
        commands = (
            ("venv", [sys.executable, "-m", "venv", str(venv)]),
            (
                "install",
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--only-binary=:all:",
                    "--find-links",
                    str(wheelhouse),
                    f"forge-os-check[code-mode] @ {wheel.as_uri()}",
                ],
            ),
            (
                "trials",
                [python, str(working / "p14_p15.py"), "--child", str(target / "trials.json")],
            ),
        )
        for label, command in commands:
            completed = subprocess.run(
                command, cwd=working, env=environment, capture_output=True, timeout=180
            )
            (target / f"{label}.stdout").write_bytes(completed.stdout)
            (target / f"{label}.stderr").write_bytes(completed.stderr)
            result[f"{label}_status"] = completed.returncode
            if completed.returncode:
                break
        if (trials := target / "trials.json").is_file():
            result.update(json.loads(trials.read_text(encoding="utf-8")))
        _write(target / "acceptance.json", result)
        return completed.returncode


def main() -> int:
    if sys.platform != "linux" or not re.fullmatch(
        r"[0-9]+", os.environ.get("FORGE_VAST_LAB_ID", "")
    ):
        raise SystemExit("Run only on the identified Vast Linux instance")
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        return _child(Path(sys.argv[2]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    target = args.output_dir.resolve()
    target.mkdir(parents=True, exist_ok=False)
    return _run(args.wheel.resolve(), args.wheelhouse.resolve(), target)


if __name__ == "__main__":
    raise SystemExit(main())
