"""Transactional closed charge and open membership for canary capacity."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import TYPE_CHECKING, Literal, TypeAlias

from forge_check.governed.db import StoreError, exact_amount, exact_sum_sql

if TYPE_CHECKING:
    from sqlite3 import Connection

Capacity: TypeAlias = tuple[int, int, int]
CapacityMode: TypeAlias = Literal["reference", "compare", "projection"]

REFERENCE_CAPACITY_SQL = """SELECT r.allocated_microusd, f.root_id IS NULL AS active,
    COALESCE((SELECT SUM(charge_microusd) FROM attempts a WHERE a.root_id = r.root_id), 0) AS charge
    FROM rollout_allocations r LEFT JOIN finished_roots f ON f.root_id = r.root_id
    WHERE r.rollout_id = ? AND r.cohort = 'canary'"""

OPEN_CAPACITY_SQL = """SELECT r.allocated_microusd,
    r.rollout_id AS canonical_rollout, r.cohort, f.root_id AS finished,
    COALESCE((SELECT SUM(charge_microusd) FROM attempts a WHERE a.root_id = o.root_id), 0) AS charge
    FROM rollout_capacity_open o
    LEFT JOIN rollout_allocations r ON r.root_id = o.root_id
    LEFT JOIN finished_roots f ON f.root_id = o.root_id
    WHERE o.rollout_id = ?"""

SCHEMA = (
    """CREATE TABLE rollout_capacity_settings (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        mode TEXT NOT NULL CHECK (mode IN ('reference', 'compare', 'projection'))
    )""",
    "INSERT INTO rollout_capacity_settings VALUES (1, 'reference')",
    """CREATE TABLE rollout_capacity_closed (
        rollout_id TEXT PRIMARY KEY NOT NULL REFERENCES rollouts(id),
        charge_microusd TEXT NOT NULL,
        open_count INTEGER NOT NULL CHECK (open_count >= 0),
        checksum TEXT NOT NULL
    )""",
    """CREATE TABLE rollout_capacity_open (
        root_id TEXT PRIMARY KEY NOT NULL REFERENCES rollout_allocations(root_id),
        rollout_id TEXT NOT NULL REFERENCES rollouts(id)
    )""",
    "CREATE INDEX rollout_capacity_open_by_rollout ON rollout_capacity_open(rollout_id, root_id)",
    # Terminal observations can still append receipts and suspend bindings after close.
    # Only a change to the monetary contribution would invalidate the closed aggregate.
    """CREATE TRIGGER finished_root_charge_insert BEFORE INSERT ON attempts
       WHEN COALESCE(NEW.charge_microusd, 0) != 0 AND
            EXISTS (SELECT 1 FROM finished_roots WHERE root_id = NEW.root_id)
       BEGIN SELECT RAISE(ABORT, 'Cannot add charge to a finished root'); END""",
    """CREATE TRIGGER finished_root_charge_update BEFORE UPDATE OF charge_microusd ON attempts
       WHEN COALESCE(NEW.charge_microusd, 0) != COALESCE(OLD.charge_microusd, 0) AND
            EXISTS (SELECT 1 FROM finished_roots WHERE root_id = OLD.root_id)
       BEGIN SELECT RAISE(ABORT, 'Cannot change charge for a finished root'); END""",
    """CREATE TRIGGER finished_root_charge_delete BEFORE DELETE ON attempts
       WHEN COALESCE(OLD.charge_microusd, 0) != 0 AND
            EXISTS (SELECT 1 FROM finished_roots WHERE root_id = OLD.root_id)
       BEGIN SELECT RAISE(ABORT, 'Cannot remove charge for a finished root'); END""",
)


class ProjectionError(StoreError):
    """The capacity projection cannot safely authorize canary work."""


def reference_capacity(conn: Connection, rollout_id: str) -> Capacity:
    """Recompute from the canonical root allocations, attempts and finish records."""
    try:
        return _reference_totals(conn, rollout_id, REFERENCE_CAPACITY_SQL)
    except sqlite3.OperationalError as error:
        return _reference_totals(conn, rollout_id, exact_sum_sql(REFERENCE_CAPACITY_SQL, error))


def _reference_totals(conn: Connection, rollout_id: str, query: str) -> Capacity:
    active = consumed = exposure = 0
    for row in conn.execute(query, (rollout_id,)):
        charge = exact_amount(row["charge"])
        consumed += charge
        if row["active"]:
            active += 1
            exposure += max(exact_amount(row["allocated_microusd"]), charge)
        else:
            exposure += charge
    return active, consumed, exposure


def _checksum(rollout_id: str, charge: str, open_count: int) -> str:
    content = f"{len(rollout_id)}:{rollout_id}:{charge}:{open_count}"
    return hashlib.sha256(content.encode()).hexdigest()


def _totals_values(rollout_id: str, charge: int, open_count: int) -> tuple[int, str, str]:
    exact = str(charge)
    return open_count, exact, _checksum(rollout_id, exact, open_count)


def _closed(conn: Connection, rollout_id: str) -> tuple[int, int]:
    row = conn.execute(
        """SELECT open_count, charge_microusd, checksum FROM rollout_capacity_closed
           WHERE rollout_id = ?""",
        (rollout_id,),
    ).fetchone()
    if row is not None:
        count, raw, checksum = row
        if (
            type(count) is int
            and count >= 0
            and isinstance(raw, str)
            and 0 < len(raw) <= 40
            and raw.isascii()
            and raw.isdecimal()
            and (raw == "0" or raw[0] != "0")
            and checksum == _checksum(rollout_id, raw, count)
        ):
            return count, int(raw)
    raise ProjectionError(f"Missing or invalid capacity totals for rollout {rollout_id}")


def projected_capacity(conn: Connection, rollout_id: str) -> Capacity:
    """Read the closed aggregate and compute contributions only for open roots."""
    active, closed = _closed(conn, rollout_id)
    observed, open_charge, open_exposure = _open_capacity(conn, rollout_id)
    if observed != active:
        raise ProjectionError(f"Invalid open root count for rollout {rollout_id}")
    return active, closed + open_charge, closed + open_exposure


def _open_capacity(conn: Connection, rollout_id: str) -> Capacity:
    try:
        return _open_totals(conn, rollout_id, OPEN_CAPACITY_SQL)
    except sqlite3.OperationalError as error:
        return _open_totals(conn, rollout_id, exact_sum_sql(OPEN_CAPACITY_SQL, error))


def _open_totals(conn: Connection, rollout_id: str, query: str) -> Capacity:
    active = consumed = exposure = 0
    for row in conn.execute(query, (rollout_id,)):
        if (
            row["canonical_rollout"] != rollout_id
            or row["cohort"] != "canary"
            or row["finished"] is not None
        ):
            raise ProjectionError(f"Invalid open root membership for rollout {rollout_id}")
        charge = exact_amount(row["charge"])
        active += 1
        consumed += charge
        exposure += max(exact_amount(row["allocated_microusd"]), charge)
    return active, consumed, exposure


def _compare(conn: Connection, rollout_id: str) -> Capacity:
    reference = reference_capacity(conn, rollout_id)
    projected = projected_capacity(conn, rollout_id)
    if reference != projected:
        raise ProjectionError(
            f"Capacity divergence for rollout {rollout_id}: {reference=} {projected=}"
        )
    return reference


def _validate(conn: Connection) -> None:
    missing = conn.execute(
        """SELECT 1
             FROM rollout_allocations a
             LEFT JOIN finished_roots f ON a.root_id = f.root_id
             LEFT JOIN rollout_capacity_open p ON a.root_id = p.root_id
            WHERE a.cohort = 'canary' AND f.root_id IS NULL
              AND (p.root_id IS NULL OR p.rollout_id != a.rollout_id)
            LIMIT 1"""
    ).fetchone()
    if missing is not None:
        raise ProjectionError("The capacity projection is missing an open canary root")
    for row in conn.execute("SELECT id FROM rollouts"):
        _compare(conn, row[0])


def set_capacity_mode(conn: Connection, mode: CapacityMode) -> None:
    """Select the calculator transactionally on an isolated, operator-owned lab ledger."""
    if not conn.in_transaction:
        raise StoreError("Setting capacity mode requires a ledger write transaction")
    if mode not in {"reference", "compare", "projection"}:
        raise ValueError("Unsupported rollout capacity mode")
    if mode != "reference":
        _validate(conn)
    conn.execute(
        """INSERT INTO rollout_capacity_settings VALUES (1, ?)
           ON CONFLICT(singleton) DO UPDATE SET mode = excluded.mode""",
        (mode,),
    )


def capacity(conn: Connection, rollout_id: str) -> Capacity:
    """Select the ledger's calculator, falling back to canonical state on invalid projection."""
    row = conn.execute("SELECT mode FROM rollout_capacity_settings WHERE singleton = 1").fetchone()
    mode = row[0] if row else "reference"
    if mode == "compare":
        return _compare(conn, rollout_id)
    if mode == "projection":
        try:
            return projected_capacity(conn, rollout_id)
        except ProjectionError:
            return reference_capacity(conn, rollout_id)
    return reference_capacity(conn, rollout_id)


