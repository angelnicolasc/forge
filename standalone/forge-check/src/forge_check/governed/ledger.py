"""Durable admission, dispatch claims and provider observations for a single host."""

from __future__ import annotations

import hmac
import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from forge_check.governed import rollout
from forge_check.governed.binding_contract import operation_contract
from forge_check.governed.db import require_dispatch_authority
from forge_check.governed.revision import (
    MAX_SAFE_INTEGER,
    Revision,
    canonical_json,
    document_digest,
    identifier,
)

if TYPE_CHECKING:
    from sqlite3 import Connection, Row

    from forge_check.governed.binding_contract import OperationContract
    from forge_check.governed.db import Store

SCHEMA = (
    """CREATE TABLE attempts (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        root_id TEXT NOT NULL REFERENCES tasks(id),
        principal TEXT NOT NULL,
        binding_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        operation_key TEXT NOT NULL,
        retry_of TEXT REFERENCES attempts(id),
        request_fingerprint TEXT NOT NULL,
        effective_digest TEXT,
        policy_digest TEXT NOT NULL,
        contract_digest TEXT,
        binding_version TEXT,
        provider TEXT,
        tariff TEXT,
        cap_microusd INTEGER,
        reserved_microusd INTEGER NOT NULL CHECK (reserved_microusd >= 0),
        charge_microusd INTEGER CHECK (charge_microusd >= 0),
        status TEXT NOT NULL,
        confirmation TEXT NOT NULL,
        dispatched_at INTEGER,
        reason TEXT,
        provider_ref TEXT,
        provider_model TEXT,
        upstream_status INTEGER,
        cache_key TEXT,
        cache_ttl_seconds INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX attempts_root ON attempts(root_id)",
    "CREATE INDEX attempts_operation ON attempts(task_id, operation_id)",
    """CREATE TABLE receipts (
        seq INTEGER PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id),
        phase TEXT NOT NULL,
        recorded_at INTEGER NOT NULL,
        body TEXT NOT NULL
    )""",
    "CREATE INDEX receipts_task ON receipts(task_id, seq)",
    "CREATE INDEX receipts_window ON receipts(phase, recorded_at)",
    """CREATE TABLE attempt_observations (
        attempt_id TEXT NOT NULL REFERENCES attempts(id),
        observation_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        PRIMARY KEY (attempt_id, observation_id)
    )""",
    """CREATE TABLE charge_claims (
        provider TEXT NOT NULL,
        charge_ref TEXT NOT NULL,
        attempt_id TEXT NOT NULL REFERENCES attempts(id),
        amount INTEGER NOT NULL,
        PRIMARY KEY (provider, charge_ref)
    )""",
    """CREATE TABLE binding_suspensions (
        binding_id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL REFERENCES attempts(id),
        recorded_at INTEGER NOT NULL
    )""",
    """CREATE TABLE read_cache (
        cache_key TEXT PRIMARY KEY,
        source_attempt_id TEXT NOT NULL REFERENCES attempts(id),
        expires_at INTEGER NOT NULL,
        result TEXT NOT NULL
    )""",
)

Outcome = Literal["succeeded", "failed", "uncertain", "cancelled", "not_sent"]
Confirmation = Literal["none", "acknowledged", "verified"]
Disposition = Literal["dispatch", "cached", "replay", "denied", "invalid_result"]
ResultProjection = Literal["accepted", "rejected"]
_CONFIRMATION_RANK = {"none": 0, "acknowledged": 1, "verified": 2}
_TERMINAL = {"succeeded", "failed", "cancelled", "not_sent"}
_RETRYABLE = {"failed", "cancelled", "uncertain", "not_sent"}
_MAX_PAYLOAD_BYTES = 256 * 1024
_MAX_CACHE_BYTES = 4096


class LedgerError(RuntimeError):
    """A governed attempt could not be resolved safely."""


class LedgerConflictError(LedgerError):
    """An idempotency key was reused for a different command."""


@dataclass(frozen=True)
class Request:
    task_id: str
    principal: str
    operation_id: str
    attempt_id: str
    binding_id: str
    payload: dict[str, object]
    retry_of: str | None = None


@dataclass(frozen=True)
class Observation:
    observation_id: str
    outcome: Outcome
    confirmation: Confirmation
    charge_microusd: int | None = None
    provider_ref: str | None = None
    provider_model: str | None = None
    upstream_status: int | None = None
    reason: str | None = None
    charge_ref: str | None = None


@dataclass(frozen=True)
class Admission:
    disposition: Disposition
    reason: str | None
    request: Request
    effective: dict[str, Any] | None
    attempt: dict[str, Any]
    result: dict[str, object] | None = None
    contract: OperationContract | None = None


def _utc() -> int:
    return time.time_ns() // 1000


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False
    )


def _compact(value: object, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    try:
        identifier(value, name)
    except ValueError as exc:
        raise LedgerError(f"{name} must be a compact identifier") from exc


def _json_object(value: object, purpose: str, max_bytes: int) -> str:
    if not isinstance(value, dict):
        raise LedgerError(f"{purpose} must be a JSON object")
    try:
        raw = _json(value)
        if json.loads(raw) != value:
            raise ValueError("not a JSON object")
    except (TypeError, ValueError, RecursionError) as exc:
        raise LedgerError(f"{purpose} must contain JSON values") from exc
    if len(raw) > max_bytes:
        raise LedgerError(f"{purpose} exceeds the byte limit")
    return raw


def _validate_observation(observation: Observation) -> None:
    _compact(observation.observation_id, "observation ID")
    _compact(observation.provider_ref, "provider reference", optional=True)
    _compact(observation.provider_model, "provider model", optional=True)
    _compact(observation.charge_ref, "billing reference", optional=True)
    _compact(observation.reason, "observation reason", optional=True)
    if not isinstance(observation.outcome, str) or observation.outcome not in _TERMINAL | {
        "uncertain"
    }:
        raise LedgerError("invalid observation outcome")
    if (
        not isinstance(observation.confirmation, str)
        or observation.confirmation not in _CONFIRMATION_RANK
    ):
        raise LedgerError("invalid confirmation level")
    charge = observation.charge_microusd
    if charge is not None and (type(charge) is not int or not 0 <= charge <= MAX_SAFE_INTEGER):
        raise LedgerError("charge must be an integer number of micro-USD")
    if observation.charge_ref is not None and charge is None:
        raise LedgerError("billing reference requires a known charge")
    status = observation.upstream_status
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise LedgerError("upstream status must be an HTTP status code")
    if observation.outcome == "not_sent" and (charge not in (None, 0) or observation.charge_ref):
        raise LedgerError("unsent intent has zero provider exposure")


def _has_grant(conn: Connection, actor: str, binding: str) -> bool:
    row = conn.execute(
        "SELECT roles, bindings, active FROM principals WHERE id = ?", (actor,)
    ).fetchone()
    return bool(
        row is not None
        and row["active"] == 1
        and "invoke" in json.loads(row["roles"])
        and binding in json.loads(row["bindings"])
    )


def _task(conn: Connection, task_id: str) -> Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise LedgerError("unknown task")
    return cast("Row", row)


def _revision(conn: Connection, digest: str) -> Revision:
    row = conn.execute("SELECT canonical FROM revisions WHERE digest = ?", (digest,)).fetchone()
    if row is None:
        raise LedgerError("unknown pinned revision")
    revision = Revision.from_canonical(bytes(row["canonical"]))
    if revision.digest != digest:
        raise LedgerError("pinned revision failed its digest check")
    return revision


def _select_attempt(conn: Connection, attempt_id: str) -> Row:
    row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    if row is None:
        raise LedgerError("unknown attempt")
    return cast("Row", row)


def _attempt_view(row: Row, task: Row) -> dict[str, Any]:
    view = dict(row)
    view.pop("cache_key")
    view.pop("cache_ttl_seconds")
    view["attempt_id"] = view.pop("id")
    view["root_task_id"] = view.pop("root_id")
    view["parent_task_id"] = task["parent_id"]
    view["revision"] = task["revision"]
    view["generation"] = task["generation"]
    view["alias"] = task["alias"]
    view["worker"] = task["worker"]
    view["case_id"] = task["case_id"]
    view["source"] = task["source"]
    view["route"] = "code_mode" if task["source"] == "code_mode" else "direct"
    view["coverage_profile"] = "governed"
    view["currency"] = "micro-USD"
    return view


def _document_ref(kind: str, value: object) -> str:
    return document_digest(f"forge.{kind}.v1", canonical_json(value))


def _event(
    conn: Connection,
    attempt: Row,
    task: Row,
    phase: str,
    *,
    scope: str,
    detail: dict[str, Any] | None = None,
) -> None:
    body = _attempt_view(attempt, task)
    body.update(
        schema_version=1,
        event_id=uuid4().hex,
        recorded_at=_utc(),
        phase=phase,
        scope=scope,
        issuer="forge.binding",
        evidence=detail or {},
    )
    conn.execute(
        "INSERT INTO receipts(event_id, task_id, attempt_id, phase, recorded_at, body) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (body["event_id"], task["id"], attempt["id"], phase, body["recorded_at"], _json(body)),
    )


def _balance(conn: Connection, root_id: str, limit: int) -> dict[str, int]:
    totals = conn.execute(
        """SELECT COALESCE(SUM(charge_microusd), 0) AS consumed,
           COALESCE(SUM(CASE WHEN status IN ('reserved', 'dispatching')
                        THEN reserved_microusd ELSE 0 END), 0) AS reserved,
           COALESCE(SUM(CASE WHEN status NOT IN ('reserved', 'dispatching')
                        THEN reserved_microusd ELSE 0 END), 0) AS pending
           FROM attempts WHERE root_id = ?""",
        (root_id,),
    ).fetchone()
    consumed, reserved, pending = totals
    return {
        "limit_microusd": limit,
        "consumed_microusd": consumed,
        "reserved_microusd": reserved,
        "pending_microusd": pending,
        "available_microusd": max(0, limit - consumed - reserved - pending),
        "over_limit_microusd": max(0, consumed + reserved + pending - limit),
    }


def charge_total(conn: Connection, *, since: int, until: int, binding_id: str) -> int:
    """Return first-booked charges in the ledger's half-open recorded UTC µs window."""
    row = conn.execute(
        """SELECT COALESCE(SUM(json_extract(r.body, '$.charge_microusd')), 0)
           FROM receipts r JOIN attempts a ON a.id = r.attempt_id
           WHERE a.binding_id = ? AND r.recorded_at >= ? AND r.recorded_at < ?
             AND r.phase = 'observation'
             AND json_extract(r.body, '$.evidence.charge_disposition') = 'recorded'""",
        (binding_id, since, until),
    ).fetchone()
    return int(row[0])


