"""Protected release policy, immutable revisions, and alias-pinned tasks."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Literal

from forge_check.contract import SCHEMA_VERSION, compare, parse_cases, parse_run
from forge_check.governed.revision import (
    VERIFIER,
    Revision,
    canonical_json,
    catalog_digest,
    digest_id,
    document_digest,
    identifier,
    integer,
    read_json,
    string_set,
)

if TYPE_CHECKING:
    from sqlite3 import Connection, Row

    from forge_check.governed.db import Store

ALIAS = "estable"
ROLES = frozenset(
    {
        "admin",
        "approve",
        "propose",
        "propose_automatic",
        "evidence",
        "inspect",
        "invoke",
        "reconcile",
    }
)

SCHEMA = (
    """CREATE TABLE scope_policy (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1), scope TEXT NOT NULL,
        cases BLOB NOT NULL, rule_digest TEXT NOT NULL, verifier TEXT NOT NULL,
        catalog_digest TEXT NOT NULL,
        evidence_ttl_seconds INTEGER NOT NULL CHECK (evidence_ttl_seconds > 0),
        initialized_at INTEGER NOT NULL, paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1))
    )""",
    """CREATE TABLE principals (
        id TEXT PRIMARY KEY NOT NULL, roles TEXT NOT NULL, bindings TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
    )""",
    """CREATE TABLE revisions (
        digest TEXT PRIMARY KEY NOT NULL, canonical BLOB NOT NULL,
        registered_by TEXT NOT NULL, registered_at INTEGER NOT NULL
    )""",
    """CREATE TABLE proposals (
        id TEXT PRIMARY KEY NOT NULL, candidate_revision TEXT NOT NULL REFERENCES revisions(digest),
        actor TEXT NOT NULL REFERENCES principals(id),
        source TEXT NOT NULL CHECK (source IN ('human','automatic')),
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE gate_evidence (
        id TEXT PRIMARY KEY NOT NULL, proposal_id TEXT NOT NULL REFERENCES proposals(id),
        actor TEXT NOT NULL REFERENCES principals(id), generation INTEGER NOT NULL,
        stable_revision TEXT NOT NULL REFERENCES revisions(digest),
        candidate_revision TEXT NOT NULL REFERENCES revisions(digest),
        rule_digest TEXT NOT NULL, verifier TEXT NOT NULL, observed_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
        snapshot_digest TEXT NOT NULL, report BLOB NOT NULL
    )""",
    """CREATE TABLE decisions (
        id TEXT PRIMARY KEY NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('promote','reject','rollback')),
        actor TEXT NOT NULL REFERENCES principals(id), proposal_id TEXT, evidence_id TEXT,
        expected_generation INTEGER NOT NULL,
        previous_revision TEXT NOT NULL REFERENCES revisions(digest),
        target_revision TEXT REFERENCES revisions(digest), generation INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('applied','rejected','conflict')),
        reason TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE aliases (
        name TEXT PRIMARY KEY NOT NULL CHECK (name = 'estable'),
        generation INTEGER NOT NULL CHECK (generation > 0),
        revision TEXT NOT NULL REFERENCES revisions(digest),
        decision_id TEXT REFERENCES decisions(id)
    )""",
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY NOT NULL, root_id TEXT NOT NULL REFERENCES tasks(id),
        parent_id TEXT REFERENCES tasks(id), actor TEXT NOT NULL REFERENCES principals(id),
        worker TEXT NOT NULL,
        alias TEXT NOT NULL REFERENCES aliases(name), generation INTEGER NOT NULL,
        revision TEXT NOT NULL REFERENCES revisions(digest), started_at INTEGER NOT NULL,
        case_id TEXT, source TEXT NOT NULL
    )""",
    "CREATE INDEX tasks_started_at ON tasks(started_at, id)",
    """CREATE TABLE control_events (
        id TEXT PRIMARY KEY NOT NULL, actor TEXT NOT NULL REFERENCES principals(id),
        action TEXT NOT NULL CHECK (action IN ('pause','resume','revoke')),
        target TEXT NOT NULL, created_at INTEGER NOT NULL
    )""",
)


def now_micros() -> int:
    return time.time_ns() // 1_000


def _required(row: Row | None, label: str) -> Row:
    if row is None:
        raise ValueError(f"Unknown {label}")
    return row


def principal(conn: Connection, actor: str) -> Row:
    identifier(actor, "actor")
    row: Row | None = conn.execute(
        "SELECT * FROM principals WHERE id = ? AND active = 1", (actor,)
    ).fetchone()
    if row is None:
        raise PermissionError("Actor is unavailable or revoked")
    return row


def require_role(conn: Connection, actor: str, role: str) -> Row:
    row = principal(conn, actor)
    if role not in json.loads(row["roles"]):
        raise PermissionError(f"Actor lacks {role} permission")
    return row


def _scope(conn: Connection) -> Row:
    return _required(
        conn.execute("SELECT * FROM scope_policy WHERE singleton = 1").fetchone(), "scope"
    )


def _alias(conn: Connection) -> Row:
    return _required(
        conn.execute("SELECT * FROM aliases WHERE name = ?", (ALIAS,)).fetchone(), "alias"
    )


def _revision(conn: Connection, digest: str) -> Revision:
    row = _required(
        conn.execute("SELECT canonical FROM revisions WHERE digest = ?", (digest,)).fetchone(),
        "revision",
    )
    revision = Revision.from_canonical(row["canonical"])
    if revision.digest != digest:
        raise ValueError("Stored revision digest mismatch")
    return revision


def _matches_revision(revision: Revision, policy: Row) -> bool:
    doc = revision.document
    rules = read_json(policy["cases"])
    return bool(
        doc["scope"] == policy["scope"]
        and doc["check"]["rule_id"] == rules["rule_id"]
        and doc["check"]["verifier"] == policy["verifier"]
        and catalog_digest(revision) == policy["catalog_digest"]
    )


def _validated_revision(revision: Revision) -> Revision:
    checked = Revision.from_canonical(revision.canonical)
    if checked.digest != revision.digest:
        raise ValueError("Revision digest mismatch")
    return checked


def _grants(
    principals: dict[str, dict[str, list[str]]], known_bindings: set[str]
) -> list[tuple[str, str, str]]:
    grants = []
    covered_roles: set[str] = set()
    for actor, claim in sorted(principals.items()):
        identifier(actor, "principal")
        if not isinstance(claim, dict) or set(claim) != {"roles", "bindings"}:
            raise ValueError("Each principal requires roles and bindings")
        roles = set(string_set(claim["roles"], "role"))
        bindings = string_set(claim["bindings"], "binding permission")
        if not roles or roles - ROLES or set(bindings) - known_bindings:
            raise ValueError("Invalid principal permissions")
        if ("propose_automatic" in roles) and roles & {"admin", "approve", "evidence"}:
            raise ValueError("An automatic proposer cannot control approvals or evidence")
        if bindings and "invoke" not in roles:
            raise ValueError("Binding permission requires the invoke role")
        covered_roles.update(roles)
        grants.append((actor, json.dumps(sorted(roles)), json.dumps(bindings)))
    if {"admin", "approve", "evidence"} - covered_roles:
        raise ValueError("Policy requires admin, approve and evidence authority")
    return grants


def _proposal(conn: Connection, proposal_id: str) -> Row:
    return _required(
        conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone(), "proposal"
    )


def _evidence_view(row: Row) -> dict[str, Any]:
    view = dict(row)
    view["report"] = read_json(row["report"])
    return view


def _evidence_digest(policy: Row, identity: dict[str, Any], report: object) -> str:
    payload = {
        "scope": policy["scope"],
        "alias": ALIAS,
        "rule_digest": policy["rule_digest"],
        "verifier": policy["verifier"],
        "report": report,
        **identity,
    }
    return document_digest("forge.check.evidence.v1", canonical_json(payload))


