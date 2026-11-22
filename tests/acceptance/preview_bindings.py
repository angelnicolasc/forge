"""Installed-wheel acceptance with an independent inventory and invoice destination."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import Any
from unittest.mock import patch

from forge_check.governed.code_mode import CodeMode
from forge_check.governed.control import Control
from forge_check.governed.db import Store
from forge_check.governed.host import Host, HttpProvider
from forge_check.governed.ledger import Observation, Request
from forge_check.governed.revision import Revision

STOCK = "inventory.stock"
INVOICE = "billing.balance"
OWNER = "release-owner"
READER = "inventory-reader"
OTHER = "secondary-reader"
DENIED = "no-stock-grant"
PRIVATE = "internal-synthetic-margin"
KEY = b"preview-reference-admission-key-v1"
TOKEN = "preview-synthetic-provider-token"
RULE = {"schema_version": 1, "rule_id": "reference-stock-policy", "source": "inventory-ledger"}
CASES = {**RULE, "cases": [{"id": "stock-visible", "expected": "correct"}]}
PRINCIPALS = {
    OWNER: {
        "roles": ["admin", "approve", "evidence", "propose", "inspect", "reconcile"],
        "bindings": [],
    },
    READER: {"roles": ["invoke"], "bindings": [STOCK, INVOICE]},
    OTHER: {"roles": ["invoke"], "bindings": [STOCK, INVOICE]},
    DENIED: {"roles": ["invoke"], "bindings": [INVOICE]},
}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise AssertionError(reason)


def schema(properties: dict[str, dict], required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def _binding(name: str, input_field: str, output: dict, visible: list[str], *, ttl: int) -> dict:
    server, method = name.split(".", 1)
    return {
        "kind": "tool",
        "server": server,
        "method": method,
        "version": "1",
        "tariff": "preview-fixture-v1",
        "reserve_microusd": 25_000,
        "effect": "read",
        "idempotency": True,
        "cache_ttl_seconds": ttl,
        "contract": {
            "schema_version": 1,
            "input": schema({input_field: {"type": "string", "format": "identifier"}}),
            "output": output,
            "visible": visible,
        },
    }


def revision_document(*, ttl: int = 30, budget: int = 120_000) -> dict:
    stock = schema(
        {
            "sku": {"type": "string", "format": "identifier"},
            "on_hand": {"type": "integer", "minimum": 0},
        },
    )
    invoices = schema(
        {
            "currency": {"type": "string", "maxLength": 3},
            "balance_cents": {"type": "integer", "minimum": 0},
            "entries": {
                "type": "array",
                "maxItems": 5,
                "items": schema(
                    {
                        "posted": {"type": "boolean"},
                        "amount_cents": {"type": "integer"},
                    }
                ),
            },
        }
    )
    return {
        "schema_version": 2,
        "scope": "inventory-preview",
        "budget_microusd": budget,
        "policy": {"allowed_bindings": [STOCK, INVOICE]},
        "check": {"rule_id": RULE["rule_id"], "verifier": "forge-check/1"},
        "bindings": {
            STOCK: _binding(STOCK, "sku", stock, ["sku", "on_hand"], ttl=ttl),
            INVOICE: _binding(
                INVOICE, "invoice_id", invoices, list(invoices["properties"]), ttl=ttl
            ),
        },
    }


class Destination(ThreadingHTTPServer):
    """Business database independent of the Forge ledger and revision definitions."""

    def __init__(self, path: Path) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.path = path
        self.journal: list[dict[str, Any]] = []
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE movement (sku TEXT, delta INTEGER)")
            connection.execute("CREATE TABLE entry (invoice_id TEXT, amount INTEGER)")
            connection.executemany(
                "INSERT INTO entry VALUES (?, ?)", [("A-18", 3500), ("A-18", -1200)]
            )
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def __enter__(self) -> Destination:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(5)

    def adjust(self, sku: str, delta: int) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO movement VALUES (?, ?)", (sku, delta))

    def respond(self, envelope: dict) -> tuple[dict, bool]:
        binding = envelope["binding_id"]
        arguments = envelope["input"]
        with closing(sqlite3.connect(self.path)) as connection:
            if binding == STOCK:
                require(set(arguments) == {"sku"}, "Inventory must receive its own argument")
                sku = arguments["sku"]
                balance = connection.execute(
                    "SELECT SUM(delta) FROM movement WHERE sku = ?", (sku,)
                ).fetchone()[0]
                result = {"sku": sku, "on_hand": balance, "internal_cost": PRIVATE}
                stalled = sku == "DELAY-1"
            elif binding == INVOICE:
                require(set(arguments) == {"invoice_id"}, "Billing must receive its own argument")
                amounts = connection.execute(
                    "SELECT amount FROM entry WHERE invoice_id = ? ORDER BY rowid",
                    (arguments["invoice_id"],),
                ).fetchall()
                result = {
                    "currency": "USD",
                    "balance_cents": sum(amount for (amount,) in amounts),
                    "entries": [
                        {"posted": True, "amount_cents": amount, "note": PRIVATE}
                        for (amount,) in amounts
                    ],
                    "private_account": PRIVATE,
                }
                stalled = False
            else:
                raise AssertionError("Unexpected capability reached the destination")
        self.journal.append(
            {
                "binding": binding,
                "input": arguments,
                "revision": envelope["revision"],
                "operation_key": envelope["operation_key"],
                "attempt_id": envelope["attempt_id"],
                "private_field_present": PRIVATE in json.dumps(result),
                "delayed": stalled,
            }
        )
        return {
            "observation_id": f"obs-{envelope['attempt_id']}",
            "attempt_id": envelope["attempt_id"],
            "operation_key": envelope["operation_key"],
            "outcome": "succeeded",
            "confirmation": "verified",
            "charge_microusd": 4000,
            "result": result,
        }, stalled


class _Handler(BaseHTTPRequestHandler):
    server: Destination

    def log_message(self, *_: object) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != "/dispatch" or self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_error(403)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        response, stalled = self.server.respond(body)
        if stalled:
            time.sleep(0.4)
        data = json.dumps(response).encode()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


def start_world(path: Path, target: Destination, *, ttl: int = 30, timeout: float = 5) -> tuple:
    store = Store(path)
    revision = Revision.parse(revision_document(ttl=ttl))
    control = Control(store)
    control.bootstrap("inventory-preview", revision, CASES, PRINCIPALS)
    host = Host(
        store,
        fingerprint_key=KEY,
        provider=HttpProvider(target.origin, token=TOKEN, timeout=timeout),
    )
    return control, host, revision


def activate(control: Control, before: Revision) -> Revision:
    after = Revision.parse(revision_document(budget=150_000))
    control.register(OWNER, after)
    control.propose(OWNER, "new-for-cache", after.digest, source="human")

    def evidence(revision: Revision) -> dict:
        return {
            **RULE,
            "revision": revision.digest,
            "cases": [
                {"id": "stock-visible", "status": "completed", "observed": "correct"},
            ],
        }

    control.evaluate(
        OWNER,
        "verified-stock",
        "new-for-cache",
        evidence(before),
        evidence(after),
        observed_at=time.time_ns() // 1000,
        generation=1,
    )
    decision = control.promote(OWNER, "new-revision", "new-for-cache", "verified-stock", 1)
    require(decision["status"] == "applied", "Could not activate the new revision")
    return after


def exercise(control: Control, host: Host, first: Revision, target: Destination) -> dict:
    def direct(task: str, binding: str, args: dict, actor: str = READER):
        control.start_task(actor, task, worker="preview", source="direct")
        return host.invoke(Request(task, actor, "query", task, binding, args))

    def program(task: str, binding: str, args: dict, actor: str = READER):
        control.start_task(actor, task, worker="preview", source="code_mode")
        return CodeMode(host, enabled=True, bindings={binding}).run(
            actor,
            task,
            f"{task}-read",
            f"const call = tools[{json.dumps(binding)}](input); "
            "if (!['succeeded', 'cached'].includes(call.status)) throw new Error('Read unavailable'); "
            "return call.result;",
            inputs=args,
        )

    expected = {"sku": "A-18", "on_hand": 9}
    direct_stock = direct("inv-direct", STOCK, {"sku": "A-18"})
    require(
        direct_stock.result == expected, "Direct stock differs from fixture or leaks private fields"
    )
    # A different SKU avoids a shared cache hit and proves the JS path at the destination.
    code_stock = program("inv-code", STOCK, {"sku": "B-29"})
    require(
        code_stock.output == {"sku": "B-29", "on_hand": 16},
        f"Code stock failed: {code_stock.reason}",
    )
    require(
        [(row["binding"], row["input"], row["revision"]) for row in target.journal]
        == [(STOCK, {"sku": "A-18"}, first.digest), (STOCK, {"sku": "B-29"}, first.digest)],
        "Destination did not receive both original stock calls at the pinned revision",
    )
    cached = program("cross-route-cache", STOCK, {"sku": "A-18"})
    require(cached.output == expected, "The exact authorized cached projection was unavailable")
    require(len(target.journal) == 2, "Cache reissued an existing authorized read")

    # An identical ID in another tool and another actor must not reuse that stock value.
    invoice = direct("invoice-holdout", INVOICE, {"invoice_id": "A-18"})
    invoice_expected = {
        "currency": "USD",
        "balance_cents": 2300,
        "entries": [
            {"posted": True, "amount_cents": 3500},
            {"posted": True, "amount_cents": -1200},
        ],
    }
    require(invoice.result == invoice_expected, "Invoice holdout returned the wrong shape")
    invoice_js = program("invoice-code", INVOICE, {"invoice_id": "A-18"}, OTHER)
    require(invoice_js.output == invoice_expected, f"Invoice program failed: {invoice_js.reason}")
    target.adjust("A-18", 3)
    isolated = direct("actor-isolated", STOCK, {"sku": "A-18"}, OTHER)
    require(
        isolated.result == {"sku": "A-18", "on_hand": 12}, "Another actor reused a cached value"
    )

    replay = direct("inv-direct", STOCK, {"sku": "A-18"})
    require(replay.disposition == "replay", "Repeating the same attempt must be idempotent")
    denied = direct("ungranted", STOCK, {"sku": "A-18"}, DENIED)
    require(denied.attempt["status"] == "denied", "Actor permission was bypassed")
    denied_js = program("ungranted-js", STOCK, {"sku": "A-18"}, DENIED)
    require(denied_js.status != "completed", "Code Mode ran for the denied actor")
    malformed = direct("ticket-alias", STOCK, {"ticket_id": "A-18"})
    require(
        malformed.attempt["reason"] == "invalid_binding_input",
        "SKU was accepted through ticket alias",
    )
    require(len(target.journal) == 5, "A replay or failed authorization reached the provider")

    second = activate(control, first)
    changed = direct("revision-isolated", STOCK, {"sku": "A-18"})
    require(changed.result == isolated.result, "The new revision received the old cached answer")
    require(
        target.journal[-1]["revision"] == second.digest, "Destination received the wrong revision"
    )
    saved = host.ledger.receipts("inv-direct")
    require(
        any(r["phase"] == "dispatch_intent" and r["reserved_microusd"] == 25_000 for r in saved),
        "No durable reservation before send",
    )

    for task, route in (("timeout-direct", "direct"), ("timeout-code", "code_mode")):
        control.start_task(READER, task, worker="preview", source=route)
    short = Host(
        host.store,
        fingerprint_key=KEY,
        provider=HttpProvider(target.origin, token=TOKEN, timeout=0.12),
    )
    delayed = short.invoke(
        Request("timeout-direct", READER, "lost", "lost", STOCK, {"sku": "DELAY-1"})
    )
    require(delayed.attempt["status"] == "uncertain", "The direct timeout became a false success")
    delayed_js = CodeMode(short, enabled=True, bindings={STOCK}).run(
        READER,
        "timeout-code",
        "lost",
        "return tools['inventory.stock'](input).result;",
        inputs={"sku": "DELAY-1"},
    )
    require(delayed_js.status != "completed", "The program accepted an uncertain send")
    for task in ("timeout-direct", "timeout-code"):
        balance = host.ledger.balance(task)
        require(balance["pending_microusd"] == 25_000, "Timeout released the provider exposure")
    require(
        Counter(row["binding"] for row in target.journal) == {STOCK: 6, INVOICE: 2},
        "Wrong destination counts",
    )
    require(
        all(row["private_field_present"] for row in target.journal),
        "Privacy control was not exercised",
    )
    evidence = {
        "direct_inventory": direct_stock.result,
        "code_inventory": code_stock.output,
        "code_invoice": invoice_js.output,
        "direct_invoice": invoice.result,
        "revisions": [first.digest, second.digest],
        "receipts": saved,
        "uncertain_direct": delayed.attempt,
        "uncertain_code": host.ledger.receipts("timeout-code"),
        "cases": {
            "authorization_zero_dispatch": True,
            "cache_isolation": True,
            "prior_reservation": True,
        },
    }
    require(PRIVATE not in json.dumps(evidence), "Sensitive result persisted in the evidence")
    return evidence


def settlement_race(control: Control, host: Host, target: Destination) -> dict:
    task = "invalid-output-after-reconciliation"
    control.start_task(READER, task, worker="preview", source="direct")
    request = Request(task, READER, "missing", "missing", STOCK, {"sku": "MISSING"})

    class ReconciledProvider(HttpProvider):
        def dispatch(self, body: dict) -> dict:
            response = super().dispatch(body)
            host.ledger.settle(
                request.attempt_id,
                Observation(
                    observation_id=response["observation_id"],
                    outcome="succeeded",
                    confirmation="verified",
                    charge_microusd=response["charge_microusd"],
                ),
            )
            return response

    concurrent = Host(
        host.store, fingerprint_key=KEY, provider=ReconciledProvider(target.origin, token=TOKEN)
    )
    trace_start = len(target.journal)
    rejected = concurrent.invoke(request)
    replay = concurrent.invoke(request)
    receipts = host.ledger.receipts(task)
    require(rejected.result is None and rejected.disposition == "invalid_result", "Invalid output")
    require(
        replay.disposition == "invalid_result", "Replaying the rejected output gave a false success"
    )
    require(len(target.journal) == trace_start + 1, "Replaying invalid output resent the operation")
    require(
        replay.attempt["charge_microusd"] == 4000, "Projection replay changed the upstream charge"
    )
    require(sum(event["phase"] == "observation" for event in receipts) == 1, "Charged twice")
    require(
        sum(event["phase"] == "result_projection" for event in receipts) == 1,
        "Missing rejection receipt",
    )
    return {"disposition": replay.disposition, "receipts": receipts}


def cache_completion_order(work: Path, *, expire_winner: bool) -> dict:
    sku = "CACHE-RACE"
    sampled_earlier = threading.Event()
    release_earlier = threading.Event()

    class OrderedDestination(Destination):
        def respond(self, envelope: dict) -> tuple[dict, bool]:
            response, stalled = super().respond(envelope)
            self.journal[-1]["observed_on_hand"] = response["result"]["on_hand"]
            if envelope["attempt_id"] == "cache-earlier":
                sampled_earlier.set()
                require(release_earlier.wait(10), "Timed out waiting to release the earlier read")
            return response, stalled

    work.mkdir()
    with OrderedDestination(work / "external.sqlite3") as target:
        target.adjust(sku, 11)
        target.adjust("CACHE-SWEEP", 2)
        control, host, _ = start_world(work / "forge.sqlite3", target, timeout=15)

        def read(task: str, subject: str = sku):
            control.start_task(READER, task, worker="preview", source="direct")
            return host.invoke(Request(task, READER, "read", task, STOCK, {"sku": subject}))

        same_tick = time.time_ns() // 1_000
        with (
            patch("forge_check.governed.ledger._utc", return_value=same_tick) as clock,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            pending = pool.submit(read, "cache-earlier")
            try:
                require(sampled_earlier.wait(5), "The earlier read never reached the destination")
                target.adjust(sku, -8)
                later = read("cache-later")
                require(
                    later.disposition == "dispatch"
                    and later.attempt["confirmation"] == "verified"
                    and later.result == {"sku": sku, "on_hand": 3},
                    "The later read did not populate the cache with the new stock",
                )
                if expire_winner:
                    clock.return_value += 31_000_000
                    # A different cache write runs the regular expiry sweep while the read waits.
                    require(read("cache-sweep", "CACHE-SWEEP").result is not None, "Sweep failed")
            finally:
                release_earlier.set()
            earlier = pending.result(timeout=5)
            require(
                earlier.disposition == "dispatch"
                and earlier.attempt["confirmation"] == "verified"
                and earlier.result == {"sku": sku, "on_hand": 11},
                "The earlier read should still return its own verified response",
            )
            observed = read("cache-check")

        tasks = ("cache-earlier", "cache-later", "cache-check")
        receipts = {task: host.ledger.receipts(task) for task in tasks}
        first_claim, second_claim = [
            next(row for row in receipts[task] if row["phase"] == "dispatch_intent")
            for task in tasks[:2]
        ]
        require(
            first_claim["dispatched_at"] == second_claim["dispatched_at"]
            and first_claim["sequence"] < second_claim["sequence"]
            and receipts["cache-later"][-1]["sequence"] < receipts["cache-earlier"][-1]["sequence"],
            "The verified provider responses did not settle in reverse durable dispatch order",
        )
        require(observed.result == later.result, "The older result became the current SKU value")
        actual = [
            (row["attempt_id"], row["observed_on_hand"])
            for row in target.journal
            if row["input"] == {"sku": sku}
        ]
        expected = [("cache-earlier", 11), ("cache-later", 3)]
        if expire_winner:
            expected.append(("cache-check", 3))
            require(observed.disposition == "dispatch", "An expired cache source became reusable")
        else:
            hit = receipts["cache-check"][-1]
            require(
                observed.disposition == "cached"
                and hit["phase"] == "cache_hit"
                and hit["evidence"]["cached_from_attempt_id"] == "cache-later",
                "The cache receipt did not preserve the later-dispatched source",
            )
        require(actual == expected, "The destination did not observe the expected reads and values")
        return {
            "disposition": observed.disposition,
            "receipts": receipts,
            "destination": target.journal,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    options = parser.parse_args()
    lab = os.environ.get("FORGE_VAST_LAB_ID", "")
    if sys.platform != "linux" or not lab.isdigit():
        parser.error("Requires Linux and a numeric FORGE_VAST_LAB_ID.")
    options.output_dir.mkdir(parents=True, exist_ok=False)
    with options.wheel.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report: dict[str, Any] = {
        "status": "fail",
        "lab_id": lab,
        "vast_lab_id": lab,
        "version": version("forge-os-check"),
        "wheel_sha256": digest,
    }
    with tempfile.TemporaryDirectory(prefix="preview-bindings-") as temporary:
        work = Path(temporary)
        with Destination(work / "external.sqlite3") as target:
            for sku, delta in (("A-18", 12), ("A-18", -3), ("B-29", 16), ("DELAY-1", 1)):
                target.adjust(sku, delta)
            try:
                control, host, first = start_world(work / "forge.sqlite3", target)
                evidence = exercise(control, host, first, target)
                evidence["invalid_output_after_reconciliation"] = settlement_race(
                    control, host, target
                )
                evidence["cache_completion_order"] = {
                    "live": cache_completion_order(work / "ordering-live", expire_winner=False),
                    "expired": cache_completion_order(
                        work / "ordering-expired", expire_winner=True
                    ),
                }
                report.update(status="pass", **evidence)
            except Exception as exc:
                report["failure"] = f"{type(exc).__name__}: {exc}"
            report["destination"] = target.journal
    (options.output_dir / "acceptance.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": report["status"], "reason": report.get("failure")}))
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
