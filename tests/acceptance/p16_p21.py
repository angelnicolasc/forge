"""Vast-only experiments against an installed wheel and independent support fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median

from forge_check.governed.host import HttpProvider
from forge_check.governed.revision import Revision
from forge_check.governed.verify import verify
from governed_oracle import TOKEN, Oracle
from governed_trials import (
    MODEL,
    OPERATOR,
    OWNER,
    TICKET,
    World,
    dump,
    evaluate,
    manifest,
    now,
    request,
    require,
    utc,
)

# Frozen synthetic fixture criteria. A production SLO requires the pilot owner.
PARALLELISM = 4
TASKS_PER_PHASE = 32
P21_P95_MS = 500.0
P21_BYTES_PER_TASK = 65_536
P16_CASES_PER_FAMILY = 24
P16_SEED = 905204


def _percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def _duration(values: list[float]) -> dict[str, float]:
    return {"p50_ms": round(median(values), 3), "p95_ms": round(_percentile(values, 0.95), 3)}


def _first_process(command: list[str]) -> float:
    start = time.perf_counter()
    subprocess.run(command, check=True, timeout=15, capture_output=True)
    return round((time.perf_counter() - start) * 1000, 3)


@dataclass(frozen=True)
class Case:
    name: str
    first: tuple[str, ...]
    followup: str | None
    expected: dict[str, str]


class RoutingOracle(Oracle):
    """A destination whose necessary second reading varies by case."""

    def __init__(self) -> None:
        super().__init__()
        self.labels: dict[str, str] = {}

    def acknowledge(self, payload: dict) -> tuple:
        behavior, observation, ticket = super().acknowledge(payload)
        observation["result"]["label"] = self.labels[ticket]
        return behavior, observation, ticket


def _cases(family: str, rng: random.Random, oracle: RoutingOracle) -> list[Case]:
    cases = []
    for index in range(P16_CASES_PER_FAMILY):
        name = f"{family}-{index:02}"
        first = tuple(f"{name}-{offset}" for offset in range(4 if family == "batch" else 1))
        labels = (
            ("human", "self_service", "unknown", "review")
            if family == "conditional"
            else (
                "human",
                "self_service",
            )
        )
        for ticket in first:
            oracle.labels[ticket] = rng.choice(labels)
        followup = None if family != "conditional" else f"{name}-detail"
        expected = {ticket: oracle.labels[ticket] for ticket in first}
        if followup is not None:
            oracle.labels[followup] = rng.choice(("human", "self_service", "unknown"))
            if expected[first[0]] == "review":
                expected[followup] = oracle.labels[followup]
        cases.append(Case(name, first, followup, expected))
    return cases


def _read_ticket(world: World, task: str, ticket: str) -> tuple[str, str]:
    result = world.host.invoke(request(task, f"{task}.{ticket}", ticket))
    if result.attempt["confirmation"] != "verified" or result.result is None:
        raise RuntimeError("Destination provided insufficient evidence for a benchmark read")
    return ticket, result.result["label"]


def _reference_run(world: World, task: str, case: Case, *, parallel: bool) -> dict[str, str]:
    if parallel and len(case.first) > 1:
        with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
            values = dict(pool.map(lambda ticket: _read_ticket(world, task, ticket), case.first))
    else:
        values = dict(_read_ticket(world, task, ticket) for ticket in case.first)
    if case.followup is not None and values[case.first[0]] == "review":
        key, value = _read_ticket(world, task, case.followup)
        values[key] = value
    return values


def _fixture_program(case: Case) -> str:
    # Emit per-case source deterministically. This fixture adds no model inference.
    body = """
