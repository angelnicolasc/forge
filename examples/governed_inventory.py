"""Run direct and programmatic inventory reads against a loopback fixture."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from secrets import token_bytes
from tempfile import TemporaryDirectory
from threading import Thread

from forge_check.governed.code_mode import CodeMode
from forge_check.governed.control import Control
from forge_check.governed.db import Store
from forge_check.governed.host import Host, HttpProvider
from forge_check.governed.ledger import Request
from forge_check.governed.revision import Revision

BINDING = "inventory.stock"
ACTOR = "inventory-reader"
STOCK = {"A-18": 7, "B-42": 2}
PROGRAM = """
let total_on_hand = 0;
for (const sku of input.skus) {
    const read = tools['inventory.stock']({sku});
    if (!read.result) throw new Error('Stock unavailable');
    total_on_hand += read.result.on_hand;
}
return {total_on_hand};
"""


class Inventory(HTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), StockHandler)
        self.reads: list[str] = []


class StockHandler(BaseHTTPRequestHandler):
    server: Inventory

    def log_message(self, *_: object) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != "/dispatch":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sku = request["input"]["sku"]
        if request["binding_id"] != BINDING or sku not in STOCK:
            self.send_error(400)
            return
        self.server.reads.append(sku)
        attempt = request["attempt_id"]
        payload = json.dumps(
            {
                "attempt_id": attempt,
                "operation_key": request["operation_key"],
                "observation_id": f"obs-{attempt}",
                "outcome": "succeeded",
                "confirmation": "verified",
                "charge_microusd": 1,
                "result": {"sku": sku, "on_hand": STOCK[sku], "internal_cost": 42},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def object_schema(properties: dict[str, dict]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def run_example(path: Path, destination: Inventory) -> dict:
    sku = {"type": "string", "format": "identifier"}
    quantity = {"type": "integer", "minimum": 0}
    binding = {
        "kind": "tool",
        "version": "stock-1",
        "server": "inventory",
        "method": "stock",
        "effect": "read",
        "tariff": "stock-1",
        "reserve_microusd": 2,
        "cache_ttl_seconds": 0,
        "contract": {
            "schema_version": 1,
            "input": object_schema({"sku": sku}),
            "output": object_schema({"sku": sku, "on_hand": quantity}),
            "visible": ["sku", "on_hand"],
        },
    }
    scope = "inventory-example"
    revision = Revision.parse(
        {
            "schema_version": 2,
            "scope": scope,
            "budget_microusd": 8,
            "policy": {"allowed_bindings": [BINDING]},
            "check": {"rule_id": "stock-correct", "verifier": "forge-check/1"},
            "bindings": {BINDING: binding},
        }
    )
    store = Store(path)
    control = Control(store)
    control.bootstrap(
        scope,
        revision,
        {
            "schema_version": 1,
            "rule_id": "stock-correct",
            "source": "inventory-ledger",
            "cases": [{"id": "stock-read", "expected": "correct"}],
        },
        {
            "owner": {"roles": ["admin", "approve", "evidence"], "bindings": []},
            ACTOR: {"roles": ["invoke"], "bindings": [BINDING]},
        },
    )
    host = Host(
        store,
        fingerprint_key=token_bytes(32),
        provider=HttpProvider(f"http://127.0.0.1:{destination.server_port}"),
    )

    control.start_task(ACTOR, "direct", worker="demo")
    direct = host.invoke(Request("direct", ACTOR, "stock", "read-1", BINDING, {"sku": "A-18"}))
    control.start_task(ACTOR, "composed", worker="demo", source="code_mode")
    executor = CodeMode(host, enabled=True, bindings={BINDING})
    catalog = executor.capabilities(ACTOR, "composed")
    if not catalog["available"] or set(catalog["bindings"]) != {BINDING}:
        raise RuntimeError("Install the code-mode extra on Linux to run this example")
    program = executor.run_model(
        ACTOR,
        "composed",
        "stock-total",
        json.dumps({"schema_version": 1, "source": PROGRAM}),
        inputs={"skus": ["A-18", "B-42"]},
        output_schema=object_schema({"total_on_hand": quantity}),
    )
    if direct.result != {"sku": "A-18", "on_hand": 7} or program.status != "completed":
        raise RuntimeError(f"Inventory read failed: {program.reason}")
    if program.output != {"total_on_hand": 9} or len(program.attempt_ids) != 2:
        raise RuntimeError("The composed result or attempt count is incorrect")
    if destination.reads != ["A-18", "A-18", "B-42"]:
        raise RuntimeError("The destination observed unexpected dispatches")
    return {"direct": direct.result, "program": program.output, "dispatches": destination.reads}


def main() -> None:
    with TemporaryDirectory(prefix="forge-inventory-") as temporary, Inventory() as destination:
        thread = Thread(target=destination.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_example(Path(temporary) / "inventory.sqlite3", destination)
        finally:
            destination.shutdown()
            thread.join()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
