"""Durable SQLite transactions for a single protected Forge scope."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_APPLICATION_ID = 0x464F5247  # FORG
_SCHEMA_VERSION = 1
_BUSY_SECONDS = 5.0


class StoreError(RuntimeError):
    """The ledger could not be opened with the expected identity and schema."""


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
        if not self.path.is_file():
            raise StoreError("The ledger path must be a regular file")
        self._initialize(create=create)

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=_BUSY_SECONDS
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _identity(connection: sqlite3.Connection) -> tuple[int, int]:
        return (
            int(connection.execute("PRAGMA application_id").fetchone()[0]),
            int(connection.execute("PRAGMA user_version").fetchone()[0]),
        )

    def _initialize(self, *, create: bool) -> None:
        with closing(self._connection()) as connection:
            connection.execute("BEGIN")
            identity = self._identity(connection)
            if identity == (_APPLICATION_ID, _SCHEMA_VERSION):
                return
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
            if self._identity(connection) == (_APPLICATION_ID, _SCHEMA_VERSION):
                return  # Another process initialized the empty ledger.
            if (
                self._identity(connection) != (0, 0)
                or connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
            ):
                raise StoreError("Expected a new Forge database")

            from forge_check.governed import control, ledger

            for statement in (*control.SCHEMA, *ledger.SCHEMA):
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

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