const values = {};
const read = (id) => tools['support.lookup']({ticket_id: id}).result.label;
for (const id of input.first) values[id] = read(id);
"""
    if case.followup is not None:
        body += "if (values[input.first[0]] === 'review') values[input.followup] = read(input.followup);\n"
    return body + "return values;"


def _p16_trial(world: World, oracle: RoutingOracle, case: Case, arm: str) -> dict:
    task = f"p16-{case.name}-{arm}"
    source = "code_mode" if arm == "program_fixture" else "direct"
    world.control.start_task(OPERATOR, task, worker="bench", source=source)
    first_dispatch = len(oracle.journal)
    start = time.perf_counter()
    used = resource.getrusage(resource.RUSAGE_CHILDREN)
    program_bytes = 0
    inputs = {"first": list(case.first), "followup": case.followup}
    if arm == "program_fixture":
        from forge_check.governed.code_mode import CodeMode

        program = _fixture_program(case)
        program_bytes = len(program.encode())
        run = CodeMode(world.host, enabled=True, bindings=frozenset({TICKET})).run(
            OPERATOR,
            task,
            f"program-{case.name}",
            program,
            inputs=inputs,
        )
        output, program_status = run.output, run.status
    else:
        output = _reference_run(world, task, case, parallel=arm == "direct")
        program_status = None
    elapsed = (time.perf_counter() - start) * 1000
    final = resource.getrusage(resource.RUSAGE_CHILDREN)
    observations = oracle.journal[first_dispatch:]
    delivered = [entry for entry in observations if entry["source"] == "dispatch"]
    return {
        "case": case.name,
        "arm": arm,
        "accepted": output == case.expected,
        "dispatches": len(delivered),
        "program_status": program_status,
        "program_bytes": program_bytes,
        "fixture_input_bytes": len(json.dumps(inputs, separators=(",", ":")).encode()),
        "normalized_output_bytes": len(json.dumps(output, separators=(",", ":")).encode()),
        "model_calls": 0,
        "model_tokens": 0,
        "synthetic_charge_microusd": sum(entry["charge_microusd"] for entry in delivered),
        "elapsed_ms": round(elapsed, 4),
        "executor_cpu_ms": round(
            1000 * (final.ru_utime + final.ru_stime - used.ru_utime - used.ru_stime), 4
        ),
    }


def _paired_latency(rows: list[dict], rng: random.Random) -> dict:
    per_case: dict[str, dict[str, float]] = {}
    for row in rows:
        per_case.setdefault(row["case"], {})[row["arm"]] = row["elapsed_ms"]
    pairs = [arms["program_fixture"] - arms["direct"] for arms in per_case.values()]
    estimates = [mean(rng.choices(pairs, k=len(pairs))) for _ in range(2_000)]
    return {
        "mean_program_minus_direct_ms": round(mean(pairs), 3),
        "bootstrap_95_ms": [round(_percentile(estimates, p), 3) for p in (0.025, 0.975)],
    }


def p16() -> dict:
    rng = random.Random(P16_SEED)
    by_family = {}
    replications = []
    since = utc(now())
    with RoutingOracle() as oracle:
        world = World("p16-mechanism", oracle)
        for family in ("single", "batch", "conditional"):
            rows = []
            for case in _cases(family, rng, oracle):
                order = ["direct", "recipe", "program_fixture"]
                rng.shuffle(order)
                rows.extend(_p16_trial(world, oracle, case, arm) for arm in order)
            replications.extend(rows)
            by_family[family] = {"paired_latency": _paired_latency(rows, rng)}
            for arm in ("direct", "recipe", "program_fixture"):
                group = [row for row in rows if row["arm"] == arm]
                accepted = sum(row["accepted"] for row in group)
                charge = sum(row["synthetic_charge_microusd"] for row in group)
                by_family[family][arm] = {
                    "assigned": len(group),
                    "accepted": accepted,
                    "dispatches": sum(row["dispatches"] for row in group),
                    "synthetic_charge_per_accepted_microusd": charge / accepted
                    if accepted
                    else None,
                    **_duration([row["elapsed_ms"] for row in group]),
                }
                require(accepted == len(group), f"P16 fixture mismatch for {family}/{arm}")
        coverage = verify(world.store, OWNER, since=since, until=utc(now() + 1))
        require("code_mode" in coverage["routes"]["mediated"], "P16 Code Mode route omitted")
    return {
        "id": "P16",
        "status": "inconclusive",
        "mechanism": "verified",
        "reason": "A deterministic program fixture does not estimate model generation cost or quality.",
        "default_route": "direct",
        "paired_seed": P16_SEED,
        "control": "direct",
        "investment_gate": {
            "minimum_cost_savings_fraction": 0.20,
            "paired_confidence": 0.95,
            "maximum_success_loss_fraction": 0.02,
            "maximum_critical_false_successes": 0,
            "pilot_slo_p95_ms": None,
        },
        "families": by_family,
        "replications": replications,
    }


def _measure(fn, phase: str, count: int = TASKS_PER_PHASE) -> dict:
    def run_one(index: int) -> tuple[float, str]:
        start = time.perf_counter()
        try:
            result = fn(f"P21-{phase}-{index:03}")
        except sqlite3.OperationalError as exc:
            result = "ledger_busy" if "locked" in str(exc).lower() else "ledger_error"
        return (time.perf_counter() - start) * 1000, result

    started = time.perf_counter()
    cpu = time.process_time()
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        samples = list(pool.map(run_one, range(count)))
    seconds = time.perf_counter() - started
    accepted = sum(status == "verified" for _, status in samples)
    return {
        "accepted": accepted,
        "assigned": count,
        "tasks_per_second": round(accepted / seconds, 3),
        "process_cpu_ms": round((time.process_time() - cpu) * 1000, 3),
        "contention_errors": sum(status == "ledger_busy" for _, status in samples),
        "statuses": [status for _, status in samples],
        **_duration([ms for ms, _ in samples]),
    }


def _ledger_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in (path, Path(f"{path}-wal")) if item.exists())


def p21(install_ms: int) -> dict:
    with Oracle() as oracle:
        world = World("p21-operations", oracle, manifest(effect="write"))
        raw_provider = HttpProvider(oracle.origin, token=TOKEN)
        configuration = world.base.document["bindings"][TICKET]
        baseline_bytes = _ledger_bytes(world.db)
        phases = {}

        def raw(task: str) -> str:
            operation_key = hashlib.sha256(task.encode()).hexdigest()
            document = raw_provider.dispatch(
                {
                    "attempt_id": task,
                    "operation_id": task,
                    "operation_key": operation_key,
                    "binding_id": TICKET,
                    "revision": world.base.digest,
                    "stream": False,
                    "configuration": configuration,
                    "input": {"ticket_id": task},
                }
            )
            return "verified" if document.get("confirmation") == "verified" else "unconfirmed"

        def governed(task: str) -> str:
            world.control.start_task(OPERATOR, task, worker="bench")
            result = world.host.invoke(request(task, task, task))
            world.control.finish_task(OPERATOR, task)
            return str(result.attempt["confirmation"])

        phases["raw_first"] = _measure(raw, "raw-first", 1)
        phases["governed_first"] = _measure(governed, "governed-first", 1)
        phases["raw"] = _measure(raw, "raw")
        phases["governed"] = _measure(governed, "governed")

        document = world.base.document
        document["bindings"][MODEL]["model"] = "rollout-benchmark-v2"
        revision = Revision.parse(document)
        proposal = "P21-proposal"
        world.control.register(OWNER, revision)
        world.control.propose(OWNER, proposal, revision.digest)
        grade = evaluate(world, "P21-evidence", proposal, revision.digest)
        require(grade["report"]["verdict"] == "pass", "P21 expected actionable evidence")
        for mode in ("shadow", "canary"):
            world.control.start_rollout(
                OWNER,
                f"P21-{mode}",
                proposal,
                "P21-evidence",
                1,
                mode=mode,
                source="direct",
                seed="p21-frozen-cohort-v1",
                basis_points=5_000,
                ends_at=now() + 60_000_000,
                max_concurrent=PARALLELISM,
                budget_microusd=10_000_000,
            )
            phases[mode] = _measure(governed, mode)
            world.control.stop_rollout(OWNER, f"P21-{mode}", 1)
        phases["returned"] = _measure(governed, "returned")

        destination = oracle.dispatches()
        for phase, measures in phases.items():
            require(measures["accepted"] == measures["assigned"], f"P21 incomplete phase: {phase}")
            count = sum(
                row["ticket"].startswith(f"P21-{phase.replace('_', '-')}-")
                and (phase not in {"raw", "governed"} or "-first-" not in row["ticket"])
                for row in destination
            )
            require(count == measures["assigned"], f"P21 unexpected destination count: {phase}")
        require(len(oracle.effects) == len(destination), "P21 duplicate or missing provider effect")
        served = {
            phase: sorted(
                {
                    row["revision"]
                    for row in destination
                    if row["ticket"].startswith(f"P21-{phase}-")
                }
            )
            for phase in ("shadow", "canary", "returned")
        }
        require(
            served["shadow"] == served["returned"] == [world.base.digest],
            "P21 stable traffic deviated",
        )
        require(revision.digest in served["canary"], "P21 provider missed the candidate cohort")

        with world.store.read() as connection:
            count, bytes_stored = connection.execute(
                "SELECT COUNT(*), SUM(LENGTH(CAST(body AS BLOB))) FROM receipts"
            ).fetchone()
            claims = [
                float(row[0])
                for row in connection.execute(
                    """SELECT (claim.recorded_at - admit.recorded_at) / 1000.0
                     FROM receipts claim JOIN receipts admit ON admit.attempt_id = claim.attempt_id
                    WHERE claim.phase = 'dispatch_intent' AND admit.phase = 'admission'"""
                )
            ]
        growth = _ledger_bytes(world.db) - baseline_bytes
        governed_tasks = sum(
            measures["accepted"]
            for phase, measures in phases.items()
            if not phase.startswith("raw")
        )
        require(
            growth / governed_tasks <= P21_BYTES_PER_TASK, "P21 ledger footprint exceeded target"
        )
        require(
            all(p["p95_ms"] <= P21_P95_MS for p in phases.values()),
            "P21 latency exceeded fixture SLO",
        )
        context = ("--db", str(world.db), "--actor", OWNER)
        inspected = world.cli("rollout-status", *context, "--rollout", "P21-canary")
        require(inspected["eligible_observed_roots"] == TASKS_PER_PHASE, "P21 wrong cohort count")
        first_page = world.cli("export", *context, "--limit", "2")
        cursor = first_page["next_after_seq"]
        require(
            len(first_page["receipts"]) == 2 and cursor is not None, "P21 export cursor missing"
        )
        next_page = world.cli("export", *context, "--limit", "2", "--after-seq", str(cursor))
        require(next_page["receipts"][0]["seq"] > cursor, "P21 export repeated a receipt")
        return {
            "id": "P21",
            "status": "pass",
            "phases": phases,
            "provider_revisions": served,
            "provider_effects": len(oracle.effects),
            "observed_eligible_canary_roots": inspected["eligible_observed_roots"],
            "parallelism": PARALLELISM,
            "fixture_slo_p95_ms": P21_P95_MS,
            "fixture_bytes_per_task": P21_BYTES_PER_TASK,
            "wheel_install_ms": install_ms,
            "first_process_ms": {
                "raw_python": _first_process([sys.executable, "-c", "pass"]),
                "forge": _first_process(
                    [str(Path(sys.executable).with_name("forge")), "--version"]
                ),
            },
            "binding_admission_to_claim": _duration(claims),
            "ledger_growth_bytes": growth,
            "bytes_per_governed_task": round(growth / governed_tasks, 2),
            "receipts": count,
            "mean_body_bytes_per_receipt": round(bytes_stored / count, 2),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", choices=("P16", "P21"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--install-ms", type=int, default=0)
    options = parser.parse_args()
    if sys.platform != "linux" or not os.environ.get("FORGE_VAST_LAB_ID", "").isdigit():
        parser.error("run only on the verified Vast Linux host")
    output = options.output.resolve()
    checkout = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="forge-operations-") as temporary:
        previous = Path.cwd()
        try:
            os.chdir(temporary)
            Path("support").mkdir()
            for name in ("cases.json", "baseline.json", "candidate-pass.json"):
                fixture = checkout / "examples/check-support" / name
                (Path("support") / name).write_bytes(fixture.read_bytes())
            report = p16() if options.trial == "P16" else p21(options.install_ms)
        finally:
            os.chdir(previous)
    dump(output, report)
    print(f"{options.trial} {report['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