def register_rollout(conn: Connection, rollout_id: str) -> None:
    count, charge, checksum = _totals_values(rollout_id, 0, 0)
    conn.execute(
        "INSERT INTO rollout_capacity_closed VALUES (?, ?, ?, ?)",
        (rollout_id, charge, count, checksum),
    )


def _write_totals(conn: Connection, rollout_id: str, closed: int, active: int) -> None:
    conn.execute(
        """UPDATE rollout_capacity_closed SET open_count = ?, charge_microusd = ?, checksum = ?
           WHERE rollout_id = ?""",
        (*_totals_values(rollout_id, closed, active), rollout_id),
    )


def _invalidate(conn: Connection, rollout_id: str) -> None:
    # Keep canonical closure and settlement available. A missing totals row triggers reference.
    conn.execute("DELETE FROM rollout_capacity_closed WHERE rollout_id = ?", (rollout_id,))


def record_open(conn: Connection, rollout_id: str, root_id: str) -> None:
    try:
        active, closed = _closed(conn, rollout_id)
    except ProjectionError:
        _invalidate(conn, rollout_id)
        return
    conn.execute("INSERT INTO rollout_capacity_open VALUES (?, ?)", (root_id, rollout_id))
    _write_totals(conn, rollout_id, closed, active + 1)


