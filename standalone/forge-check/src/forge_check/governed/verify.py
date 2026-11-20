"""Windowed evidence queries over the immutable receipt and decision records."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from forge_check.governed.control import require_role
from forge_check.governed.ledger import charge_total
from forge_check.governed.revision import identifier, integer, string_set

if TYPE_CHECKING:
    from sqlite3 import Connection

    from forge_check.governed.db import Store

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _micros(value: object) -> int:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Use an ISO-8601 UTC timestamp")
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() != timedelta(0):
        raise ValueError("Use an ISO-8601 UTC timestamp")
    elapsed = timestamp - _EPOCH
    return (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds


def _window(since: str, until: str) -> tuple[int, int]:
    start, end = _micros(since), _micros(until)
    if not 0 <= start < end:
        raise ValueError("Expected a nonempty UTC window after the Unix epoch")
    return start, end


def _stamp(micros: int) -> str:
    return (_EPOCH + timedelta(microseconds=micros)).isoformat(timespec="microseconds")


def _coverage(
    connection: Connection,
    actor: str,
    start: int,
    end: int,
    roster: dict[str, object] | None,
) -> dict[str, object]:
    rows = connection.execute(
        """SELECT DISTINCT t.worker FROM receipts r JOIN tasks t ON t.id = r.task_id
           WHERE r.recorded_at >= ? AND r.recorded_at < ? ORDER BY t.worker""",
        (start, end),
    )
    observed = {str(row[0]) for row in rows}
    expected: set[str] | None = None
    source = None
    if roster is not None:
        require_role(connection, actor, "evidence")
        if set(roster) != {"source", "since", "until", "workers"} or (
            _micros(roster["since"]),
            _micros(roster["until"]),
        ) != (start, end):
            raise ValueError("Roster must attest the same UTC window")
        source = identifier(roster["source"], "roster source")
        expected = set(string_set(roster["workers"], "worker ID"))
        if not expected:
            raise ValueError("A supplied roster must contain eligible workers")
    return {
        "observed_workers": sorted(observed),
        "expected_workers": None if expected is None else sorted(expected),
        "silent_workers": None if expected is None else sorted(expected - observed),
        "unexpected_workers": None if expected is None else sorted(observed - expected),
        "observed": len(observed if expected is None else observed & expected),
        "eligible": None if expected is None else len(expected),
        "roster_source": source,
        "roster_attested_by": actor if expected is not None else None,
    }


def _groups(connection: Connection, start: int, end: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """WITH observed AS (
               SELECT DISTINCT task_id FROM receipts WHERE recorded_at >= ? AND recorded_at < ?
           ), relevant AS (
               SELECT t.revision, t.generation, t.source, t.id, t.root_id,
                      root.started_at AS root_started, o.task_id IS NOT NULL AS observed
                 FROM tasks t JOIN tasks root ON root.id = t.root_id
                 LEFT JOIN observed o ON o.task_id = t.id
                WHERE o.task_id IS NOT NULL
                   OR (t.id = t.root_id AND t.started_at >= ? AND t.started_at < ?)
           )
           SELECT revision, generation, source,
                  COUNT(DISTINCT CASE WHEN id = root_id AND root_started >= ? THEN root_id END)
                      AS roots_started,
                  COUNT(DISTINCT CASE WHEN observed THEN id END) AS observed_tasks,
                  COUNT(DISTINCT CASE WHEN observed THEN root_id END) AS observed_roots,
                  COUNT(DISTINCT CASE WHEN observed AND root_started < ? THEN root_id END)
                      AS observed_earlier_roots
             FROM relevant GROUP BY revision, generation, source
            ORDER BY generation, revision, source""",
        (start, end, start, end, start, start),
    )
    return [dict(row) for row in rows]


def _unconfirmed(connection: Connection, start: int, end: int) -> list[dict[str, object]]:
    rows = connection.execute(
        """WITH relevant AS (
               SELECT DISTINCT attempt_id FROM receipts WHERE recorded_at >= ? AND recorded_at < ?
           ), latest AS (
               SELECT MAX(r.seq) AS seq
                 FROM receipts r JOIN relevant o ON o.attempt_id = r.attempt_id
                WHERE r.recorded_at < ? GROUP BY r.attempt_id
           )
           SELECT r.body FROM receipts r JOIN latest l ON l.seq = r.seq
            WHERE EXISTS (SELECT 1 FROM receipts d WHERE d.attempt_id = r.attempt_id
                            AND d.phase = 'dispatch_intent' AND d.recorded_at < ?)
            ORDER BY r.seq""",
        (start, end, end, end),
    )
    findings: list[dict[str, object]] = []
    for row in rows:
        receipt = json.loads(row[0])
        unknown_effect = receipt["confirmation"] != "verified" or receipt["status"] == "uncertain"
        unknown_charge = receipt["charge_microusd"] is None
        if unknown_effect or unknown_charge:
            findings.append(
                {
                    **{
                        key: receipt[key]
                        for key in (
                            "case_id",
                            "task_id",
                            "attempt_id",
                            "revision",
                            "status",
                            "source",
                        )
                    },
                    "effect_unconfirmed": unknown_effect,
                    "charge_unknown": unknown_charge,
                }
            )
    return findings


def verify(
    store: Store,
    actor: str,
    *,
    since: str,
    until: str,
    decision_id: str | None = None,
    roster: dict[str, object] | None = None,
) -> dict[str, object]:
    start, end = _window(since, until)
    with store.read() as connection:
        require_role(connection, actor, "inspect")
        scope = connection.execute("SELECT scope FROM scope_policy WHERE singleton = 1").fetchone()
        alias = connection.execute("SELECT * FROM aliases WHERE name = 'estable'").fetchone()
        if scope is None or alias is None:
            raise ValueError("Scope has not been bootstrapped")
        requested_decision = decision_id if decision_id is not None else alias["decision_id"]
        decision = None
        if requested_decision is not None:
            row = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (requested_decision,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown decision")
            decision = dict(row)
            decision["created_at"] = _stamp(int(row["created_at"]))
        groups = _groups(connection, start, end)
        mediated = ["direct"]
        if any(row["source"] == "code_mode" and row["observed_tasks"] for row in groups):
            mediated.append("code_mode")
        return {
            "schema_version": 1,
            "scope": str(scope["scope"]),
            "window": {"since": _stamp(start), "until": _stamp(end), "end_exclusive": True},
            "active_alias": dict(alias),
            "decision": decision,
            "by_revision": groups,
            "coverage": _coverage(connection, actor, start, end, roster),
            "routes": {
                "mediated": mediated,
                "observed": [],
                "unknown": ["outside_explicit_bindings"],
            },
            "unconfirmed_cases": _unconfirmed(connection, start, end),
        }


def compare_charges(
    store: Store,
    actor: str,
    *,
    since: str,
    until: str,
    billing_scope: str,
    provider_total_microusd: int,
) -> dict[str, object]:
    start, end = _window(since, until)
    binding_id = identifier(billing_scope, "billing binding")
    total = integer(provider_total_microusd, "provider total")
    with store.read() as connection:
        require_role(connection, actor, "reconcile")
        known = connection.execute(
            """SELECT 1 FROM aliases a JOIN revisions v ON v.digest = a.revision
               JOIN json_each(CAST(v.canonical AS TEXT), '$.bindings') b
               WHERE a.name = 'estable' AND b.key = ?""",
            (binding_id,),
        ).fetchone()
        if known is None:
            raise ValueError("Unknown billing binding")
        observed = charge_total(connection, since=start, until=end, binding_id=binding_id)
    return {
        "window": {"since": _stamp(start), "until": _stamp(end), "end_exclusive": True},
        "time_basis": "ledger_recorded_at",
        "billing_scope": billing_scope,
        "currency": "micro-USD",
        "observed_charge_microusd": observed,
        "provider_total_microusd": total,
        "difference_microusd": total - observed,
        "attribution": "aggregate_only",
    }
