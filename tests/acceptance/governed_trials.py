"""Acceptance cases invoked exclusively by the clean-wheel Vast runner."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import UTC, datetime, timedelta
from importlib.metadata import distribution
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from forge_check.governed.control import Control
from forge_check.governed.db import Store
from forge_check.governed.host import Host, HttpProvider
from forge_check.governed.ledger import Ledger, Request
from forge_check.governed.revision import Revision
from governed_oracle import PRIVATE_INPUT, PRIVATE_OUTPUT, TOKEN, Behavior, Oracle

FINGERPRINT = b"synthetic-test-fixture-fingerprint-key"
OWNER = "release-owner"
OPERATOR = "support-worker"
OTHER = "other-worker"
BOT = "proposer-bot"
MODEL = "support.model"
TICKET = "support.lookup"
SCOPE = "support.test"
ACTORS = {
    OWNER: {
        "roles": ["admin", "propose", "evidence", "approve", "inspect", "reconcile", "invoke"],
        "bindings": [MODEL, TICKET],
    },
    OPERATOR: {"roles": ["invoke"], "bindings": [MODEL, TICKET]},
    OTHER: {"roles": ["invoke"], "bindings": [TICKET]},
    BOT: {"roles": ["propose_automatic"], "bindings": []},
}


def now() -> int:
    return time.time_ns() // 1_000


def utc(value: int) -> str:
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)).isoformat()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@contextlib.contextmanager
def rejects(error: type[Exception]):
    try:
        yield
    except error:
        return
    raise AssertionError(f"Operation should have raised {error.__name__}")


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, document: Any) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def manifest(
    model: str = "lab-model-reference",
    *,
    budget: int = 1_000_000,
    allowed: tuple[str, ...] = (MODEL, TICKET),
    model_reserve: int | None = 600_000,
    ticket_reserve: int = 50_000,
    method: str = "lookup",
    effect: str = "read",
    ticket_cache: int = 0,
) -> dict:
    return {
        "schema_version": 1,
        "scope": SCOPE,
        "budget_microusd": budget,
        "policy": {"allowed_bindings": list(allowed)},
        "check": {"rule_id": "support-routing-v1", "verifier": "forge-check/1"},
        "bindings": {
            MODEL: {
                "kind": "model",
                "version": "1",
                "contract": {},
                "tariff": "fixture-v1",
                "reserve_microusd": model_reserve,
                "provider": "fixture",
                "model": model,
                "prompt": f"Route support tickets with {model}",
                "max_output_tokens": 128,
            },
            TICKET: {
                "kind": "tool",
                "version": "1",
                "contract": {},
                "tariff": "fixture-v1",
                "reserve_microusd": ticket_reserve,
                "server": "tickets",
                "method": method,
                "effect": effect,
                "idempotency": True,
                "cache_ttl_seconds": ticket_cache,
            },
        },
    }


def request(
    task_id: str,
    operation: str,
    ticket: str,
    *,
    actor: str = OPERATOR,
    binding: str = TICKET,
    attempt: str | None = None,
    retry_of: str | None = None,
) -> Request:
    payload = {"ticket_id": ticket}
    if binding == MODEL:
        payload["text"] = PRIVATE_INPUT
    return Request(task_id, actor, operation, attempt or operation, binding, payload, retry_of)


def snapshot(invocation: Any) -> dict:
    return {
        "disposition": invocation.disposition,
        "attempt": invocation.attempt,
        "result": invocation.result,
    }


class World:
    def __init__(self, name: str, provider: Oracle, document: dict | None = None) -> None:
        self.path = Path(name).resolve()
        self.path.mkdir()
        self.db = self.path / "ledger.sqlite3"
        self.store = Store(self.db)
        self.control = Control(self.store)
        self.base = Revision.parse(document or manifest())
        self.cases = read(Path("support/cases.json"))
        self.control.bootstrap(SCOPE, self.base, self.cases, ACTORS)
        self.provider = provider
        self.ledger = Ledger(self.store, fingerprint_key=FINGERPRINT)
        self.host = Host(
            self.store,
            fingerprint_key=FINGERPRINT,
            provider=HttpProvider(provider.origin, token=TOKEN, timeout=12),
        )

    def task(
        self,
        key: str,
        *,
        actor: str = OPERATOR,
        worker: str = "worker-A",
        parent_id: str | None = None,
        case_id: str | None = None,
    ) -> dict:
        return self.control.start_task(
            actor, key, worker=worker, parent_id=parent_id, case_id=case_id
        )

    def cli(self, *args: str, status: int = 0) -> Any:
        command = [str(Path(sys.executable).with_name("forge")), *args]
        env = {
            **os.environ,
            "FORGE_FINGERPRINT_KEY": FINGERPRINT.hex(),
            "FORGE_PROVIDER_TOKEN": TOKEN,
        }
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
        require(
            result.returncode == status, f"{args[0]} returned {result.returncode}: {result.stderr}"
        )
        return json.loads(result.stdout)

    def submit(self, ticket: Request) -> dict:
        return snapshot(self.host.invoke(ticket))

    def child(self, action: str, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                __file__,
                "--worker",
                action,
                str(self.db),
                self.provider.origin,
                *args,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def collect(process: subprocess.Popen[str]) -> dict:
    try:
        stdout, stderr = process.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise AssertionError(f"Child process hung: {stderr}") from None
    require(process.returncode == 0, f"Child failed: {stderr}")
    return json.loads(stdout)


@contextlib.contextmanager
def running(process: subprocess.Popen[str]):
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def latest(doc: dict) -> dict:
    attempt = doc["attempt"]
    require(isinstance(attempt, dict), "Invocation did not expose its attempt snapshot")
    return attempt


def state_matches(result: dict, disposition: str, status: str) -> None:
    require(result["disposition"] == disposition, f"Expected {disposition}: {result['attempt']}")
    require(latest(result)["status"] == status, f"Expected {status}: {result['attempt']}")


def verify_p04(oracle: Oracle) -> dict:
    raw = manifest()
    reversed_fields = {
        key: dict(reversed(value.items())) if isinstance(value, dict) else value
        for key, value in reversed(raw.items())
    }
    reversed_fields["policy"]["allowed_bindings"] = [TICKET, MODEL]
    revision = Revision.parse(raw)
    reordered = Revision.from_json(json.dumps(reversed_fields, ensure_ascii=False))
    vector = copy.deepcopy(raw)
    vector["policy"]["allowed_bindings"] = sorted(vector["policy"]["allowed_bindings"])
    vector["bindings"][MODEL].update(
        idempotency=False, cache_ttl_seconds=0, parameters={}, snapshot=False
    )
    vector_bytes = json.dumps(vector, sort_keys=True, separators=(",", ":")).encode()
    expected_vector = hashlib.sha256(b"forge.revision.v1\0" + vector_bytes).hexdigest()
    require(revision.digest == reordered.digest == expected_vector, "Unstable canonical identity")
    require(revision.canonical == vector_bytes, "Canonical bytes differ from reference vector")
    changed = {}
    for name, path, value in (
        ("prompt", ("bindings", MODEL, "prompt"), "Different prompt"),
        ("model", ("bindings", MODEL, "model"), "candidate-model"),
        ("policy", ("policy", "allowed_bindings"), [MODEL]),
        ("budget", ("budget_microusd",), 800_000),
        ("tool", ("bindings", TICKET, "method"), "lookup-v2"),
    ):
        mutated = copy.deepcopy(raw)
        slot = mutated
        for component in path[:-1]:
            slot = slot[component]
        slot[path[-1]] = value
        changed[name] = Revision.parse(mutated).digest
    require(
        len(set(changed.values()) | {revision.digest}) == 6,
        "Effective state failed to change digest",
    )
    world = World("P04", oracle, raw)
    with rejects(ValueError):
        world.control.register(OWNER, Revision.parse({**raw, "schema_version": 2}))
    with rejects(ValueError):
        Revision.from_json('{"schema_version":1,"schema_version":1}')
    with rejects(ValueError):
        Revision.parse({**raw, "budget_microusd": 2**53})
    require(
        world.control.get_alias(OWNER)["revision"] == revision.digest,
        "Invalid revisions changed alias",
    )
    historic = Control(Store(world.db)).get_revision(OWNER, revision.digest)
    require(historic.canonical == revision.canonical, "Historical manifest did not survive reopen")
    return {
        "reference": revision.digest,
        "canonical": revision.canonical.decode(),
        "mutations": changed,
    }


def verify_p05(oracle: Oracle) -> dict:
    world = World("P05", oracle)
    invoked = world.cli(
        "wrap",
        "--db",
        str(world.db),
        "--actor",
        OPERATOR,
        "--task",
        "P05-root",
        "--worker",
        "worker-A",
        "--binding",
        MODEL,
        "--operation",
        "model-first",
        "--attempt",
        "P05-model",
        "--input",
        json.dumps({"ticket_id": "P05-M", "text": PRIVATE_INPUT}),
        "--provider",
        oracle.origin,
    )
    state_matches(invoked, "dispatch", "succeeded")
    result = world.submit(request("P05-root", "P05-tool", "P05-T"))
    state_matches(result, "dispatch", "succeeded")
    model = oracle.dispatches("P05-M")[0]
    ticket = oracle.dispatches("P05-T")[0]
    require(
        model["model"] == "lab-model-reference",
        "Provider used a different model than the pinned one",
    )
    require(
        model["prompt"] == "Route support tickets with lab-model-reference",
        "Provider prompt did not match revision",
    )
    require(model["max_output_tokens"] == 128, "Provider did not receive the owned limit")
    require(
        (ticket["server"], ticket["method"]) == ("tickets", "lookup"), "Wrong tool reached provider"
    )
    require(
        model["revision"] == ticket["revision"] == world.base.digest,
        "Dispatch did not preserve pin",
    )
    receipts = world.ledger.receipts("P05-root")
    require(
        receipts and all(row["revision"] == world.base.digest for row in receipts),
        "Receipt did not preserve pin",
    )
    require(
        latest(invoked)["provider_model"] == "provider-snapshot-v1",
        "Provider version unavailable in receipt",
    )
    serialized = json.dumps(receipts)
    require(
        not any(marker in serialized for marker in (PRIVATE_INPUT, PRIVATE_OUTPUT, TOKEN)),
        "Sensitive payload in receipt",
    )
    return {"model": model, "tool": ticket, "receipts": receipts}


def verify_p06(oracle: Oracle) -> dict:
    restricted = World("P06-policy", oracle, manifest(allowed=(MODEL,)))
    restricted.task("P06-policy-root")
    policy = restricted.submit(request("P06-policy-root", "P06-policy", "P06-policy"))
    state_matches(policy, "denied", "denied")
    require(latest(policy)["reason"] == "policy_denied", "Missing explicit policy denial")

    world = World("P06-effect", oracle, manifest(method="escalate", effect="write"))
    world.task("P06-unauthorized-root", actor=OTHER)
    principal = world.submit(
        request("P06-unauthorized-root", "P06-acl", "P06-acl", actor=OTHER, binding=MODEL)
    )
    state_matches(principal, "denied", "denied")
    require(latest(principal)["reason"] == "principal_denied", "Missing actor permission denial")
    require(
        not oracle.dispatches("P06-policy") and not oracle.dispatches("P06-acl"),
        "Denied work reached provider",
    )

    world.task("P06-root")
    override = world.submit(
        Request(
            "P06-root",
            OPERATOR,
            "P06-override",
            "P06-override",
            MODEL,
            {"ticket_id": "P06-override", "text": PRIVATE_INPUT, "model": "injected-model"},
        )
    )
    state_matches(override, "denied", "denied")
    require(latest(override)["reason"] == "invalid_binding_input", "Model override was accepted")
    require(not oracle.dispatches("P06-override"), "Model override reached the provider")
    oracle.arrange("P06-LOST", Behavior(mode="drop", charge=18_000, confirmation="verified"))
    lost = request("P06-root", "P06-lost", "P06-LOST")
    ambiguous = world.submit(lost)
    state_matches(ambiguous, "dispatch", "uncertain")
    held = world.ledger.balance("P06-root")
    require(held["available_microusd"] == 950_000, "Lost acknowledgement released reserve")
    world.task("P06-foreign-root")
    foreign = world.submit(request("P06-foreign-root", "P06-foreign", "P06-foreign"))
    state_matches(foreign, "dispatch", "succeeded")
    lost_key = (latest(ambiguous)["operation_key"], "P06-lost")
    foreign_key = (latest(foreign)["operation_key"], "P06-foreign")
    with oracle.changed:
        correct = oracle.outcomes[lost_key]
        oracle.outcomes[lost_key] = oracle.outcomes[foreign_key]
    try:
        mismatched = snapshot(world.host.reconcile(OWNER, "P06-lost"))
        require(latest(mismatched)["status"] == "uncertain", "Foreign result confirmed the attempt")
        require(world.ledger.balance("P06-root") == held, "Foreign result settled reserved spend")
    finally:
        with oracle.changed:
            oracle.outcomes[lost_key] = correct
    confirmed = snapshot(world.host.reconcile(OWNER, "P06-lost"))
    require(latest(confirmed)["confirmation"] == "verified", "Lookup did not corroborate effect")
    require(
        latest(confirmed)["charge_microusd"] == 18_000,
        "Lookup did not reconcile the verified charge",
    )
    repeated = world.submit(lost)
    require(repeated["disposition"] == "replay", "Same attempt was not idempotent")
    dispatches = oracle.dispatches("P06-LOST")
    require(
        len(dispatches) == 1 and dispatches[0]["effect_created"],
        "Acknowledgement replay duplicated effect",
    )
    world.task("P06-revocation-root")
    preflight = world.ledger.admit(
        request("P06-revocation-root", "P06-revocation", "P06-revocation")
    )
    require(
        preflight.disposition == "dispatch", "Revocation control could not reserve before revoke"
    )
    world.control.revoke(OWNER, "P06-revoke-worker", OPERATOR)
    require(
        not world.ledger.mark_dispatched("P06-revocation"),
        "Revoked actor passed the dispatch claim",
    )
    revoked = world.ledger.attempt("P06-revocation")
    require(
        revoked["reason"] == "principal_denied" and not oracle.dispatches("P06-revocation"),
        "Revoked work reached the destination",
    )
    return {
        "denials": [latest(policy), latest(principal), latest(override)],
        "initial": latest(ambiguous),
        "foreign_observation": latest(mismatched),
        "confirmed": latest(confirmed),
        "destination": dispatches,
        "revoked_after_admission": revoked,
    }


def verify_p07(oracle: Oracle) -> dict:
    world = World("P07", oracle)
    world.task("P07-root")
    oracle.arrange("P07-HOLD", Behavior(mode="hold", charge=410_000))
    with running(world.child("invoke", "P07-root", "P07-first", "P07-HOLD", MODEL)) as first:
        oracle.await_dispatch("P07-HOLD")
        before = world.ledger.balance("P07-root")
        try:
            with running(
                world.child("invoke", "P07-root", "P07-second", "P07-second", MODEL)
            ) as second:
                denied = collect(second)
            require(
                latest(denied)["reason"] == "budget_exceeded",
                "Concurrent sibling escaped root limit",
            )
            require(
                not oracle.dispatches("P07-second"),
                "Second reservation reached provider while first was open",
            )
        finally:
            oracle.release("P07-HOLD")
        settled = collect(first)
    state_matches(settled, "dispatch", "succeeded")
    after = world.ledger.balance("P07-root")
    require(
        before["available_microusd"] == 400_000, "Held reservation was not reflected in balance"
    )
    require(
        after["consumed_microusd"] == 410_000 and after["available_microusd"] == 590_000,
        "Wrong settlement amount",
    )

    world.task("P07-other-root")
    another = world.submit(request("P07-other-root", "P07-other", "P07-other", binding=MODEL))
    state_matches(another, "dispatch", "succeeded")
    require(world.ledger.balance("P07-root") == after, "Independent root changed prior spending")

    zero = World("P07-zero", oracle, manifest(budget=0))
    zero.task("P07-zero-root")
    zero_result = zero.submit(request("P07-zero-root", "P07-zero", "P07-zero", binding=MODEL))
    require(
        latest(zero_result)["reason"] == "budget_exceeded", "Zero budget was discarded by defaults"
    )
    require(not oracle.dispatches("P07-zero"), "Zero-budget request reached provider")

    unknown = World("P07-unknown", oracle, manifest(model_reserve=None))
    unknown.task("P07-unknown-root")
    unknown_result = unknown.submit(
        request("P07-unknown-root", "P07-unknown", "P07-unknown", binding=MODEL)
    )
    require(latest(unknown_result)["reason"] == "price_unknown", "Unbounded price was admitted")
    require(not oracle.dispatches("P07-unknown"), "Unbounded request reached provider")

    world.task("P07-cancel-root")
    oracle.arrange("P07-cancel", Behavior(charge=19_000, outcome="cancelled"))
    event = threading.Event()
    stream = world.host.stream(
        request("P07-cancel-root", "P07-cancel", "P07-cancel", binding=MODEL), cancel=event
    )
    require(next(stream).type == "chunk", "Cancellation control never began streaming")
    event.set()
    terminal = list(stream)[-1]
    require(terminal.type == "terminal", "Cancellation had no terminal receipt")
    cancel_pending = world.ledger.balance("P07-cancel-root")
    require(
        cancel_pending["available_microusd"] == 400_000,
        "Cancellation freed exposure before reconciliation",
    )
    world.host.reconcile(OWNER, "P07-cancel")
    cancel_final = world.ledger.balance("P07-cancel-root")
    require(
        cancel_final["consumed_microusd"] == 19_000, "Cancellation lost the partial provider charge"
    )
    return {
        "held": before,
        "settled": after,
        "cancel_pending": cancel_pending,
        "cancel_final": cancel_final,
        "denial": latest(denied),
    }


def verify_p08(oracle: Oracle) -> dict:
    world = World("P08", oracle, manifest(ticket_cache=30))
    since = utc(now() - 1_000_000)
    world.task("P08-root")
    oracle.arrange("P08-CACHE", Behavior(charge=9_000))
    direct = world.submit(request("P08-root", "P08-original", "P08-CACHE"))
    state_matches(direct, "dispatch", "succeeded")
    cached = world.submit(request("P08-root", "P08-cached", "P08-CACHE"))
    require(cached["disposition"] == "cached", "Cacheable authorized read dispatched again")
    require(len(oracle.dispatches("P08-CACHE")) == 1, "Cache hit had an unexpected provider call")

    oracle.arrange("P08-DROP", Behavior(mode="timeout", charge=12_000, outcome="failed"))
    short = Host(
        world.store,
        fingerprint_key=FINGERPRINT,
        provider=HttpProvider(oracle.origin, token=TOKEN, timeout=1.0),
    )
    try:
        missing = snapshot(
            short.invoke(request("P08-root", "P08-op", "P08-DROP", attempt="P08-first"))
        )
        require(
            bool(oracle.dispatches("P08-DROP")),
            "Timeout occurred before independent provider accepted work",
        )
    finally:
        oracle.release("P08-DROP")
    state_matches(missing, "dispatch", "uncertain")
    pending = world.ledger.balance("P08-root")
    require(
        pending["consumed_microusd"] == 9_000 and pending["available_microusd"] == 941_000,
        "Timeout exposure was not retained",
    )
    after_query = snapshot(world.host.reconcile(OWNER, "P08-first"))
    require(latest(after_query)["status"] == "failed", "Late provider failure was not recorded")
    require(
        latest(after_query)["charge_microusd"] == 12_000, "Post-dispatch failure lost real charge"
    )
    key = latest(after_query)["operation_key"]
    with oracle.changed:
        oracle.outcomes[key, "P08-first"]["observation_id"] = "provider-duplicate-charge-notice"
    world.host.reconcile(OWNER, "P08-first")
    duplicate = world.ledger.balance("P08-root")
    require(
        duplicate["consumed_microusd"] == 21_000, "A duplicate charge notice changed consumption"
    )

    oracle.arrange("P08-DROP", Behavior(charge=8_000))
    retried = world.submit(
        request("P08-root", "P08-op", "P08-DROP", attempt="P08-retry", retry_of="P08-first")
    )
    state_matches(retried, "dispatch", "succeeded")
    attempts = oracle.dispatches("P08-DROP")
    require(
        len(attempts) == 2 and {row["operation_key"] for row in attempts} == {key},
        "Retry lost logical operation key",
    )
    require(
        latest(snapshot(world.host.reconcile(OWNER, "P08-first")))["charge_microusd"] == 12_000,
        "Retry's charge was attributed to its predecessor",
    )
    balance = world.ledger.balance("P08-root")
    require(
        balance["consumed_microusd"] == 29_000 and balance["available_microusd"] == 971_000,
        "Consumer balance diverged from destination charges",
    )
    require(
        balance["reserved_microusd"] == balance["pending_microusd"] == 0,
        "Known completed costs left stale obligations",
    )
    external = sum(
        row["charge_microusd"] for row in oracle.dispatches() if row["ticket"].startswith("P08-")
    )
    comparison = world.host.compare_charges(
        OWNER,
        since=since,
        until=utc(now() + 1_000_000),
        billing_scope=TICKET,
        provider_total_microusd=external + 500,
    )
    require(
        comparison["observed_charge_microusd"] == external,
        "Aggregate ledger charges differ from oracle",
    )
    require(comparison["difference_microusd"] == 500, "Aggregate discrepancy was not preserved")
    require(
        comparison.get("attribution") == "aggregate_only",
        "Aggregate difference was attributed to a worker",
    )
    world.task("P08-breach-root")
    oracle.arrange("P08-OVER", Behavior(charge=60_000))
    over = world.submit(request("P08-breach-root", "P08-breach", "P08-OVER"))
    require(latest(over)["charge_microusd"] == 60_000, "Over-cap charge was truncated")
    suspended = world.submit(request("P08-breach-root", "P08-suspended", "P08-SUSPENDED"))
    require(
        latest(suspended)["reason"] == "binding_suspended"
        and not oracle.dispatches("P08-SUSPENDED"),
        "Breached binding continued to dispatch",
    )
    return {
        "pending": pending,
        "after_duplicate": duplicate,
        "final": balance,
        "reconciliation": comparison,
        "circuit_breaker": latest(suspended),
    }


def observed_run(
    oracle: Oracle, revision: str, fixture: str, *, unavailable: str | None = None
) -> dict:
    source = read(Path(f"support/{fixture}.json"))
    rows = source["cases"]
    with oracle.changed:
        oracle.queue[revision] = {
            row["id"]: None if row["id"] == unavailable else row["observed"] for row in rows
        }
    for row in rows:
        with urlopen(f"{oracle.origin}/queue/{revision}/{row['id']}", timeout=3) as response:
            row["observed"] = json.load(response)["observed"]
    return {**source, "revision": revision}


def candidate(
    world: World, name: str, *, automatic: bool = False, **overrides: Any
) -> tuple[str, str]:
    revision = Revision.parse(manifest(name, **overrides))
    world.control.register(OWNER, revision)
    proposal = f"proposal-{name}"
    world.control.propose(
        BOT if automatic else OWNER,
        proposal,
        revision.digest,
        source="automatic" if automatic else "human",
    )
    return proposal, revision.digest


def evaluate(
    world: World,
    key: str,
    proposal: str,
    digest: str,
    *,
    input_file: str = "candidate-pass",
    generation: int = 1,
    current: str | None = None,
    observed_at: int | None = None,
) -> dict:
    baseline = observed_run(world.provider, current or world.base.digest, "baseline")
    run = observed_run(world.provider, digest, input_file)
    return world.control.evaluate(
        OWNER, key, proposal, baseline, run, observed_at=observed_at or now(), generation=generation
    )


def verify_p09(oracle: Oracle) -> dict:
    world = World("P09-flow", oracle)
    world.task("P09-parent", case_id="S01")
    child = world.task("P09-child", parent_id="P09-parent")
    ordinary = world.submit(request("P09-child", "P09-read", "P09-READ"))
    terminal = list(world.host.stream(request("P09-child", "P09-stream", "P09-READ")))
    require(
        [frame.type for frame in terminal] == ["chunk", "chunk", "terminal"],
        "Streaming altered the request boundary",
    )
    streamed = snapshot(terminal[-1].invocation)
    state_matches(ordinary, "dispatch", "succeeded")
    state_matches(streamed, "dispatch", "succeeded")
    wire = oracle.dispatches("P09-READ")
    require(
        len(wire) == 2 and {entry["stream"] for entry in wire} == {False, True},
        "Stream did not go through the provider binding",
    )
    require(
        all(row["revision"] == child["revision"] and row["method"] == "lookup" for row in wire),
        "Streaming changed effective arguments",
    )
    receipts = world.ledger.receipts("P09-child")
    require(
        receipts
        and all(
            row["revision"] == world.base.digest
            and row["root_task_id"] == "P09-parent"
            and row["parent_task_id"] == "P09-parent"
            for row in receipts
        ),
        "Streaming lost root or revision receipts",
    )

    oracle.arrange("P09-PARTIAL", Behavior(mode="partial-stream", charge=17_000, outcome="failed"))
    lost = list(
        world.host.stream(request("P09-child", "P09-recover", "P09-PARTIAL", attempt="P09-first"))
    )
    require(
        [frame.type for frame in lost] == ["chunk", "terminal"],
        "Partial stream transitions missing",
    )
    require(
        latest(snapshot(lost[-1].invocation))["status"] == "uncertain",
        "Missing terminal frame was treated as complete",
    )
    reconciled = snapshot(world.host.reconcile(OWNER, "P09-first"))
    require(latest(reconciled)["status"] == "failed", "Missing stream could not be reconciled")
    oracle.arrange("P09-PARTIAL", Behavior(charge=11_000))
    recovered = list(
        world.host.stream(
            request(
                "P09-child", "P09-recover", "P09-PARTIAL", attempt="P09-retry", retry_of="P09-first"
            )
        )
    )
    state_matches(snapshot(recovered[-1].invocation), "dispatch", "succeeded")
    stream_receipts = world.ledger.receipts("P09-child")
    require(
        {"P09-first", "P09-retry"} <= {row["attempt_id"] for row in stream_receipts},
        "Missing distinct attempts in receipts",
    )
    world.task("P09-denied-root", actor=OTHER)
    denied = list(
        world.host.stream(
            request("P09-denied-root", "P09-denied", "P09-denied", actor=OTHER, binding=MODEL)
        )
    )
    require(
        len(denied) == 1
        and latest(snapshot(denied[0].invocation))["reason"] == "principal_denied"
        and not oracle.dispatches("P09-denied"),
        "Streaming escaped admission policy",
    )

    cache = World("P09-cache", oracle, manifest(ticket_cache=30))
    cache.task("P09-cache-A")
    cache.task("P09-cache-B", actor=OTHER)
    state_matches(
        cache.submit(request("P09-cache-A", "P09-baseline-A", "P09-CACHE")), "dispatch", "succeeded"
    )
    state_matches(
        cache.submit(request("P09-cache-B", "P09-baseline-B", "P09-CACHE", actor=OTHER)),
        "dispatch",
        "succeeded",
    )
    proposal, digest = candidate(cache, "P09-cache-v2", ticket_cache=30)
    evaluate(cache, "P09-evidence", proposal, digest)
    applied = cache.control.promote(OWNER, "P09-promotion", proposal, "P09-evidence", 1)
    require(applied["status"] == "applied", "Could not activate alternate cache scope")
    cache.task("P09-cache-C")
    state_matches(
        cache.submit(request("P09-cache-C", "P09-candidate-A", "P09-CACHE")),
        "dispatch",
        "succeeded",
    )
    require(len(oracle.dispatches("P09-CACHE")) == 3, "Cache leaked across actor or revision")
    return {
        "ordinary_and_streaming": wire,
        "recovered": oracle.dispatches("P09-PARTIAL"),
        "isolated_dispatches": oracle.dispatches("P09-CACHE"),
    }


def verify_p10(oracle: Oracle) -> dict:
    rows = []
    digests = set()
    for automatic in (False, True):
        mode = "automatic" if automatic else "human"
        world = World(f"P10-{mode}", oracle)
        proposal, digest = candidate(world, "P10-identical-candidate", automatic=automatic)
        digests.add(digest)
        result = evaluate(world, "P10-pass", proposal, digest)
        require(result["report"]["verdict"] == "pass", "Independent baseline/control did not pass")
        rejected = world.control.reject(OWNER, "P10-rejected", proposal, "P10-pass", 1)
        require(rejected["status"] == "rejected", "Authorized rejection missing")
        require(
            world.control.get_alias(OWNER)["revision"] == world.base.digest,
            "Rejection moved stable",
        )
        weak = observed_run(oracle, digest, "candidate-pass", unavailable="S01")
        baseline = observed_run(oracle, world.base.digest, "baseline")
        insufficient = world.control.evaluate(
            OWNER, "P10-insufficient", proposal, baseline, weak, observed_at=now(), generation=1
        )
        require(
            insufficient["report"]["verdict"] == "insufficient", "Unconfirmed effect was accepted"
        )
        blocked = world.control.promote(OWNER, "P10-blocked", proposal, "P10-insufficient", 1)
        require(
            blocked["status"] == "rejected"
            and world.control.get_alias(OWNER)["revision"] == world.base.digest,
            "Insufficient evidence moved stable",
        )
        with rejects(PermissionError):
            world.control.promote(
                BOT if automatic else OPERATOR, "P10-bad-actor", proposal, "P10-pass", 1
            )
        evaluate(world, "P10-expired", proposal, digest, observed_at=now() - 3_601_000_000)
        expired = world.control.promote(OWNER, "P10-stale", proposal, "P10-expired", 1)
        require(expired["reason"] == "evidence_expired", "Expired proof remained promotable")
        approved = world.control.promote(OWNER, "P10-approved", proposal, "P10-pass", 1)
        require(
            approved["status"] == "applied"
            and world.control.get_alias(OWNER)["revision"] == digest,
            "Approved evidence did not activate candidate",
        )
        require(
            not any(row["ticket"].startswith("P10-") for row in oracle.dispatches()),
            "Release decisions caused an external dispatch",
        )
        attribution = world.control.propose(BOT if automatic else OWNER, proposal, digest, mode)
        require(
            attribution["source"] == mode
            and attribution["actor"] == (BOT if automatic else OWNER)
            and approved["actor"] == OWNER,
            "Proposal and authorization attribution were lost",
        )
        rows.append(
            {
                "proposal": attribution,
                "approved": approved,
                "rejected": rejected,
                "insufficient": blocked,
                "expired": expired,
            }
        )
    require(len(digests) == 1, "Human and automatic routes used different candidate content")
    return {"candidate_digest": next(iter(digests)), "decisions": rows}


def _wait_for_files(files: list[Path]) -> None:
    deadline = time.monotonic() + 10
    while not all(path.exists() for path in files):
        require(time.monotonic() < deadline, "CAS workers did not both reach their start barrier")
        time.sleep(0.01)


def verify_p11(oracle: Oracle) -> dict:
    world = World("P11", oracle)
    left, one = candidate(world, "P11-left")
    right, two = candidate(world, "P11-right")
    evaluate(world, "P11-proof-left", left, one)
    evaluate(world, "P11-proof-right", right, two)
    ready_left = world.path / "left.ready"
    ready_right = world.path / "right.ready"
    go = world.path / "start"
    with (
        running(
            world.child(
                "promote", "P11-race-left", left, "P11-proof-left", str(ready_left), str(go)
            )
        ) as first,
        running(
            world.child(
                "promote", "P11-race-right", right, "P11-proof-right", str(ready_right), str(go)
            )
        ) as second,
    ):
        _wait_for_files([ready_left, ready_right])
        go.touch()
        decisions = [collect(first), collect(second)]
    require(
        {row["status"] for row in decisions} == {"applied", "conflict"},
        "CAS did not yield one applied and one stale write",
    )
    winner_index = next(index for index, row in enumerate(decisions) if row["status"] == "applied")
    winner = decisions[winner_index]
    proposal, evidence = ((left, "P11-proof-left"), (right, "P11-proof-right"))[winner_index]
    repeated = world.control.promote(OWNER, winner["id"], proposal, evidence, 1)
    require(
        repeated == winner and world.control.get_alias(OWNER)["generation"] == 2,
        "Idempotent CAS advanced alias twice",
    )
    third, digest = candidate(world, "P11-later")
    evaluate(
        world, "P11-proof-later", third, digest, generation=2, current=winner["target_revision"]
    )
    later = world.control.promote(OWNER, "P11-newer", third, "P11-proof-later", 2)
    require(later["status"] == "applied", "Control for stale rollback did not move alias")
    stale = world.control.rollback(OWNER, "P11-stale-rollback", 2)
    require(stale["status"] == "conflict", "Stale rollback was not rejected")
    require(
        world.control.get_alias(OWNER)["revision"] == digest,
        "Stale rollback displaced new stable revision",
    )
    return {"race": decisions, "replay": repeated, "later": later, "stale_rollback": stale}


def verify_p12(oracle: Oracle) -> dict:
    world = World("P12", oracle)
    regression, bad = candidate(world, "P12-cheap", model_reserve=400_000)
    good_proposal, good = candidate(world, "P12-correct")
    uncertain_proposal, uncertain = candidate(world, "P12-unavailable")
    fail = evaluate(world, "P12-fail", regression, bad, input_file="candidate-fail")
    passed = evaluate(world, "P12-pass", good_proposal, good)
    baseline = observed_run(oracle, world.base.digest, "baseline")
    absent = observed_run(oracle, uncertain, "candidate-pass", unavailable="S01")
    missing = world.control.evaluate(
        OWNER, "P12-missing", uncertain_proposal, baseline, absent, observed_at=now(), generation=1
    )
    oracle_reads = [
        row
        for row in oracle.journal
        if row["source"] == "support-queue" and row["revision"] in {bad, good, uncertain}
    ]
    require(len(oracle_reads) == 72, "Missing independent case-by-case observations")
    report = fail["report"]
    known_cost = read(Path("support/baseline.json"))["cost_micro_usd"]
    reduced_cost = read(Path("support/candidate-fail.json"))["cost_micro_usd"]
    require(reduced_cost < known_cost, "Business regression did not exercise a cheaper candidate")
    require(
        report["verdict"] == "fail" and report["coverage"]["eligible"] == 24,
        "Regression lost eligible-case denominator",
    )
    require(
        {row["id"] for row in report["cases"] if row["result"] == "fail"} == {"S07", "S08"},
        "Wrong missing escalations",
    )
    require(report["source"] == "support-queue", "Provenance lost the ticket system source")
    require(
        passed["report"]["verdict"] == "pass" and missing["report"]["verdict"] == "insufficient",
        "Control verdicts diverged",
    )
    decision = world.control.promote(OWNER, "P12-block", regression, "P12-fail", 1)
    require(
        decision["status"] == "rejected"
        and world.control.get_alias(OWNER)["revision"] == world.base.digest,
        "Cheaper regression activated",
    )
    with rejects(ValueError):
        world.control.evaluate(
            OWNER,
            "P12-mismatch",
            good_proposal,
            baseline,
            {**absent, "rule_id": "foreign-rule"},
            observed_at=now(),
            generation=1,
        )
    try:
        foreign = world.control.promote(OWNER, "P12-foreign", regression, "P12-pass", 1)
    except ValueError:
        foreign = None
    require(
        foreign is None or foreign["status"] != "applied",
        "A proof for another revision activated the target",
    )
    return {
        "regression": report,
        "pass": passed["report"]["verdict"],
        "missing": missing["report"]["verdict"],
        "no_cross_revision_evidence": True,
        "baseline_cost_microusd": known_cost,
        "regressed_cost_microusd": reduced_cost,
    }


def verify_p13(oracle: Oracle) -> dict:
    world = World("P13", oracle)
    since = utc(now() - 1_000_000)
    prior = world.task("P13-before", worker="worker-A", case_id="S01")
    state_matches(
        world.submit(request("P13-before", "P13-old-before", "P13-old-before", binding=MODEL)),
        "dispatch",
        "succeeded",
    )
    proposal, digest = candidate(world, "P13-candidate")
    evaluate(world, "P13-evidence", proposal, digest)
    applied = world.control.promote(OWNER, "P13-promote", proposal, "P13-evidence", 1)
    require(applied["status"] == "applied", "Promotion was not applied")
    resumed = world.task("P13-before", worker="worker-A", case_id="S01")
    require(resumed == prior, "A resumed task resolved the alias again")
    child = world.task("P13-child", worker="worker-A", parent_id="P13-before", case_id="S01")
    state_matches(
        world.submit(request("P13-child", "P13-old-after", "P13-old-after", binding=MODEL)),
        "dispatch",
        "succeeded",
    )
    modern = world.task("P13-new", worker="worker-B", case_id="S02")
    state_matches(
        world.submit(
            request("P13-new", "P13-candidate-model", "P13-candidate-model", binding=MODEL)
        ),
        "dispatch",
        "succeeded",
    )
    world.task("P13-unconfirmed", worker="worker-C", case_id="S03")
    oracle.arrange("P13-unconfirmed", Behavior(mode="drop", charge=16_000))
    state_matches(
        world.submit(request("P13-unconfirmed", "P13-unconfirmed", "P13-unconfirmed")),
        "dispatch",
        "uncertain",
    )
    rolled = world.control.rollback(OWNER, "P13-rollback", 2)
    require(
        rolled["status"] == "applied" and rolled["generation"] == 3,
        "Rollback did not create generation 3",
    )
    post_rollback = world.task("P13-rollback-task", worker="worker-C", case_id="S04")
    state_matches(
        world.submit(
            request("P13-rollback-task", "P13-rollback-model", "P13-rollback-model", binding=MODEL)
        ),
        "dispatch",
        "succeeded",
    )
    require(
        child["revision"] == post_rollback["revision"] == world.base.digest
        and modern["revision"] == digest,
        "Alias pins diverged",
    )
    require(
        (child["generation"], modern["generation"], post_rollback["generation"]) == (1, 2, 3),
        "Pin generation was not observed",
    )
    rows = {row["ticket"]: row for row in oracle.dispatches() if row["ticket"].startswith("P13-")}
    expected = {
        "P13-old-before": (world.base.digest, "lab-model-reference"),
        "P13-old-after": (world.base.digest, "lab-model-reference"),
        "P13-candidate-model": (digest, "P13-candidate"),
        "P13-rollback-model": (world.base.digest, "lab-model-reference"),
    }
    require(
        all(
            (rows[key]["revision"], rows[key]["model"]) == effective
            for key, effective in expected.items()
        ),
        "Independent provider observed the wrong revision movement",
    )

    until = utc(now() + 1_000_000)
    verification = (
        "verify",
        "--db",
        str(world.db),
        "--actor",
        OWNER,
        "--since",
        since,
        "--until",
        until,
        "--decision",
        "P13-rollback",
    )
    observed = world.cli(*verification)
    coverage = observed["coverage"]
    workers = ["worker-A", "worker-B", "worker-C"]
    require(
        coverage["observed_workers"] == workers
        and coverage["eligible"] is None
        and coverage["silent_workers"] is None,
        "Coverage invented a denominator",
    )
    census = world.path / "roster.json"
    dump(
        census,
        {
            "source": "worker-registry",
            "since": since,
            "until": until,
            "workers": [*workers, "worker-D"],
        },
    )
    verified = world.cli(*verification, "--roster", str(census))
    census_coverage = verified["coverage"]
    require(
        census_coverage["observed"] == 3
        and census_coverage["eligible"] == 4
        and census_coverage["silent_workers"] == ["worker-D"],
        "Trusted roster did not establish observed coverage",
    )
    require(census_coverage["roster_source"] == "worker-registry", "Roster provenance was omitted")
    groups = {
        (row["generation"], row["revision"], row["source"]): row for row in verified["by_revision"]
    }
    require(
        set(groups)
        == {
            (1, world.base.digest, "direct"),
            (2, digest, "direct"),
            (3, world.base.digest, "direct"),
        },
        "Coverage omitted revision or source",
    )
    require(
        {row["case_id"] for row in verified["unconfirmed_cases"]} == {"S03"},
        "Unknown effect was omitted from verification",
    )
    require(verified["decision"]["id"] == "P13-rollback", "Decision was not reconstructable")
    recent = world.cli(
        "verify",
        "--db",
        str(world.db),
        "--actor",
        OWNER,
        "--since",
        utc(applied["created_at"]),
        "--until",
        until,
    )
    earlier = next(row for row in recent["by_revision"] if row["generation"] == 1)
    require(
        earlier["roots_started"] == 0 and earlier["observed_earlier_roots"] == 1,
        "Verification conflated resumed roots with newly started tasks",
    )
    return {
        "before": prior,
        "after_promotion": modern,
        "after_rollback": post_rollback,
        "dispatches": rows,
        "observed": observed,
        "with_roster": verified,
        "after_promotion_window": recent,
    }


def worker(arguments: list[str]) -> int:
    action, db, origin, *values = arguments
    store = Store(Path(db))
    if action == "invoke":
        task, operation, ticket, binding = values
        host = Host(
            store,
            fingerprint_key=FINGERPRINT,
            provider=HttpProvider(origin, token=TOKEN, timeout=12),
        )
        reply = snapshot(host.invoke(request(task, operation, ticket, binding=binding)))
    elif action == "promote":
        decision, proposal, evidence, ready, go = values
        control = Control(store)
        Path(ready).touch()
        _wait_for_files([Path(go)])
        reply = control.promote(OWNER, decision, proposal, evidence, 1)
    else:
        raise ValueError("Unknown worker action")
    print(json.dumps(reply))
    return 0


def run_suite(output: Path) -> int:
    trials = (
        verify_p04,
        verify_p05,
        verify_p06,
        verify_p07,
        verify_p08,
        verify_p09,
        verify_p10,
        verify_p11,
        verify_p12,
        verify_p13,
    )
    installed = distribution("forge-os-check")
    install_location = Path(installed.locate_file("")).resolve()
    require(
        install_location.is_relative_to(Path(sys.prefix)),
        "Test process did not use the clean wheel",
    )
    require(not installed.requires, "Candidate wheel declared unexpected runtime dependencies")
    results: dict[str, str] = {}
    failures: dict[str, str] = {}
    times: dict[str, float] = {}
    with Oracle() as oracle:
        for number, trial in enumerate(trials, 4):
            name = f"P{number:02}"
            start = time.monotonic()
            try:
                evidence = trial(oracle)
                dump(output / f"{name}.json", evidence)
                results[name] = "pass"
            except Exception as error:
                failures[name] = str(error)
                results[name] = "fail"
                (output / f"{name}.traceback").write_text(traceback.format_exc(), encoding="utf-8")
            times[name] = round(time.monotonic() - start, 4)
        dump(output / "destination-journal.json", oracle.journal)
    report: dict[str, Any] = {
        "tests": results,
        "seconds": times,
        "failures": failures,
        "installed_version": installed.version,
        "runtime_requires": installed.requires or [],
        "install_location": str(install_location),
    }
    if failures:
        report["failure"] = "One or more governed acceptance trials failed"
    dump(output / "trials.json", report)
    return int(bool(failures))


if __name__ == "__main__":
    if sys.argv[1] == "--worker":
        raise SystemExit(worker(sys.argv[2:]))
    raise SystemExit(run_suite(Path(sys.argv[1])))