def record_finish(conn: Connection, root_id: str) -> None:
    row = conn.execute(
        "SELECT rollout_id FROM rollout_allocations WHERE root_id = ? AND cohort = 'canary'",
        (root_id,),
    ).fetchone()
    if row is None:
        return
    rollout_id = row[0]
    removed = conn.execute(
        "DELETE FROM rollout_capacity_open WHERE root_id = ? AND rollout_id = ?",
        (root_id, rollout_id),
    ).rowcount
    try:
        active, closed = _closed(conn, rollout_id)
    except ProjectionError:
        _invalidate(conn, rollout_id)
        return
    if removed != 1 or active == 0:
        _invalidate(conn, rollout_id)
        return
    query = "SELECT COALESCE(SUM(charge_microusd), 0) FROM attempts WHERE root_id = ?"
    try:
        charge = exact_amount(conn.execute(query, (root_id,)).fetchone()[0])
    except sqlite3.OperationalError as error:
        charge = exact_amount(conn.execute(exact_sum_sql(query, error), (root_id,)).fetchone()[0])
    _write_totals(conn, rollout_id, closed + charge, active - 1)


def backfill(conn: Connection) -> None:
    """Populate an empty projection from a coherent snapshot before recording v3."""
    conn.execute(
        """INSERT INTO rollout_capacity_open
           SELECT r.root_id, r.rollout_id FROM rollout_allocations r
           LEFT JOIN finished_roots f ON f.root_id = r.root_id
           WHERE r.cohort = 'canary' AND f.root_id IS NULL"""
    )
    for row in conn.execute("SELECT id FROM rollouts"):
        rollout_id = row[0]
        active, consumed, _ = reference_capacity(conn, rollout_id)
        _, open_charge, _ = _open_capacity(conn, rollout_id)
        register_rollout(conn, rollout_id)
        _write_totals(conn, rollout_id, consumed - open_charge, active)
    _validate(conn)
