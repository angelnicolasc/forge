"""Durable SQLite transactions for a single protected Forge scope."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

APPLICATION_ID = 0x464F5247  # FORG
SCHEMA_VERSION = 3
_BUSY_SECONDS = 5.0

_RECOVERY_SCHEMA = (
    """CREATE TABLE recovery_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        open INTEGER NOT NULL CHECK (open IN (0, 1)),
        recovery_id TEXT,
        snapshot_sha256 TEXT,
        captured_at_us INTEGER,
        restored_at_us INTEGER
    )""",
    "INSERT INTO recovery_state (singleton, open) VALUES (1, 1)",
    """CREATE TABLE recovery_certificates (
        recovery_id TEXT PRIMARY KEY,
        actor TEXT NOT NULL REFERENCES principals(id),
        audit_ref TEXT NOT NULL,
        covered_through_us INTEGER NOT NULL,
        resumed_at_us INTEGER NOT NULL
    )""",
)


class StoreError(RuntimeError):
    """The ledger could not be opened with the expected identity and schema."""


class _ExactSum:
    """Accumulate beyond SQLite's signed 64-bit limit and return decimal text."""

    def __init__(self) -> None:
        self.total = 0

    def step(self, value: int | None) -> None:
        if value is not None:
            if type(value) is not int:
                raise ValueError("Expected integer ledger amounts")
            self.total += value

    def finalize(self) -> str:
        return str(self.total)


def exact_sum_sql(query: str, error: sqlite3.OperationalError) -> str:
    """Select the unbounded aggregate only after SQLite reports integer overflow."""
    if str(error) != "integer overflow":
        raise error
    return query.replace("SUM(", "forge_exact_sum(")


def exact_amount(value: object) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str):
        return int(value)  # forge_exact_sum returns decimal text.
    raise StoreError("Invalid non-integer amount in the canonical ledger")


def open_ledger(path: Path) -> sqlite3.Connection:
    """Open an existing ledger with the writer's connection settings."""
    if path.is_symlink() or not path.is_file():
        raise StoreError("Expected a regular ledger file in a protected directory")
    connection = sqlite3.connect(
        f"{path.absolute().as_uri()}?mode=rw",
        uri=True,
        isolation_level=None,
        timeout=_BUSY_SECONDS,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.create_function("forge_v3_writer", 0, lambda: 1, deterministic=True)
        connection.create_aggregate("forge_exact_sum", 1, _ExactSum)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection
    except BaseException:
        connection.close()
        raise


def ledger_identity(connection: sqlite3.Connection) -> tuple[int, int]:
    return (
        int(connection.execute("PRAGMA application_id").fetchone()[0]),
        int(connection.execute("PRAGMA user_version").fetchone()[0]),
    )


def install_v2_schema(connection: sqlite3.Connection) -> None:
    """Apply the additive v2 schema within the caller's SQLite transaction."""
    from forge_check.governed import code_mode, rollout

    for statement in (*_RECOVERY_SCHEMA, *rollout.SCHEMA, *code_mode.SCHEMA):
        connection.execute(statement)
    connection.execute("PRAGMA user_version=2")


def install_v3_schema(connection: sqlite3.Connection) -> None:
    """Backfill capacity and exclude connections whose binary predates the projection."""
    from forge_check.governed import rollout_projection

    for statement in rollout_projection.SCHEMA:
        connection.execute(statement)
    rollout_projection.backfill(connection)
    # The prior Store validated the schema only when it was constructed. Gate its
    # already-live writers at SQLite statement preparation, after they acquire the lock.
    for table in (
        "alias_leases",
        "aliases",
        "attempt_observations",
        "attempts",
        "binding_suspensions",
        "charge_claims",
        "code_programs",
        "control_events",
        "decisions",
        "finished_roots",
        "gate_evidence",
        "leased_roots",
        "principals",
        "proposals",
        "read_cache",
        "receipts",
        "recovery_certificates",
        "recovery_state",
        "revisions",
        "revoked_revisions",
        "rollout_allocations",
        "rollouts",
        "scope_policy",
        "tasks",
    ):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            connection.execute(
                f"""CREATE TRIGGER writer_v3_{table}_{operation.lower()}
                    BEFORE {operation} ON {table} WHEN forge_v3_writer() != 1
                    BEGIN SELECT RAISE(ABORT, 'Ledger requires a schema v3 writer'); END"""
            )
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def require_dispatch_authority(connection: sqlite3.Connection) -> None:
    """Fence new writes with dispatch or release authority during restore."""
    state = connection.execute("SELECT open FROM recovery_state WHERE singleton = 1").fetchone()
    if state is None or state[0] != 1:
        raise StoreError("Ledger is fenced pending external reconciliation")


class Store:
    """Open a writer and read snapshots on local disk, with one connection per transaction."""

    def __init__(self, path: str | Path, *, create: bool = True) -> None:
        self.path = Path(path).absolute()
        if self.path.is_symlink():
            raise StoreError("Use a ledger path inside a protected local directory")
        if create:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        self._initialize(create=create)

    def _connection(self) -> sqlite3.Connection:
        return open_ledger(self.path)

    def _initialize(self, *, create: bool) -> None:
        with closing(self._connection()) as connection:
            connection.execute("BEGIN")
            identity = ledger_identity(connection)
            if identity == (APPLICATION_ID, SCHEMA_VERSION):
                if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    raise StoreError("Restore snapshots through the fenced recovery path")
                return
            if identity in {(APPLICATION_ID, 1), (APPLICATION_ID, 2)}:
                raise StoreError(f"Schema v{identity[1]} requires an explicit migration and backup")
            if (
                not create
                or identity != (0, 0)
                or connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
            ):
                raise StoreError("Unsupported Forge ledger or schema version")
            connection.rollback()
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise StoreError("The ledger requires WAL on local disk")

        with self.write() as connection:
            if ledger_identity(connection) == (APPLICATION_ID, SCHEMA_VERSION):
                return  # Another process initialized the empty ledger.
            if (
                ledger_identity(connection) != (0, 0)
                or connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
            ):
                raise StoreError("Expected a new Forge database")

            from forge_check.governed import control, ledger

            for statement in (*control.SCHEMA, *ledger.SCHEMA):
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            install_v2_schema(connection)
            install_v3_schema(connection)

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connection()) as connection, connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection
