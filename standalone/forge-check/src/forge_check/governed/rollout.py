"""Transaction-local rollout planning and authorization for pinned root tasks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from forge_check.governed import rollout_projection
from forge_check.governed.revision import canonical_json, certificate_verifier

if TYPE_CHECKING:
    from sqlite3 import Connection, Row

RolloutMode = Literal["shadow", "canary"]
MAX_LEASE_SECONDS = 300
MAX_ROLLOUT_MICROS = 86_400_000_000

REFERENCE_CAPACITY_SQL = rollout_projection.REFERENCE_CAPACITY_SQL
capacity = rollout_projection.capacity
projected_capacity = rollout_projection.projected_capacity
reference_capacity = rollout_projection.reference_capacity
set_capacity_mode = rollout_projection.set_capacity_mode

SCHEMA = (
    """CREATE TABLE rollouts (
        id TEXT PRIMARY KEY NOT NULL, actor TEXT NOT NULL REFERENCES principals(id),
        mode TEXT NOT NULL CHECK (mode IN ('shadow','canary')), source TEXT NOT NULL,
        seed TEXT NOT NULL, basis_points INTEGER NOT NULL CHECK (basis_points BETWEEN 1 AND 10000),
        generation INTEGER NOT NULL, stable_revision TEXT NOT NULL REFERENCES revisions(digest),
        candidate_revision TEXT NOT NULL REFERENCES revisions(digest),
        proposal_id TEXT NOT NULL REFERENCES proposals(id),
        evidence_id TEXT NOT NULL REFERENCES gate_evidence(id),
        created_at INTEGER NOT NULL, ends_at INTEGER NOT NULL CHECK (ends_at > created_at),
        max_concurrent INTEGER NOT NULL CHECK (max_concurrent > 0),
        budget_microusd INTEGER NOT NULL CHECK (budget_microusd >= 0),
        root_cap_microusd INTEGER NOT NULL CHECK (root_cap_microusd >= 0),
        stopped_at INTEGER, stopped_by TEXT REFERENCES principals(id), stop_reason TEXT
    )""",
    "CREATE UNIQUE INDEX rollouts_open ON rollouts((1)) WHERE stopped_at IS NULL",
    """CREATE TABLE rollout_allocations (
        root_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(id),
        rollout_id TEXT NOT NULL REFERENCES rollouts(id),
        bucket INTEGER NOT NULL CHECK (bucket BETWEEN 0 AND 9999),
        cohort TEXT NOT NULL CHECK (cohort IN ('stable','shadow','canary')),
        reason TEXT NOT NULL, allocated_microusd INTEGER NOT NULL CHECK (allocated_microusd >= 0)
    )""",
    "CREATE INDEX rollout_allocations_by_rollout ON rollout_allocations(rollout_id, cohort)",
    """CREATE TABLE finished_roots (
        root_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(id),
        actor TEXT NOT NULL REFERENCES principals(id), finished_at INTEGER NOT NULL
    )""",
    """CREATE TABLE revoked_revisions (
        revision TEXT PRIMARY KEY NOT NULL REFERENCES revisions(digest),
        event_id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL REFERENCES principals(id),
        revoked_at INTEGER NOT NULL
    )""",
    """CREATE TABLE alias_leases (
        id TEXT PRIMARY KEY NOT NULL, actor TEXT NOT NULL REFERENCES principals(id),
        generation INTEGER NOT NULL, revision TEXT NOT NULL REFERENCES revisions(digest),
        issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        invalidated_at INTEGER
    )""",
    """CREATE TABLE leased_roots (
        root_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(id),
        lease_id TEXT NOT NULL REFERENCES alias_leases(id)
    )""",
)


@dataclass(frozen=True, slots=True)
class Allocation:
    rollout_id: str
    revision: str
    bucket: int
    cohort: Literal["stable", "shadow", "canary"]
    reason: str
    allocated_microusd: int = 0

    def record(self, conn: Connection, root_id: str) -> None:
        conn.execute(
            "INSERT INTO rollout_allocations VALUES (?, ?, ?, ?, ?, ?)",
            (
                root_id,
                self.rollout_id,
                self.bucket,
                self.cohort,
                self.reason,
                self.allocated_microusd,
            ),
        )
        if self.cohort == "canary":
            rollout_projection.record_open(conn, self.rollout_id, root_id)


def bucket(scope: str, seed: str, task_id: str) -> int:
    """Map a trusted task identifier deterministically into [0, 10000)."""
    message = b"forge.rollout.v1\0" + canonical_json([scope, seed, task_id])
    return int.from_bytes(hashlib.sha256(message).digest()[:8], "big") % 10_000


def is_revoked(conn: Connection, revision: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM revoked_revisions WHERE revision = ?", (revision,)).fetchone()
        is not None
    )


def resolve_lease(conn: Connection, actor: str, lease_id: str, now: int) -> Row:
    lease: Row | None = conn.execute(
        """SELECT * FROM alias_leases WHERE id = ? AND actor = ? AND invalidated_at IS NULL
           AND issued_at <= ? AND expires_at > ?""",
        (lease_id, actor, now, now),
    ).fetchone()
    if lease is None or is_revoked(conn, lease["revision"]):
        raise PermissionError("Alias lease is unavailable, expired or revoked")
    return lease


def invalidate_leases(conn: Connection, now: int) -> None:
    conn.execute("UPDATE alias_leases SET invalidated_at = ? WHERE invalidated_at IS NULL", (now,))


def stop_open(conn: Connection, actor: str | None, now: int, reason: str) -> None:
    conn.execute(
        """UPDATE rollouts SET stopped_at = ?, stopped_by = ?, stop_reason = ?
           WHERE stopped_at IS NULL""",
        (now, actor, reason),
    )


def _current_evidence(conn: Connection, evidence_id: str) -> bool:
    row = conn.execute(
        """SELECT e.verifier AS certificate, p.verifier AS policy FROM gate_evidence e
           CROSS JOIN scope_policy p WHERE e.id = ? AND p.singleton = 1""",
        (evidence_id,),
    ).fetchone()
    return row is not None and row["certificate"] == certificate_verifier(row["policy"])


def plan_root(
    conn: Connection,
    *,
    scope: str,
    root_id: str,
    source: str,
    generation: int,
    stable: str,
    now: int,
) -> Allocation | None:
    row = conn.execute(
        """SELECT * FROM rollouts WHERE generation = ? AND stable_revision = ? AND source = ?
           AND stopped_at IS NULL AND created_at <= ? AND ends_at > ?""",
        (generation, stable, source, now, now),
    ).fetchone()
    if row is None:
        return None
    rollout_id = str(row["id"])
    candidate = str(row["candidate_revision"])
    selected = bucket(scope, row["seed"], root_id)
    if not _current_evidence(conn, row["evidence_id"]):
        return Allocation(rollout_id, stable, selected, "stable", "evidence_revalidation_required")
    if selected >= row["basis_points"]:
        return Allocation(rollout_id, stable, selected, "stable", "cohort_holdout")
    if is_revoked(conn, candidate):
        return Allocation(rollout_id, stable, selected, "stable", "candidate_revoked")
    if row["mode"] == "shadow":
        return Allocation(rollout_id, stable, selected, "shadow", "dry_run")
    active, _, exposure = capacity(conn, rollout_id)
    if active >= row["max_concurrent"]:
        return Allocation(rollout_id, stable, selected, "stable", "concurrency_limit")
    budget = row["root_cap_microusd"]
    if budget + exposure > row["budget_microusd"]:
        return Allocation(rollout_id, stable, selected, "stable", "spend_limit")
    return Allocation(rollout_id, candidate, selected, "canary", "selected", budget)


def dispatch_block(conn: Connection, task: Row, now: int) -> str | None:
    if is_revoked(conn, task["revision"]):
        return "revision_revoked"
    if (
        conn.execute(
            "SELECT 1 FROM finished_roots WHERE root_id = ?", (task["root_id"],)
        ).fetchone()
        is not None
    ):
        return "root_finished"
    row = conn.execute(
        """SELECT c.id, c.ends_at, c.budget_microusd, c.stop_reason, c.evidence_id
             FROM rollout_allocations a
             JOIN rollouts c ON c.id = a.rollout_id WHERE a.root_id = ? AND a.cohort = 'canary'""",
        (task["root_id"],),
    ).fetchone()
    if row is None:
        return None
    if row["stop_reason"] == "recovery_fenced":
        return "canary_recovery_fenced"
    if not _current_evidence(conn, row["evidence_id"]):
        return "evidence_revalidation_required"
    if now >= row["ends_at"]:
        return "canary_window_expired"
    if capacity(conn, row["id"])[2] > row["budget_microusd"]:
        return "canary_budget_exceeded"
    return None


def shadow_target(conn: Connection, root_id: str, now: int) -> str | None:
    row = conn.execute(
        """SELECT c.candidate_revision, c.evidence_id FROM rollout_allocations AS a
             JOIN rollouts AS c ON c.id = a.rollout_id
            WHERE a.root_id = ? AND a.cohort = 'shadow' AND c.stopped_at IS NULL
              AND c.created_at <= ? AND c.ends_at > ?""",
        (root_id, now, now),
    ).fetchone()
    if row is None or not _current_evidence(conn, row["evidence_id"]):
        return None
    return str(row["candidate_revision"])


def task_view(conn: Connection, row: Row) -> dict[str, Any]:
    view = dict(row)
    allocation = conn.execute(
        """SELECT a.*, c.candidate_revision FROM rollout_allocations a
           JOIN rollouts c ON c.id = a.rollout_id WHERE a.root_id = ?""",
        (row["root_id"],),
    ).fetchone()
    lease = conn.execute(
        """SELECT a.id, a.issued_at, a.expires_at FROM alias_leases a
           JOIN leased_roots l ON l.lease_id = a.id WHERE l.root_id = ?""",
        (row["root_id"],),
    ).fetchone()
    if allocation is not None:
        view["allocation"] = dict(allocation)
    if lease is not None:
        view["alias_lease"] = dict(lease)
    return view


def rollout_view(conn: Connection, row: Row, now: int) -> dict[str, Any]:
    view = dict(row)
    active, consumed, exposure = capacity(conn, row["id"])
    grouped = list(
        conn.execute(
            """SELECT cohort, reason, COUNT(*) AS roots FROM rollout_allocations
               WHERE rollout_id = ? GROUP BY cohort, reason ORDER BY cohort, reason""",
            (row["id"],),
        )
    )
    # Join on the root so child attempts contribute to their assigned cohort.
    traffic = conn.execute(
        """SELECT a.cohort, COUNT(DISTINCT a.root_id) AS allocated_roots,
                  COUNT(DISTINCT CASE WHEN t.dispatched_at IS NOT NULL
                                      THEN a.root_id END) AS roots_with_dispatch_intent,
                  COUNT(t.dispatched_at) AS dispatch_intents,
                  COUNT(DISTINCT CASE WHEN t.dispatched_at IS NOT NULL AND
                      (t.status IN ('dispatching', 'uncertain') OR
                       t.confirmation != 'verified' OR t.charge_microusd IS NULL)
                      THEN a.root_id END) AS roots_pending_confirmation
             FROM rollout_allocations a LEFT JOIN attempts t ON t.root_id = a.root_id
            WHERE a.rollout_id = ? GROUP BY a.cohort ORDER BY a.cohort""",
        (row["id"],),
    )
    state = "open"
    if row["stopped_at"] is not None:
        state = "stopped"
    elif now >= row["ends_at"]:
        state = "expired"
    elif not _current_evidence(conn, row["evidence_id"]):
        state = "evidence_revalidation_required"
    view.update(
        state=state,
        active_roots=active,
        consumed_microusd=consumed,
        exposure_microusd=exposure,
        eligible_observed_roots=sum(int(item["roots"]) for item in grouped),
        allocations=[dict(item) for item in grouped],
        traffic_by_cohort=[dict(item) for item in traffic],
    )
    return view
