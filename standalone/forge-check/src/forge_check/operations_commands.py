"""Operator entry points for rollout and ledger recovery."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from typing import TypeVar

    _Parser = TypeVar("_Parser", bound=argparse.ArgumentParser)


OPERATIONS = {
    "rollout-start": "Assign a bounded shadow or canary from approved evidence.",
    "rollout-stop": "Stop assigning new tasks to a rollout.",
    "rollout-status": "Read rollout configuration and exposure.",
    "task-finish": "Close a root task and account for its rollout exposure.",
    "alias-lease": "Issue a short-lived alias lease for the authenticated worker.",
    "revision-revoke": "Revoke future dispatches under a revision.",
    "backup": "Create a consistent ledger snapshot and print its manifest.",
    "restore": "Restore a verified snapshot to a new ledger under a recovery fence.",
    "migrate": "Preserve a snapshot and migrate the supported ledger format.",
    "recovery-status": "Inspect the fence and unresolved exposures.",
    "recovery-resume": "Attest reconciliation and resume the sole ledger writer.",
    "export": "Export minimized ledger evidence.",
}


def add_operations(subcommands: argparse._SubParsersAction[_Parser]) -> None:
    for name, description in OPERATIONS.items():
        command = subcommands.add_parser(name, help=description, allow_abbrev=False)
        command.add_argument("--db", type=Path, required=True)
        if name != "restore":
            command.add_argument("--actor", required=True)
        if name.startswith("rollout-"):
            command.add_argument("--rollout", required=True)
        if name in {"rollout-start", "rollout-stop"}:
            command.add_argument("--generation", required=True, type=int)
        if name == "rollout-start":
            command.add_argument("--proposal", required=True)
            command.add_argument("--evidence", required=True)
            command.add_argument("--mode", choices=("shadow", "canary"), required=True)
            command.add_argument("--source", default="direct")
            command.add_argument("--seed", required=True)
            command.add_argument("--basis-points", required=True, type=int)
            command.add_argument("--ends-at", required=True, type=int, help="UTC Unix microseconds")
            command.add_argument("--max-concurrent", required=True, type=int)
            command.add_argument("--budget-microusd", required=True, type=int)
        if name == "task-finish":
            command.add_argument("--task", required=True)
        if name == "alias-lease":
            command.add_argument("--lease", required=True)
            command.add_argument("--ttl-seconds", required=True, type=int)
        if name == "revision-revoke":
            command.add_argument("--event", required=True)
            command.add_argument("--revision", required=True)
        if name == "backup":
            command.add_argument("--output", type=Path, required=True)
        if name == "restore":
            command.add_argument("--snapshot", type=Path, required=True)
            command.add_argument("--manifest", type=Path, required=True)
        if name == "migrate":
            command.add_argument("--backup", type=Path, required=True)
        if name == "export":
            command.add_argument("--after-seq", type=int, default=0)
            command.add_argument("--limit", type=int, default=1_000)
        if name == "recovery-resume":
            command.add_argument("--recovery-id", required=True)
            command.add_argument("--audit-ref", required=True)
            command.add_argument("--covered-through", type=int, required=True)
            command.add_argument("--source-quiesced", action="store_true", required=True)
            command.add_argument("--post-snapshot-effects", type=int, required=True)


def run_operation(
    options: argparse.Namespace, read_file: Callable[[Path], dict[str, object]]
) -> object:
    from forge_check.governed import recovery
    from forge_check.governed.control import Control
    from forge_check.governed.db import Store

    action = options.command
    if action == "restore":
        return recovery.restore(options.snapshot, options.db, read_file(options.manifest))
    if action == "migrate":
        return recovery.migrate(options.db, options.actor, options.backup)

    store = Store(options.db, create=False)
    control = Control(store)
    actor = options.actor
    match action:
        case "rollout-start":
            return control.start_rollout(
                actor,
                options.rollout,
                options.proposal,
                options.evidence,
                options.generation,
                mode=options.mode,
                source=options.source,
                seed=options.seed,
                basis_points=options.basis_points,
                ends_at=options.ends_at,
                max_concurrent=options.max_concurrent,
                budget_microusd=options.budget_microusd,
            )
        case "rollout-stop":
            return control.stop_rollout(actor, options.rollout, options.generation)
        case "rollout-status":
            return control.get_rollout(actor, options.rollout)
        case "task-finish":
            return control.finish_task(actor, options.task)
        case "alias-lease":
            return control.issue_alias_lease(actor, options.lease, ttl_seconds=options.ttl_seconds)
        case "revision-revoke":
            control.revoke_revision(actor, options.event, options.revision)
            return {"revision": options.revision, "event": options.event}
        case "backup":
            return recovery.backup(store, actor, options.output)
        case "recovery-status":
            return recovery.recovery_status(store, actor)
        case "recovery-resume":
            return recovery.resume(
                store,
                actor,
                options.recovery_id,
                audit_ref=options.audit_ref,
                covered_through_us=options.covered_through,
                source_quiesced=options.source_quiesced,
                post_snapshot_effects=options.post_snapshot_effects,
            )
        case "export":
            return recovery.export_evidence(
                store, actor, after_seq=options.after_seq, limit=options.limit
            )
        case _:
            raise ValueError("Unknown operations command")