class Control:
    """Trusts the host caller to authenticate the actor outside agent-controlled data."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def bootstrap(
        self,
        scope: str,
        initial: Revision,
        cases: object,
        principals: dict[str, dict[str, list[str]]],
        *,
        evidence_ttl_seconds: int = 3600,
    ) -> None:
        identifier(scope, "scope")
        ttl = integer(evidence_ttl_seconds, "evidence TTL", minimum=1, maximum=86_400)
        revision = _validated_revision(initial)
        expected = parse_cases(cases)
        doc = revision.document
        if (
            scope != doc["scope"]
            or expected.rule_id != doc["check"]["rule_id"]
            or not expected.expected
        ):
            raise ValueError(
                "Bootstrap requires a scoped revision and a matching protected case set"
            )
        policy_cases = canonical_json(
            {
                "schema_version": SCHEMA_VERSION,
                "rule_id": expected.rule_id,
                "source": expected.source,
                "cases": [
                    {"id": key, "expected": value}
                    for key, value in sorted(expected.expected.items())
                ],
            }
        )
        grants = _grants(principals, set(doc["bindings"]))

        with self.store.write() as conn:
            if (
                conn.execute("SELECT 1 FROM scope_policy WHERE singleton = 1").fetchone()
                is not None
            ):
                raise ValueError("Protected scope is already initialized")
            now = now_micros()
            conn.execute(
                """INSERT INTO scope_policy (
                    singleton, scope, cases, rule_digest, verifier, catalog_digest,
                    evidence_ttl_seconds, initialized_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scope,
                    policy_cases,
                    document_digest("forge.check.cases.v1", policy_cases),
                    VERIFIER,
                    catalog_digest(revision),
                    ttl,
                    now,
                ),
            )
            conn.executemany(
                "INSERT INTO principals (id, roles, bindings) VALUES (?, ?, ?)", grants
            )
            conn.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?)",
                (revision.digest, revision.canonical, "host-bootstrap", now),
            )
            conn.execute("INSERT INTO aliases VALUES (?, 1, ?, NULL)", (ALIAS, revision.digest))

    def register(self, actor: str, revision: Revision) -> str:
        checked = _validated_revision(revision)
        with self.store.write() as conn:
            roles = set(json.loads(principal(conn, actor)["roles"]))
            if not roles & {"propose", "propose_automatic"}:
                raise PermissionError("Actor cannot register candidate revisions")
            if not _matches_revision(checked, _scope(conn)):
                raise ValueError("Revision conflicts with protected scope, check or host catalog")
            conn.execute(
                """INSERT INTO revisions VALUES (?, ?, ?, ?) ON CONFLICT(digest) DO NOTHING""",
                (checked.digest, checked.canonical, actor, now_micros()),
            )
            if _revision(conn, checked.digest).canonical != checked.canonical:
                raise ValueError("Revision already has different canonical content")
        return checked.digest

    def propose(
        self,
        actor: str,
        proposal_id: str,
        digest: str,
        source: Literal["human", "automatic"] = "human",
    ) -> dict[str, Any]:
        identifier(proposal_id, "proposal")
        digest_id(digest)
        if source not in ("human", "automatic"):
            raise ValueError("Invalid proposal source")
        role = "propose" if source == "human" else "propose_automatic"
        with self.store.write() as conn:
            require_role(conn, actor, role)
            _revision(conn, digest)
            previous = conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if previous is not None:
                if (previous["actor"], previous["candidate_revision"], previous["source"]) != (
                    actor,
                    digest,
                    source,
                ):
                    raise ValueError("Proposal ID already binds a different request")
                return dict(previous)
            conn.execute(
                "INSERT INTO proposals VALUES (?, ?, ?, ?, ?)",
                (proposal_id, digest, actor, source, now_micros()),
            )
            return dict(_proposal(conn, proposal_id))

    def evaluate(
        self,
        actor: str,
        evidence_id: str,
        proposal_id: str,
        baseline: object | None,
        candidate: object,
        *,
        observed_at: int,
        generation: int,
    ) -> dict[str, Any]:
        identifier(evidence_id, "evidence")
        identifier(proposal_id, "proposal")
        integer(observed_at, "evidence timestamp", minimum=1)
        integer(generation, "generation", minimum=1)
        with self.store.write() as conn:
            require_role(conn, actor, "evidence")
            policy = _scope(conn)
            alias = _alias(conn)
            proposal = _proposal(conn, proposal_id)
            current = conn.execute(
                "SELECT * FROM gate_evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
            if current is not None:
                if (current["actor"], current["proposal_id"], current["generation"]) != (
                    actor,
                    proposal_id,
                    generation,
                ):
                    raise ValueError("Evidence ID already binds a different request")
                stable = current["stable_revision"]
            elif generation == alias["generation"]:
                stable = alias["revision"]
            else:
                raise ValueError("Evidence generation differs from the current alias")

            now = now_micros()
            if observed_at > now:
                raise ValueError("Evidence timestamp is in the future")
            target = proposal["candidate_revision"]
            case_set = parse_cases(read_json(policy["cases"]))
            prior = None if baseline is None else parse_run(baseline, "baseline", case_set)
            later = parse_run(candidate, "candidate", case_set)
            if (prior is not None and prior.revision != stable) or later.revision != target:
                raise ValueError("Evidence binds the wrong stable or candidate revision")
            report = compare(case_set, prior, later)
            identity = {
                "actor": actor,
                "observed_at": observed_at,
                "generation": generation,
                "stable_revision": stable,
                "candidate_revision": target,
                "proposal_id": proposal_id,
            }
            snapshot = _evidence_digest(policy, identity, report)
            if current is not None:
                if current["snapshot_digest"] != snapshot:
                    raise ValueError("Evidence ID already binds different evidence")
                return _evidence_view(current)

            conn.execute(
                """INSERT INTO gate_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence_id,
                    proposal_id,
                    actor,
                    generation,
                    stable,
                    target,
                    policy["rule_digest"],
                    policy["verifier"],
                    observed_at,
                    observed_at + policy["evidence_ttl_seconds"] * 1_000_000,
                    now,
                    snapshot,
                    canonical_json(report),
                ),
            )
            return _evidence_view(
                _required(
                    conn.execute(
                        "SELECT * FROM gate_evidence WHERE id = ?", (evidence_id,)
                    ).fetchone(),
                    "evidence",
                )
            )

    def promote(
        self,
        actor: str,
        decision_id: str,
        proposal_id: str,
        evidence_id: str,
        expected_generation: int,
    ) -> dict[str, Any]:
        return self._decide(
            actor, decision_id, "promote", expected_generation, proposal_id, evidence_id
        )

    def reject(
        self,
        actor: str,
        decision_id: str,
        proposal_id: str,
        evidence_id: str,
        expected_generation: int,
    ) -> dict[str, Any]:
        return self._decide(
            actor, decision_id, "reject", expected_generation, proposal_id, evidence_id
        )

    def rollback(self, actor: str, decision_id: str, expected_generation: int) -> dict[str, Any]:
        return self._decide(actor, decision_id, "rollback", expected_generation)

    def _decide(
        self,
        actor: str,
        decision_id: str,
        kind: Literal["promote", "reject", "rollback"],
        expected_generation: int,
        proposal_id: str | None = None,
        evidence_id: str | None = None,
    ) -> dict[str, Any]:
        identifier(decision_id, "decision")
        integer(expected_generation, "expected generation", minimum=1)
        if kind != "rollback":
            identifier(proposal_id, "proposal")
            identifier(evidence_id, "evidence")
        with self.store.write() as conn:
            require_role(conn, actor, "approve")
            previous = conn.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            request = (kind, actor, proposal_id, evidence_id, expected_generation)
            if previous is not None:
                if tuple(previous)[1:6] != request:
                    raise ValueError("Decision ID already binds a different request")
                return dict(previous)

            alias = _alias(conn)
            policy = _scope(conn)
            now = now_micros()
            status, reason, target = self._disposition(
                conn, kind, proposal_id, evidence_id, expected_generation, alias, policy, now
            )
            generation = int(alias["generation"]) + int(status == "applied")
            conn.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision_id, *request, alias["revision"], target, generation, status, reason, now),
            )
            if status == "applied":
                cursor = conn.execute(
                    """UPDATE aliases SET revision = ?, generation = ?, decision_id = ?
                    WHERE name = ? AND generation = ? AND revision = ?""",
                    (
                        target,
                        generation,
                        decision_id,
                        ALIAS,
                        expected_generation,
                        alias["revision"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Concurrent alias change inside the writer transaction")
            return dict(
                _required(
                    conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone(),
                    "decision",
                )
            )

    @staticmethod
    def _disposition(
        conn: Connection,
        kind: str,
        proposal_id: str | None,
        evidence_id: str | None,
        expected_generation: int,
        alias: Row,
        policy: Row,
        now: int,
    ) -> tuple[str, str, str | None]:
        if expected_generation != alias["generation"]:
            return "conflict", "stale_generation", None
        if kind == "rollback":
            last = conn.execute(
                "SELECT * FROM decisions WHERE id = ?", (alias["decision_id"],)
            ).fetchone()
            if last is None or last["kind"] != "promote" or last["status"] != "applied":
                return "rejected", "previous_revision_unavailable", None
            return "applied", "rollback", str(last["previous_revision"])

        proposal = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if proposal is None:
            return "rejected", "proposal_unavailable", None
        target = str(proposal["candidate_revision"])
        if kind == "reject":
            return "rejected", "operator_rejected", target
        if policy["paused"]:
            return "rejected", "scope_paused", target
        if target == alias["revision"] or not _matches_revision(_revision(conn, target), policy):
            return "rejected", "revision_preflight_failed", target

        evidence = conn.execute(
            "SELECT * FROM gate_evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        if evidence is None:
            return "rejected", "evidence_unavailable", target
        if (
            evidence["proposal_id"] != proposal_id
            or evidence["generation"] != expected_generation
            or evidence["stable_revision"] != alias["revision"]
            or evidence["candidate_revision"] != target
            or evidence["rule_digest"] != policy["rule_digest"]
            or evidence["verifier"] != policy["verifier"]
        ):
            return "rejected", "evidence_mismatch", target
        if not evidence["observed_at"] <= now < evidence["expires_at"]:
            return "rejected", "evidence_expired", target
        try:
            require_role(conn, evidence["actor"], "evidence")
        except PermissionError:
            return "rejected", "evidence_authority_revoked", target
        report = read_json(evidence["report"])
        identity = {
            field: evidence[field]
            for field in (
                "actor",
                "observed_at",
                "generation",
                "stable_revision",
                "candidate_revision",
                "proposal_id",
            )
        }
        if _evidence_digest(policy, identity, report) != evidence["snapshot_digest"]:
            return "rejected", "evidence_integrity_failure", target
        verdict = report["verdict"]
        if verdict != "pass":
            return "rejected", "gate_failed" if verdict == "fail" else "gate_insufficient", target
        return "applied", "gate_passed", target

    def start_task(
        self,
        actor: str,
        task_id: str,
        *,
        alias: str = ALIAS,
        parent_id: str | None = None,
        worker: str,
        case_id: str | None = None,
        source: str = "direct",
    ) -> dict[str, Any]:
        identifier(task_id, "task")
        identifier(worker, "worker")
        identifier(source, "source")
        if alias != ALIAS:
            raise ValueError("Unsupported alias")
        if parent_id is not None:
            identifier(parent_id, "parent task")
            if parent_id == task_id:
                raise ValueError("A task cannot parent itself")
        if case_id is not None:
            identifier(case_id, "case")

        with self.store.write() as conn:
            require_role(conn, actor, "invoke")
            existing = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if existing is not None:
                if existing["actor"] != actor:
                    raise PermissionError("Task belongs to another actor")
                if (
                    existing["parent_id"],
                    existing["worker"],
                    existing["alias"],
                    existing["case_id"],
                    existing["source"],
                ) != (parent_id, worker, alias, case_id, source):
                    raise ValueError("Task ID already binds a different start request")
                return dict(existing)

            if parent_id is None:
                if _scope(conn)["paused"]:
                    raise PermissionError("Scope is paused for new root tasks")
                current = _alias(conn)
                root_id, generation, digest = task_id, current["generation"], current["revision"]
            else:
                parent = _required(
                    conn.execute("SELECT * FROM tasks WHERE id = ?", (parent_id,)).fetchone(),
                    "parent task",
                )
                if parent["actor"] != actor:
                    raise PermissionError("Parent task belongs to another actor")
                root_id, generation, digest = (
                    parent["root_id"],
                    parent["generation"],
                    parent["revision"],
                )
            conn.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    root_id,
                    parent_id,
                    actor,
                    worker,
                    alias,
                    generation,
                    digest,
                    now_micros(),
                    case_id,
                    source,
                ),
            )
            return dict(
                _required(
                    conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone(), "task"
                )
            )

    def get_alias(self, actor: str) -> dict[str, Any]:
        with self.store.read() as conn:
            require_role(conn, actor, "inspect")
            return dict(_alias(conn))

    def get_decision(self, actor: str, decision_id: str) -> dict[str, Any]:
        identifier(decision_id, "decision")
        with self.store.read() as conn:
            require_role(conn, actor, "inspect")
            return dict(
                _required(
                    conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone(),
                    "decision",
                )
            )

    def get_revision(self, actor: str, digest: str) -> Revision:
        digest_id(digest)
        with self.store.read() as conn:
            require_role(conn, actor, "inspect")
            return _revision(conn, digest)

    def get_evidence(self, actor: str, evidence_id: str) -> dict[str, Any]:
        identifier(evidence_id, "evidence")
        with self.store.read() as conn:
            require_role(conn, actor, "inspect")
            row = conn.execute(
                "SELECT * FROM gate_evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
            return _evidence_view(_required(row, "evidence"))

    def set_paused(self, actor: str, event_id: str, paused: bool) -> None:
        if type(paused) is not bool:
            raise ValueError("Invalid pause state")
        self._admin_event(actor, event_id, "pause" if paused else "resume", ALIAS)

    def revoke(self, actor: str, event_id: str, target: str) -> None:
        self._admin_event(actor, event_id, "revoke", target)

    def _admin_event(self, actor: str, event_id: str, action: str, target: str) -> None:
        identifier(event_id, "control event")
        identifier(target, "target")
        with self.store.write() as conn:
            require_role(conn, actor, "admin")
            previous = conn.execute(
                "SELECT * FROM control_events WHERE id = ?", (event_id,)
            ).fetchone()
            if previous is not None:
                if (previous["actor"], previous["action"], previous["target"]) != (
                    actor,
                    action,
                    target,
                ):
                    raise ValueError("Control event already binds a different request")
                return
            if action == "revoke":
                if actor == target:
                    raise ValueError("An administrator cannot revoke its own authority")
                cursor = conn.execute("UPDATE principals SET active = 0 WHERE id = ?", (target,))
                if cursor.rowcount != 1:
                    raise ValueError("Unknown principal")
            else:
                conn.execute(
                    "UPDATE scope_policy SET paused = ? WHERE singleton = 1",
                    (int(action == "pause"),),
                )
            conn.execute(
                "INSERT INTO control_events VALUES (?, ?, ?, ?, ?)",
                (event_id, actor, action, target, now_micros()),
            )