def _retry_denial(
    previous: Row | None, request: Request, operation_key: str, binding: dict[str, Any]
) -> str | None:
    if previous is None:
        return "retry_invalid" if request.retry_of is not None else None
    if request.retry_of != previous["id"] or previous["operation_key"] != operation_key:
        return "retry_invalid" if request.retry_of is not None else "operation_exists"
    if previous["status"] not in _RETRYABLE:
        return "retry_not_allowed"
    if (
        previous["status"] != "not_sent"
        and binding.get("effect") == "write"
        and not binding["idempotency"]
    ):
        return "retry_not_idempotent"
    return None


def _cache_row(conn: Connection, cache_key: str | None) -> Row | None:
    if cache_key is None:
        return None
    return cast(
        "Row | None",
        conn.execute(
            "SELECT source_attempt_id, expires_at, result FROM read_cache "
            "WHERE cache_key = ? AND expires_at > ?",
            (cache_key, _utc()),
        ).fetchone(),
    )


def _arguments_valid(payload: dict[str, object], contract: OperationContract) -> bool:
    try:
        contract.validate_arguments(payload)
    except ValueError:
        return False
    return True


def _result_projection(
    conn: Connection, attempt: Row, observation_id: str | None = None
) -> ResultProjection | None:
    event = conn.execute(
        """SELECT json_extract(body, '$.evidence.result_projection')
           FROM receipts WHERE task_id = ? AND attempt_id = ?
             AND phase IN ('observation', 'result_projection')
             AND json_extract(body, '$.evidence.result_projection') IS NOT NULL
             AND (? IS NULL OR json_extract(body, '$.evidence.observation_id') = ?)
           ORDER BY seq DESC LIMIT 1""",
        (attempt["task_id"], attempt["id"], observation_id, observation_id),
    ).fetchone()
    return cast("ResultProjection", event[0]) if event else None


