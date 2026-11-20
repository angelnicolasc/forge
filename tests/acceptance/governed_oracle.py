"""Independent loopback provider for the governed Vast acceptance trials."""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

TOKEN = "FORGE_SYNTHETIC_PROVIDER_CREDENTIAL"
PRIVATE_INPUT = "FORGE_SYNTHETIC_PRIVATE_INPUT"
PRIVATE_OUTPUT = "FORGE_SYNTHETIC_PRIVATE_OUTPUT"


@dataclass(frozen=True)
class Behavior:
    mode: str = "ok"
    charge: int = 13_000
    outcome: str = "succeeded"
    confirmation: str = "verified"


class Oracle(ThreadingHTTPServer):
    """The fixture knows its network boundary and never opens Forge's ledger."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.changed = threading.Condition()
        self.journal: list[dict] = []
        self.behaviors: dict[str, Behavior] = {}
        self.outcomes: dict[tuple[str, str], dict] = {}
        self.effects: dict[str, str] = {}
        self.open_holds: set[str] = set()
        self.queue: dict[str, dict[str, str | None]] = {}
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def __enter__(self) -> Oracle:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        with self.changed:
            self.open_holds.clear()
            self.changed.notify_all()
        self.shutdown()
        self.server_close()
        self.thread.join(5)

    def arrange(self, ticket: str, behavior: Behavior) -> None:
        with self.changed:
            self.behaviors[ticket] = behavior
            if behavior.mode in {"hold", "timeout"}:
                self.open_holds.add(ticket)

    def release(self, ticket: str) -> None:
        with self.changed:
            self.open_holds.discard(ticket)
            self.changed.notify_all()

    def dispatches(self, ticket: str | None = None) -> list[dict]:
        with self.changed:
            return [
                row
                for row in self.journal
                if row["source"] == "dispatch" and (ticket is None or row["ticket"] == ticket)
            ]

    def await_dispatch(self, ticket: str) -> dict:
        with self.changed:
            if not self.changed.wait_for(lambda: self.dispatches(ticket), timeout=12):
                raise RuntimeError(f"Independent provider never received {ticket}")
            return self.dispatches(ticket)[0]

    def acknowledge(self, request: dict) -> tuple[Behavior, dict, str]:
        configuration = request["configuration"]
        attempt_id = request["attempt_id"]
        key = request["operation_key"]
        ticket = request["input"]["ticket_id"]
        with self.changed:
            behavior = self.behaviors.get(ticket, Behavior())
            effect_id = None
            effect_created = False
            if behavior.outcome == "succeeded" and configuration.get("effect") not in (
                None,
                "read",
            ):
                effect_created = key not in self.effects
                effect_id = self.effects.setdefault(key, f"ticket-effect-{len(self.effects) + 1}")
            response = {
                "attempt_id": attempt_id,
                "operation_key": key,
                "observation_id": f"provider-{attempt_id}",
                "outcome": behavior.outcome,
                "confirmation": behavior.confirmation,
                "charge_microusd": behavior.charge,
                "provider_ref": effect_id or f"ref-{attempt_id}",
                "charge_ref": f"charge-{attempt_id}",
                "upstream_status": 200,
                "result": {"ticket_id": ticket, "label": "human"},
            }
            if "model" in configuration:
                response["provider_model"] = "provider-snapshot-v1"
                response["result"]["text"] = PRIVATE_OUTPUT
            self.outcomes[key, attempt_id] = response
            self.journal.append(
                {
                    "source": "dispatch",
                    "attempt_id": attempt_id,
                    "operation_key": key,
                    "operation_id": request["operation_id"],
                    "binding_id": request["binding_id"],
                    "revision": request["revision"],
                    "stream": request["stream"],
                    "model": configuration.get("model"),
                    "prompt": configuration.get("prompt"),
                    "max_output_tokens": configuration.get("max_output_tokens"),
                    "method": configuration.get("method"),
                    "server": configuration.get("server"),
                    "ticket": ticket,
                    "effect_created": effect_created,
                    "effect_id": effect_id,
                    "charge_microusd": behavior.charge,
                }
            )
            self.changed.notify_all()
            if behavior.mode in {"hold", "timeout"} and not self.changed.wait_for(
                lambda: ticket not in self.open_holds, timeout=15
            ):
                raise TimeoutError("fixture hold did not release")
        return behavior, response, ticket


class Handler(BaseHTTPRequestHandler):
    server: Oracle

    def log_message(self, *_: object) -> None:
        pass

    def transmit(self, data: bytes, *, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond(self, response: dict, *, status: int = 200) -> None:
        self.transmit(json.dumps(response).encode(), content_type="application/json", status=status)

    def do_POST(self) -> None:
        if self.path != "/dispatch" or self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.respond({"reason": "unrecognized route or credential"}, status=403)
            return
        size = int(self.headers.get("Content-Length", 0))
        if not 0 < size <= 65_536:
            self.respond({"reason": "invalid request size"}, status=400)
            return
        try:
            request = json.loads(self.rfile.read(size))
            behavior, observation, _ = self.server.acknowledge(request)
        except (KeyError, TypeError, ValueError):
            self.respond({"reason": "invalid dispatch protocol"}, status=400)
            return
        if behavior.mode in {"drop", "timeout"}:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if not request["stream"]:
            self.respond(observation)
            return
        frames = [
            {"type": "chunk", "delta": "first"},
            {"type": "chunk", "delta": "second"},
            {"type": "observation", "observation": observation},
        ]
        if behavior.mode == "partial-stream":
            frames = frames[:1]
        data = b"".join((json.dumps(frame) + "\n").encode() for frame in frames)
        self.transmit(data, content_type="application/x-ndjson")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path.strip("/").split("/")
        if len(path) == 2 and path[0] == "operations":
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self.respond({"reason": "invalid credential"}, status=403)
                return
            with self.server.changed:
                attempt_id = self.headers.get("X-Forge-Attempt", "")
                value = self.server.outcomes.get((path[1], attempt_id))
                self.server.journal.append(
                    {"source": "reconcile", "operation_key": path[1], "attempt_id": attempt_id}
                )
                if value is not None:
                    value = dict(value)
            self.respond(value or {"reason": "unknown operation"}, status=200 if value else 404)
        elif len(path) == 3 and path[0] == "queue":
            with self.server.changed:
                observed = self.server.queue.get(path[1], {}).get(path[2])
                self.server.journal.append(
                    {
                        "source": "support-queue",
                        "revision": path[1],
                        "ticket": path[2],
                        "observed": observed,
                    }
                )
            self.respond({"observed": observed})
        else:
            self.respond({"reason": "unknown route"}, status=404)
