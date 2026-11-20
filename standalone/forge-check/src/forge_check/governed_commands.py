"""CLI adapters for the trusted operator and the explicit dispatch host."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

from forge_check.operations_commands import OPERATIONS, add_operations, run_operation

if TYPE_CHECKING:
    import argparse
    from typing import TypeVar

    from forge_check.governed.control import Control
    from forge_check.governed.host import Host

    _Parser = TypeVar("_Parser", bound=argparse.ArgumentParser)


def add_commands(subcommands: argparse._SubParsersAction[_Parser]) -> None:
    descriptions = {
        "bootstrap": "Initialize one governed scope from protected host configuration.",
        "register": "Register an immutable revision.",
        "propose": "Record a human or automatic proposal.",
        "evaluate": "Evaluate protected ticket evidence against the active revision.",
        "promote": "Apply an authorized promotion at an expected alias generation.",
        "reject": "Record an explicit rejection at an expected alias generation.",
        "rollback": "Reactivate the previous revision at an expected alias generation.",
        "pause": "Pause admission of new root tasks for this scope.",
        "resume": "Resume admission of new root tasks for this scope.",
        "revoke": "Revoke a principal's future admissions and dispatch claims.",
        "wrap": "Dispatch a model or ticket operation in a pinned task.",
        "reconcile": "Read a provider operation to resolve an uncertain attempt.",
        "verify": "Inspect observed dispatch and alias decisions for a UTC window.",
    }
    for name, help_text in descriptions.items():
        command = subcommands.add_parser(name, help=help_text, allow_abbrev=False)
        command.add_argument("--db", type=Path, required=True, help="Local governed ledger")
        if name != "bootstrap":
            command.add_argument("--actor", required=True, help="Identity from the trusted host")
        if name == "bootstrap":
            command.add_argument("--config", type=Path, required=True)
        if name == "register":
            command.add_argument("--manifest", type=Path, required=True)
        if name in {"propose", "evaluate", "promote", "reject"}:
            command.add_argument("--proposal", required=True)
        if name == "propose":
            command.add_argument("--revision", required=True)
            command.add_argument("--source", choices=("human", "automatic"), default="human")
        if name in {"evaluate", "promote", "reject"}:
            command.add_argument("--evidence", required=True)
        if name == "evaluate":
            command.add_argument("--baseline", type=Path)
            command.add_argument("--candidate", type=Path, required=True)
            command.add_argument(
                "--observed-at", type=int, required=True, help="UTC Unix microseconds"
            )
        if name in {"evaluate", "promote", "reject", "rollback"}:
            command.add_argument("--generation", type=int, required=True)
        if name in {"promote", "reject", "rollback"}:
            command.add_argument("--decision", required=True)
        if name in {"pause", "resume", "revoke"}:
            command.add_argument("--event", required=True)
        if name == "revoke":
            command.add_argument("--principal", required=True)
        if name in {"wrap", "reconcile"}:
            command.add_argument("--provider", required=True, help="Operator-configured origin")
            command.add_argument("--timeout", type=float, default=5.0)
            command.add_argument("--attempt", required=True)
        if name == "wrap":
            command.add_argument("--task", required=True)
            command.add_argument("--worker", required=True)
            command.add_argument("--case")
            command.add_argument("--parent")
            command.add_argument("--alias-lease")
            command.add_argument("--binding", required=True)
            command.add_argument("--operation", required=True)
            command.add_argument("--retry-of")
            command.add_argument("--stream", action="store_true")
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--input")
            source.add_argument("--input-file", type=Path)
        if name == "verify":
            command.add_argument("--since", required=True)
            command.add_argument("--until", required=True)
            command.add_argument("--decision")
            command.add_argument("--roster", type=Path)
    add_operations(subcommands)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("Invalid JSON number")


def _input(text: str) -> dict[str, object]:
    value: object = json.loads(text, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return cast("dict[str, object]", value)


def _file(path: Path) -> dict[str, object]:
    return _input(path.read_text(encoding="utf-8"))


def _emit(data: object) -> None:
    print(json.dumps(data, sort_keys=True, allow_nan=False), flush=True)


def _invoke(options: argparse.Namespace, control: Control, host: Host) -> int:
    from forge_check.governed.ledger import Request

    payload = _file(options.input_file) if options.input_file else _input(options.input)
    control.start_task(
        options.actor,
        options.task,
        worker=options.worker,
        parent_id=options.parent,
        case_id=options.case,
        alias_lease=options.alias_lease,
    )
    request = Request(
        task_id=options.task,
        principal=options.actor,
        operation_id=options.operation,
        attempt_id=options.attempt,
        binding_id=options.binding,
        payload=payload,
        retry_of=options.retry_of,
    )
    if options.stream:
        terminal = None
        for frame in host.stream(request):
            if frame.type == "chunk":
                _emit({"type": "chunk", "delta": frame.delta})
            else:
                terminal = frame.invocation
        if terminal is None:
            raise RuntimeError("Stream closed without an attempt result")
    else:
        terminal = host.invoke(request)
    _emit(asdict(terminal))
    return 2 if terminal.disposition == "invalid_result" else _attempt_exit(terminal.attempt)


def _attempt_exit(attempt: dict[str, object]) -> int:
    status = attempt.get("status")
    if status in ("cached", "succeeded"):
        return 0
    return 1 if status in ("denied", "failed", "not_sent") else 2


def execute(options: argparse.Namespace) -> int:
    from forge_check.governed.control import Control
    from forge_check.governed.db import Store, StoreError
    from forge_check.governed.host import Host, HttpProvider
    from forge_check.governed.ledger import LedgerError
    from forge_check.governed.revision import Revision
    from forge_check.governed.verify import verify

    try:
        if options.command in OPERATIONS:
            _emit(run_operation(options, _file))
            return 0
        store = Store(options.db, create=options.command == "bootstrap")
        control = Control(store)
        result: object
        match options.command:
            case "bootstrap":
                config = _file(options.config)
                scope = cast("str", config["scope"])
                revision = Revision.parse(config["initial"])
                control.bootstrap(
                    scope,
                    revision,
                    cast("dict[str, object]", config["cases"]),
                    cast("dict[str, dict[str, list[str]]]", config["principals"]),
                    evidence_ttl_seconds=cast("int", config.get("evidence_ttl_seconds", 3600)),
                )
                result = {"scope": scope, "revision": revision.digest, "generation": 1}
            case "register":
                result = {
                    "revision": control.register(
                        options.actor, Revision.parse(_file(options.manifest))
                    )
                }
            case "propose":
                result = control.propose(
                    options.actor, options.proposal, options.revision, source=options.source
                )
            case "evaluate":
                report = control.evaluate(
                    options.actor,
                    options.evidence,
                    options.proposal,
                    _file(options.baseline) if options.baseline else None,
                    _file(options.candidate),
                    observed_at=options.observed_at,
                    generation=options.generation,
                )
                _emit(report)
                return {"pass": 0, "fail": 1, "insufficient": 2}.get(
                    str(report["report"]["verdict"]), 3
                )
            case "promote" | "reject" | "rollback":
                if options.command == "promote":
                    decision = control.promote(
                        options.actor,
                        options.decision,
                        options.proposal,
                        options.evidence,
                        options.generation,
                    )
                elif options.command == "rollback":
                    decision = control.rollback(options.actor, options.decision, options.generation)
                else:
                    decision = control.reject(
                        options.actor,
                        options.decision,
                        options.proposal,
                        options.evidence,
                        options.generation,
                    )
                _emit(decision)
                recorded = decision["status"] == "applied" or (
                    options.command == "reject" and decision["status"] == "rejected"
                )
                return 0 if recorded else 2
            case "pause" | "resume":
                control.set_paused(options.actor, options.event, options.command == "pause")
                result = {"event": options.event, "paused": options.command == "pause"}
            case "revoke":
                control.revoke(options.actor, options.event, options.principal)
                result = {"event": options.event, "revoked": options.principal}
            case "wrap" | "reconcile":
                key = bytes.fromhex(os.environ.get("FORGE_FINGERPRINT_KEY", ""))
                provider = HttpProvider(
                    options.provider,
                    token=os.environ.get("FORGE_PROVIDER_TOKEN"),
                    timeout=options.timeout,
                )
                host = Host(store, fingerprint_key=key, provider=provider)
                if options.command == "wrap":
                    return _invoke(options, control, host)
                reconciled = host.reconcile(options.actor, options.attempt)
                _emit(asdict(reconciled))
                return _attempt_exit(reconciled.attempt)
            case "verify":
                result = verify(
                    store,
                    options.actor,
                    since=options.since,
                    until=options.until,
                    decision_id=options.decision,
                    roster=_file(options.roster) if options.roster else None,
                )
            case _:
                raise ValueError("Unknown governed command")
        _emit(result)
        return 0
    except PermissionError:
        _emit({"error": "unauthorized"})
        return 1
    except (KeyError, TypeError, ValueError, RecursionError, LedgerError):
        print(
            "Invalid governed command or input. Check the command help and document schema.",
            file=sys.stderr,
        )
        return 3
    except (OSError, sqlite3.Error, StoreError):
        print("Governed operation could not access the local ledger or input.", file=sys.stderr)
        return 3