def _shadow_preview(conn: Connection, task: Row, request: Request) -> dict[str, Any] | None:
    target = rollout.shadow_target(conn, task["root_id"], _utc())
    if target is None:
        return None
    document = _revision(conn, target).document
    binding = document["bindings"].get(request.binding_id)
    reason = "preflight_passed"
    if rollout.is_revoked(conn, target):
        reason = "revision_revoked"
    elif task["actor"] != request.principal or not _has_grant(
        conn, request.principal, request.binding_id
    ):
        reason = "principal_denied"
    elif binding is None or request.binding_id not in document["policy"]["allowed_bindings"]:
        reason = "policy_denied"
    elif not _arguments_valid(
        request.payload, operation_contract(document["schema_version"], binding)
    ):
        reason = "invalid_binding_input"
    elif binding["reserve_microusd"] is None:
        reason = "price_unknown"
    elif binding["reserve_microusd"] > document["budget_microusd"]:
        reason = "binding_exceeds_root_budget"
    return {"shadow": {"revision": target, "preflight": reason, "provider_dispatched": False}}


class Ledger:
    """Enforce transactions before I/O and retain independent observations afterward."""

    def __init__(self, store: Store, *, fingerprint_key: bytes) -> None:
        if not isinstance(fingerprint_key, bytes) or len(fingerprint_key) < 32:
            raise LedgerError("fingerprint key requires at least 32 bytes")
        self._store = store
        self._fingerprint_key = fingerprint_key

    def _mac(self, purpose: str, *parts: object) -> str:
        return hmac.digest(self._fingerprint_key, _json([purpose, *parts]).encode(), "sha256").hex()

    def admit(self, request: Request) -> Admission:
        for field in (
            "task_id",
            "principal",
            "operation_id",
            "attempt_id",
            "binding_id",
            "retry_of",
        ):
            _compact(getattr(request, field), field, optional=field == "retry_of")
        payload = _json_object(request.payload, "payload", _MAX_PAYLOAD_BYTES)
        # Snapshot mutable host input so the envelope claimed for dispatch matches this decision.
        request = Request(
            request.task_id,
            request.principal,
            request.operation_id,
            request.attempt_id,
            request.binding_id,
            json.loads(payload),
            request.retry_of,
        )
        fingerprint = self._mac(
            "request-v1",
            request.task_id,
            request.principal,
            request.operation_id,
            request.binding_id,
            request.retry_of,
            payload,
        )
        with self._store.write() as conn:
            task = _task(conn, request.task_id)
            document = _revision(conn, task["revision"]).document
            scope = document["scope"]
            effective = document["bindings"].get(request.binding_id)
            existing = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (request.attempt_id,)
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(existing["request_fingerprint"], fingerprint):
                    raise LedgerConflictError("attempt ID was already used for another request")
                view = _attempt_view(existing, task)
                if existing["status"] == "denied":
                    return Admission("denied", existing["reason"], request, None, view)
                if not _has_grant(conn, request.principal, request.binding_id):
                    return Admission("denied", "principal_denied", request, None, {})
                if (
                    existing["status"] == "succeeded"
                    and _result_projection(conn, existing) == "rejected"
                ):
                    return Admission(
                        "invalid_result", "invalid_binding_output", request, None, view
                    )
                return Admission("replay", "attempt_exists", request, None, view)

            require_dispatch_authority(conn)
            contract = (
                operation_contract(document["schema_version"], effective) if effective else None
            )
            operation_key = self._mac(
                "operation-v1",
                scope,
                request.task_id,
                request.principal,
                request.operation_id,
                request.binding_id,
                task["revision"],
                payload,
            )
            previous = conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? AND operation_id = ? "
                "AND status != 'denied' ORDER BY rowid DESC LIMIT 1",
                (request.task_id, request.operation_id),
            ).fetchone()
            denial = self._denial(
                conn, task, request, document, effective, contract, previous, operation_key
            )
            cap = effective["reserve_microusd"] if effective else None
            ttl = (
                effective["cache_ttl_seconds"]
                if effective and effective.get("effect") == "read"
                else 0
            )
            cache_key = (
                self._mac(
                    "cache-v1",
                    scope,
                    request.principal,
                    task["revision"],
                    request.binding_id,
                    payload,
                )
                if ttl
                else None
            )
            cached = _cache_row(conn, cache_key) if not denial else None
            cached_result: dict[str, object] | None = None
            if cached is not None:
                try:
                    stored = json.loads(cached["result"])
                    cached_result = (
                        contract.project(stored) if contract and not contract.legacy else stored
                    )
                except ValueError:
                    conn.execute("DELETE FROM read_cache WHERE cache_key = ?", (cache_key,))
                    cached = None
            if not denial and cached is None and cap is not None:
                balance = _balance(conn, task["root_id"], document["budget_microusd"])
                if cap > balance["available_microusd"] or balance["over_limit_microusd"] > 0:
                    denial = "budget_exceeded"
            reserved = not denial and cached is None
            status = "denied" if denial else ("cached" if cached is not None else "reserved")
            provider = None
            if effective:
                name = effective.get("provider", effective.get("server"))
                provider = f"{effective['kind']}:{name}"
            conn.execute(
                """INSERT INTO attempts (
                    id, task_id, root_id, principal, binding_id, operation_id, operation_key,
                    retry_of, request_fingerprint, effective_digest, policy_digest, contract_digest,
                    binding_version, provider, tariff,
                    cap_microusd, reserved_microusd, charge_microusd, status, confirmation, reason,
                    cache_key, cache_ttl_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request.attempt_id,
                    task["id"],
                    task["root_id"],
                    request.principal,
                    request.binding_id,
                    request.operation_id,
                    operation_key,
                    previous["id"] if previous and request.retry_of == previous["id"] else None,
                    fingerprint,
                    _document_ref("binding", effective) if effective else None,
                    _document_ref("policy", document["policy"]),
                    _document_ref("contract", effective["contract"]) if effective else None,
                    effective["version"] if effective else None,
                    provider,
                    effective["tariff"] if effective else None,
                    cap,
                    cap if reserved else 0,
                    None if reserved else 0,
                    status,
                    "none",
                    denial,
                    cache_key,
                    ttl,
                ),
            )
            attempt = _select_attempt(conn, request.attempt_id)
            _event(
                conn,
                attempt,
                task,
                "admission",
                scope=scope,
                detail=_shadow_preview(conn, task, request),
            )
            view = _attempt_view(attempt, task)
            if denial:
                return Admission("denied", denial, request, None, view)
            if cached is not None:
                _event(
                    conn,
                    attempt,
                    task,
                    "cache_hit",
                    scope=scope,
                    detail={
                        "cached_from_attempt_id": cached["source_attempt_id"],
                        "valid_until": cached["expires_at"],
                    },
                )
                return Admission("cached", None, request, effective, view, cached_result, contract)
            return Admission("dispatch", None, request, effective, view, contract=contract)

    @staticmethod
    def _denial(
        conn: Connection,
        task: Row,
        request: Request,
        document: dict[str, Any],
        binding: dict[str, Any] | None,
        contract: OperationContract | None,
        previous: Row | None,
        operation_key: str,
    ) -> str | None:
        if task["actor"] != request.principal or not _has_grant(
            conn, request.principal, request.binding_id
        ):
            return "principal_denied"
        if block := rollout.dispatch_block(conn, task, _utc()):
            return block
        if binding is None or request.binding_id not in document["policy"]["allowed_bindings"]:
            return "policy_denied"
        if contract is None or not _arguments_valid(request.payload, contract):
            return "invalid_binding_input"
        if binding["reserve_microusd"] is None:
            return "price_unknown"
        if conn.execute(
            "SELECT 1 FROM binding_suspensions WHERE binding_id = ?", (request.binding_id,)
        ).fetchone():
            return "binding_suspended"
        return _retry_denial(previous, request, operation_key, binding)

    def mark_dispatched(self, attempt_id: str) -> bool:
        """Claim one physical send, rechecking mutable grants inside the transaction."""
        with self._store.write() as conn:
            attempt = _select_attempt(conn, attempt_id)
            if attempt["status"] != "reserved":
                return False
            require_dispatch_authority(conn)
            task = _task(conn, attempt["task_id"])
            document = _revision(conn, task["revision"]).document
            suspended = conn.execute(
                "SELECT 1 FROM binding_suspensions WHERE binding_id = ?", (attempt["binding_id"],)
            ).fetchone()
            now = _utc()
            reason = rollout.dispatch_block(conn, task, now)
            if not _has_grant(conn, attempt["principal"], attempt["binding_id"]):
                reason = "principal_denied"
            elif suspended and reason is None:
                reason = "binding_suspended"
            elif (
                reason is None
                and _balance(conn, task["root_id"], document["budget_microusd"])[
                    "over_limit_microusd"
                ]
                > 0
            ):
                reason = "budget_exceeded"
            if reason:
                conn.execute(
                    "UPDATE attempts SET status = 'denied', reason = ?, reserved_microusd = 0, "
                    "charge_microusd = 0 WHERE id = ?",
                    (reason, attempt_id),
                )
            else:
                conn.execute(
                    "UPDATE attempts SET status = 'dispatching', dispatched_at = ? WHERE id = ?",
                    (now, attempt_id),
                )
            _event(
                conn,
                _select_attempt(conn, attempt_id),
                task,
                "dispatch_denied" if reason else "dispatch_intent",
                scope=document["scope"],
                detail={"released_microusd": attempt["reserved_microusd"]} if reason else None,
            )
            return reason is None

    def settle(
        self,
        attempt_id: str,
        observation: Observation,
        *,
        result: dict[str, object] | None = None,
        result_projection: ResultProjection | None = None,
    ) -> dict[str, Any]:
        """Record upstream evidence and an optional host-approved read-cache projection."""
        _validate_observation(observation)
        fingerprint = self._mac("observation-v1", asdict(observation))
        with self._store.write() as conn:
            attempt = _select_attempt(conn, attempt_id)
            task = _task(conn, attempt["task_id"])
            prior = conn.execute(
                "SELECT fingerprint FROM attempt_observations "
                "WHERE attempt_id = ? AND observation_id = ?",
                (attempt_id, observation.observation_id),
            ).fetchone()
            if prior:
                if not hmac.compare_digest(prior["fingerprint"], fingerprint):
                    raise LedgerConflictError("observation ID conflicts with existing evidence")
                if (
                    result_projection is not None
                    and _result_projection(conn, attempt, observation.observation_id) is None
                ):
                    scope = _revision(conn, task["revision"]).document["scope"]
                    _event(
                        conn,
                        attempt,
                        task,
                        "result_projection",
                        scope=scope,
                        detail={
                            "observation_id": observation.observation_id,
                            "result_projection": result_projection,
                        },
                    )
                return _attempt_view(attempt, task)
            self._check_observable(attempt, observation)
            conn.execute(
                "INSERT INTO attempt_observations VALUES (?, ?, ?)",
                (attempt_id, observation.observation_id, fingerprint),
            )
            document = _revision(conn, task["revision"]).document
            updated = self._apply_observation(
                conn,
                attempt,
                task,
                document["scope"],
                observation,
                result_projection=result_projection,
            )
            if attempt["status"] == "dispatching":
                self._cache_result(conn, updated, observation, result, document)
            return _attempt_view(updated, task)

    @staticmethod
    def _check_observable(attempt: Row, observation: Observation) -> None:
        previous = attempt["status"]
        if observation.outcome == "not_sent":
            if previous not in {"reserved", "not_sent"}:
                raise LedgerError("a claimed dispatch cannot be settled as unsent")
        elif previous not in {"dispatching", "uncertain", "succeeded", "failed", "cancelled"}:
            raise LedgerError("attempt has no dispatch intent")

    def _apply_observation(
        self,
        conn: Connection,
        attempt: Row,
        task: Row,
        scope: str,
        observation: Observation,
        *,
        result_projection: ResultProjection | None,
    ) -> Row:
        amount, charge_disposition = self._observe_charge(conn, attempt, observation)
        previous_status = attempt["status"]
        previous_confirmation = attempt["confirmation"]
        conflicting = previous_status in _TERMINAL and observation.outcome != previous_status
        advances = not conflicting or (
            observation.confirmation == "verified"
            and previous_confirmation != "verified"
            and observation.outcome != "uncertain"
        )
        status = observation.outcome if advances else previous_status
        stronger = (
            _CONFIRMATION_RANK[observation.confirmation]
            >= _CONFIRMATION_RANK[previous_confirmation]
        )
        identity_conflict = previous_confirmation == observation.confirmation == "verified" and any(
            supplied is not None and attempt[name] is not None and supplied != attempt[name]
            for name, supplied in (
                ("provider_ref", observation.provider_ref),
                ("provider_model", observation.provider_model),
            )
        )
        corroborates = advances and stronger and not identity_conflict
        confirmation = observation.confirmation if corroborates else previous_confirmation
        known_charge = amount if amount is not None else attempt["charge_microusd"]
        reserved = 0 if known_charge is not None else attempt["reserved_microusd"]
        conn.execute(
            """UPDATE attempts SET status = ?, confirmation = ?, reserved_microusd = ?,
                charge_microusd = ?, reason = ?, provider_ref = ?, provider_model = ?,
                upstream_status = ? WHERE id = ?""",
            (
                status,
                confirmation,
                reserved,
                known_charge,
                observation.reason if advances else attempt["reason"],
                (observation.provider_ref or attempt["provider_ref"])
                if corroborates
                else attempt["provider_ref"],
                (observation.provider_model or attempt["provider_model"])
                if corroborates
                else attempt["provider_model"],
                observation.upstream_status
                if corroborates and observation.upstream_status is not None
                else attempt["upstream_status"],
                attempt["id"],
            ),
        )
        updated = _select_attempt(conn, attempt["id"])
        detail = asdict(observation)
        released = (
            max(0, attempt["reserved_microusd"] - known_charge) if known_charge is not None else 0
        )
        detail.update(
            outcome_conflict=conflicting and not advances,
            identity_conflict=identity_conflict,
            charge_disposition=charge_disposition,
            released_microusd=released,
        )
        if result_projection is not None:
            detail["result_projection"] = result_projection
        _event(conn, updated, task, "observation", scope=scope, detail=detail)
        if (
            detail["outcome_conflict"]
            or identity_conflict
            or charge_disposition == "conflicting_charge"
        ):
            conn.execute("DELETE FROM read_cache WHERE source_attempt_id = ?", (attempt["id"],))
        cap = attempt["cap_microusd"]
        cap_breach = amount is not None and cap is not None and amount > cap
        if cap_breach or charge_disposition == "conflicting_charge":
            conn.execute(
                "INSERT OR IGNORE INTO binding_suspensions VALUES (?, ?, ?)",
                (attempt["binding_id"], attempt["id"], _utc()),
            )
            phase = "price_cap_breached" if cap_breach else "charge_conflict"
            _event(conn, updated, task, phase, scope=scope)
        return updated

    @staticmethod
    def _observe_charge(
        conn: Connection, attempt: Row, observation: Observation
    ) -> tuple[int | None, str]:
        amount = 0 if observation.outcome == "not_sent" else observation.charge_microusd
        current = attempt["charge_microusd"]
        if amount is None:
            return None, "not_reported"
        if observation.charge_ref is not None:
            row = conn.execute(
                "SELECT attempt_id, amount FROM charge_claims "
                "WHERE provider = ? AND charge_ref = ?",
                (attempt["provider"], observation.charge_ref),
            ).fetchone()
            if row and row["amount"] != amount:
                return None, "conflicting_charge"
            if row and row["attempt_id"] != attempt["id"]:
                # Another attempt's bill proves no monetary outcome for this attempt.
                return None, "duplicate_charge"
        if current is not None and current != amount:
            return None, "conflicting_charge"
        if observation.charge_ref is not None:
            conn.execute(
                "INSERT OR IGNORE INTO charge_claims VALUES (?, ?, ?, ?)",
                (attempt["provider"], observation.charge_ref, attempt["id"], amount),
            )
        if current is not None:
            return None, "duplicate_charge"
        return amount, "recorded"

    @staticmethod
    def _cache_result(
        conn: Connection,
        attempt: Row,
        observation: Observation,
        result: dict[str, object] | None,
        document: dict[str, Any],
    ) -> None:
        if (
            result is None
            or attempt["cache_key"] is None
            or observation.outcome != "succeeded"
            or observation.confirmation != "verified"
            or attempt["status"] != "succeeded"
            or attempt["charge_microusd"] is None
            or not _has_grant(conn, attempt["principal"], attempt["binding_id"])
        ):
            return
        try:
            binding = document["bindings"][attempt["binding_id"]]
            contract = operation_contract(document["schema_version"], binding)
            projected = result if contract.legacy else contract.project(result)
            serialized = _json_object(projected, "cache result", _MAX_CACHE_BYTES)
        except (LedgerError, ValueError):
            return  # An unusable optional cache entry cannot discard provider evidence.
        # Responses can arrive out of order. Compare the durable claim sequence, including
        # for claims made in the same clock tick, rather than the response time.
        order = conn.execute(
            """SELECT incoming.seq > COALESCE((
                   SELECT prior.seq
                   FROM read_cache AS cached
                   JOIN attempts AS source ON source.id = cached.source_attempt_id
                   JOIN receipts AS prior ON prior.task_id = source.task_id
                     AND prior.attempt_id = source.id AND prior.phase = 'dispatch_intent'
                   WHERE cached.cache_key = ?
               ), 0)
               FROM receipts AS incoming
               WHERE incoming.task_id = ? AND incoming.attempt_id = ?
                 AND incoming.phase = 'dispatch_intent'""",
            (attempt["cache_key"], attempt["task_id"], attempt["id"]),
        ).fetchone()
        if order is None or not order[0]:
            return
        now = _utc()
        # An expired value cannot be read, but its source still orders same-key sends in flight.
        conn.execute(
            """DELETE FROM read_cache WHERE expires_at <= ? AND cache_key NOT IN (
                   SELECT cache_key FROM attempts
                   WHERE status = 'dispatching' AND cache_key IS NOT NULL
               )""",
            (now,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO read_cache VALUES (?, ?, ?, ?)",
            (
                attempt["cache_key"],
                attempt["id"],
                now + attempt["cache_ttl_seconds"] * 1_000_000,
                serialized,
            ),
        )

    def attempt(self, attempt_id: str) -> dict[str, Any]:
        with self._store.read() as conn:
            attempt = _select_attempt(conn, attempt_id)
            return _attempt_view(attempt, _task(conn, attempt["task_id"]))

    def balance(self, root_task_id: str) -> dict[str, int]:
        with self._store.read() as conn:
            task = _task(conn, root_task_id)
            if task["id"] != task["root_id"]:
                raise LedgerError("balance requires a root task ID")
            revision = _revision(conn, task["revision"])
            return _balance(conn, root_task_id, revision.document["budget_microusd"])

    def receipts(self, task_id: str) -> list[dict[str, Any]]:
        with self._store.read() as conn:
            _task(conn, task_id)
            rows = conn.execute(
                "SELECT r.seq, r.body FROM receipts AS r JOIN attempts AS a ON a.id = r.attempt_id "
                "WHERE a.task_id = ? OR a.root_id = ? ORDER BY r.seq",
                (task_id, task_id),
            )
            return [{**json.loads(row["body"]), "sequence": row["seq"]} for row in rows]
