"""Consistent backup, explicit migration, and fenced single-host recovery."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from forge_check.governed import rollout
from forge_check.governed.control import require_role
from forge_check.governed.db import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    StoreError,
    install_v2_schema,
    install_v3_schema,
    ledger_identity,
    open_ledger,
    require_dispatch_authority,
)
from forge_check.governed.revision import identifier, integer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection, Row

    from forge_check.governed.db import Store

_MANIFEST_KEYS = {
    "manifest_version",
    "schema_version",
    "scope",
    "captured_at_us",
    "receipt_high_water",
    "alias_generation",
    "sha256",
}
_RECEIPT_FIELDS = (
    "scope",
    "root_task_id",
    "parent_task_id",
    "operation_id",
    "binding_id",
    "revision",
    "generation",
    "status",
    "confirmation",
    "provider_ref",
    "reserved_microusd",
    "charge_microusd",
    "currency",
)


def _now() -> int:
    return time.time_ns() // 1_000


def _integrity(connection: Connection) -> None:
    states = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if states != ["ok"] or connection.execute("PRAGMA foreign_key_check").fetchone():
        raise StoreError("Ledger integrity or foreign key check failed")


def _state(connection: Connection) -> Row:
    row: Row | None = connection.execute(
        "SELECT * FROM recovery_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise StoreError("Missing recovery state")
    return row


def _snapshot_state(connection: Connection) -> dict[str, object]:
    app, schema = ledger_identity(connection)
    if app != APPLICATION_ID or schema not in {1, 2, SCHEMA_VERSION}:
        raise StoreError("Unsupported ledger snapshot")
    row = connection.execute(
        """SELECT p.scope, a.generation, COALESCE((SELECT MAX(seq) FROM receipts), 0)
           FROM scope_policy p, aliases a
           WHERE p.singleton = 1 AND a.name = 'estable'"""
    ).fetchone()
    if row is None:
        raise StoreError("Snapshot requires a bootstrapped scope")
    return {
        "schema_version": schema,
        "scope": str(row[0]),
        "alias_generation": int(row[1]),
        "receipt_high_water": int(row[2]),
    }


def _unresolved(connection: Connection) -> int:
    row = connection.execute(
        """SELECT COUNT(*) FROM attempts
           WHERE reserved_microusd > 0 OR status IN ('reserved', 'dispatching', 'uncertain')
              OR (dispatched_at IS NOT NULL
                  AND (confirmation != 'verified' OR charge_microusd IS NULL))"""
    ).fetchone()
    return int(row[0])


@contextmanager
def _temporary(destination: Path) -> Iterator[Path]:
    # Stage next to the destination for atomic, exclusive publication.
    fd, name = tempfile.mkstemp(prefix=".forge-stage-", dir=destination.parent)
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def _publish(staged: Path, destination: Path) -> None:
    with staged.open("rb") as handle:
        os.fsync(handle.fileno())
    os.link(staged, destination)
    if hasattr(os, "O_DIRECTORY"):
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write_backup(
    source: Connection,
    destination: Path,
    captured_at_us: int,
) -> dict[str, object]:
    # The caller captured the time before its first read pinned the SQLite snapshot.
    metadata = _snapshot_state(source)
    deadline = time.monotonic() + 30

    def progress(_code: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise StoreError("Online backup exceeded its deadline")

    with _temporary(destination) as temporary:
        with closing(open_ledger(temporary)) as target:
            source.backup(target, pages=256, sleep=0.05, progress=progress)
            if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise StoreError("Could not seal backup into a single file")
            _integrity(target)
            if _snapshot_state(target) != metadata:
                raise StoreError("Backup snapshot identity changed")
        sha256 = _sha256(temporary)
        _publish(temporary, destination)
    return {
        "manifest_version": 1,
        "captured_at_us": captured_at_us,
        "sha256": sha256,
        **metadata,
    }


def backup(store: Store, actor: str, destination: Path) -> dict[str, object]:
    """Create a consistent, standalone backup of an authoritative ledger."""
    with store.read() as source:
        captured_at_us = _now()
        require_role(source, actor, "admin")
        require_dispatch_authority(source)
        return _write_backup(source, destination, captured_at_us)


def migrate(
    path: Path,
    actor: str,
    backup_path: Path,
) -> dict[str, object]:
    """Back up a quiesced ledger, then migrate it and fence older writers."""
    with closing(open_ledger(path)) as writer, writer:
        writer.execute("BEGIN IMMEDIATE")
        app, schema = ledger_identity(writer)
        if app != APPLICATION_ID or schema not in {1, 2}:
            raise StoreError("Migration requires a supported v1 or v2 ledger")
        if writer.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            raise StoreError("Migration requires an authoritative WAL ledger")
        require_role(writer, actor, "admin")
        with closing(open_ledger(path)) as source:
            source.execute("PRAGMA query_only=ON")
            source.execute("BEGIN")
            manifest = _write_backup(source, backup_path, _now())
        if schema == 1:
            install_v2_schema(writer)
        install_v3_schema(writer)
    return manifest


def restore(
    snapshot: Path,
    destination: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    """Restore a known snapshot to a fresh path with dispatch authority fenced."""
    if set(manifest) != _MANIFEST_KEYS:
        raise ValueError("Invalid backup manifest fields")
    integer(manifest["manifest_version"], "manifest version", minimum=1, maximum=1)
    expected_sha = manifest["sha256"]
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("Backup manifest requires an expected SHA-256")
    captured_at = integer(manifest["captured_at_us"], "backup capture time", minimum=1)
    recovered_at = _now()
    snapshot_metadata = {
        "schema_version": integer(
            manifest["schema_version"], "schema version", minimum=1, maximum=SCHEMA_VERSION
        ),
        "scope": identifier(manifest["scope"], "scope"),
        "alias_generation": integer(manifest["alias_generation"], "alias generation", minimum=1),
        "receipt_high_water": integer(manifest["receipt_high_water"], "receipt high-water"),
    }
    if captured_at > recovered_at:
        raise ValueError("Backup capture time is in the future")
    if snapshot.is_symlink() or not snapshot.is_file():
        raise StoreError("Expected a regular sealed SQLite backup")

    recovery_id = uuid4().hex
    with _temporary(destination) as temporary:
        shutil.copyfile(snapshot, temporary)
        if not hmac.compare_digest(_sha256(temporary), expected_sha):
            raise StoreError("Backup does not match its expected SHA-256")
        with closing(open_ledger(temporary)) as connection:
            _integrity(connection)
            if _snapshot_state(connection) != snapshot_metadata:
                raise StoreError("Backup manifest does not match the ledger contents")
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise StoreError("Restore requires a sealed single-file backup")
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise StoreError("The restored ledger requires WAL on local disk")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if snapshot_metadata["schema_version"] == 1:
                    install_v2_schema(connection)
                else:
                    require_dispatch_authority(connection)
                if snapshot_metadata["schema_version"] != SCHEMA_VERSION:
                    install_v3_schema(connection)
                # Projection authority is lab-only and must be explicitly re-established.
                rollout.set_capacity_mode(connection, "reference")
                recovered_at = _now()
                rollout.invalidate_leases(connection, recovered_at)
                rollout.stop_open(connection, None, recovered_at, "recovery_fenced")
                connection.execute("DELETE FROM read_cache")
                connection.execute(
                    """UPDATE recovery_state
                       SET open = 0, recovery_id = ?, snapshot_sha256 = ?,
                           captured_at_us = ?, restored_at_us = ?
                       WHERE singleton = 1""",
                    (recovery_id, expected_sha, captured_at, recovered_at),
                )
                unresolved = _unresolved(connection)
            if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise StoreError("Could not checkpoint the fenced copy")
        _publish(temporary, destination)
    return {
        **snapshot_metadata,
        "schema_version": SCHEMA_VERSION,
        "recovery_id": recovery_id,
        "fenced": True,
        "snapshot_sha256": expected_sha,
        "captured_at_us": captured_at,
        "restored_at_us": recovered_at,
        "unresolved_attempts": unresolved,
    }


def _status(connection: Connection) -> dict[str, object]:
    state = _state(connection)
    certificate = connection.execute(
        "SELECT * FROM recovery_certificates WHERE recovery_id = ?",
        (state["recovery_id"],),
    ).fetchone()
    return {
        **_snapshot_state(connection),
        "fenced": not bool(state["open"]),
        "recovery_id": state["recovery_id"],
        "captured_at_us": state["captured_at_us"],
        "restored_at_us": state["restored_at_us"],
        "snapshot_sha256": state["snapshot_sha256"],
        "unresolved_attempts": _unresolved(connection),
        "certificate": dict(certificate) if certificate is not None else None,
    }


def recovery_status(store: Store, actor: str) -> dict[str, object]:
    with store.read() as connection:
        require_role(connection, actor, "inspect")
        return _status(connection)


def resume(
    store: Store,
    actor: str,
    recovery_id: str,
    *,
    audit_ref: str,
    covered_through_us: int,
    source_quiesced: bool,
    post_snapshot_effects: int,
) -> dict[str, object]:
    """Reopen authority after the operator certifies a zero-loss external audit."""
    identifier(recovery_id, "recovery ID")
    identifier(audit_ref, "external audit reference")
    covered = integer(covered_through_us, "coverage end", minimum=1)
    effects = integer(post_snapshot_effects, "post-snapshot activity")
    if source_quiesced is not True or effects:
        raise StoreError("Recovery requires a quiesced writer and a reconciled external window")
    with store.write() as connection:
        require_role(connection, actor, "admin")
        state = _state(connection)
        if state["recovery_id"] != recovery_id:
            raise ValueError("Recovery ID does not match this ledger")
        if state["open"]:
            prior = connection.execute(
                """SELECT 1 FROM recovery_certificates
                   WHERE recovery_id = ? AND actor = ? AND audit_ref = ?
                     AND covered_through_us = ?""",
                (recovery_id, actor, audit_ref, covered),
            ).fetchone()
            if prior is None:
                raise StoreError("Recovery already completed under a different certificate")
            return _status(connection)
        now = _now()
        if not state["restored_at_us"] <= covered <= now:
            raise ValueError("External audit must cover through restoration")
        if _unresolved(connection):
            raise StoreError("Unresolved attempts still fence dispatch authority")
        connection.execute(
            """INSERT INTO recovery_certificates
               (recovery_id, actor, audit_ref, covered_through_us, resumed_at_us)
               VALUES (?, ?, ?, ?, ?)""",
            (recovery_id, actor, audit_ref, covered, now),
        )
        connection.execute("UPDATE recovery_state SET open = 1 WHERE singleton = 1")
        return _status(connection)


def export_evidence(
    store: Store,
    actor: str,
    *,
    after_seq: int = 0,
    limit: int = 1_000,
) -> dict[str, object]:
    """Export bounded evidence without manifests, raw cache contents, or operation keys."""
    after = integer(after_seq, "receipt cursor")
    size = integer(limit, "page size", minimum=1, maximum=10_000)
    with store.read() as connection:
        require_role(connection, actor, "inspect")
        page = connection.execute(
            """SELECT seq, event_id, task_id, attempt_id, phase, recorded_at, body
               FROM receipts WHERE seq > ? ORDER BY seq LIMIT ?""",
            (after, size + 1),
        ).fetchall()
        decisions = connection.execute(
            "SELECT * FROM decisions ORDER BY created_at DESC, rowid DESC LIMIT ?", (size + 1,)
        ).fetchall()
        receipts: list[dict[str, object]] = []
        for row in page[:size]:
            receipt = dict(row)
            raw = json.loads(receipt.pop("body"))
            receipt.update({name: raw.get(name) for name in _RECEIPT_FIELDS})
            receipts.append(receipt)
        return {
            "recovery": _status(connection),
            "receipts": receipts,
            "next_after_seq": int(page[size - 1]["seq"]) if len(page) > size else None,
            "decisions": [dict(row) for row in decisions[:size]],
            "decisions_truncated": len(decisions) > size,
        }
