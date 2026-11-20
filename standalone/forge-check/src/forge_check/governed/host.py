"""The dispatch boundary for one explicitly integrated host."""

from __future__ import annotations

from dataclasses import dataclass
from http.client import HTTPException
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from forge_check.governed.control import require_role
from forge_check.governed.ledger import Ledger, LedgerError, Observation
from forge_check.governed.provider import HttpProvider as HttpProvider
from forge_check.governed.provider import TransportError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from threading import Event

    from forge_check.governed.db import Store
    from forge_check.governed.ledger import Admission, Request

_OUTCOMES = ("succeeded", "failed", "cancelled", "uncertain")
_CONFIRMATIONS = ("none", "acknowledged", "verified")
_MAX_MICROUSD = (1 << 53) - 1
_UNCONFIRMED_TRANSPORT = (TransportError, HTTPException)


@dataclass(frozen=True)
class Invocation:
    disposition: str
    attempt: dict[str, object]
    result: dict[str, object] | None = None


@dataclass(frozen=True)
class StreamEvent:
    type: Literal["chunk", "terminal"]
    delta: str | None = None
    invocation: Invocation | None = None


def _label(document: dict[str, object], key: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 128
        or not value.isascii()
        or not value[0].isalnum()
        or not all(c.isalnum() or c in "._:-" for c in value)
    ):
        raise TransportError("Invalid provider observation identifier")
    return value


def _provider_observation(
    document: dict[str, object],
    *,
    attempt_id: str,
    operation_key: str,
) -> tuple[Observation, dict[str, object] | None]:
    if document.get("attempt_id") != attempt_id or document.get("operation_key") != operation_key:
        raise TransportError("Provider observation refers to a different attempt")
    observation_id = _label(document, "observation_id")
    outcome = document.get("outcome")
    confirmation = document.get("confirmation")
    charge = document.get("charge_microusd")
    status = document.get("upstream_status")
    result = document.get("result")
    if (
        observation_id is None
        or observation_id.startswith("host.")
        or outcome not in _OUTCOMES
        or confirmation not in _CONFIRMATIONS
        or (charge is not None and (type(charge) is not int or not 0 <= charge <= _MAX_MICROUSD))
        or (status is not None and (type(status) is not int or not 100 <= status <= 599))
        or (result is not None and not isinstance(result, dict))
    ):
        raise TransportError("Malformed provider observation")
    return (
        Observation(
            observation_id=observation_id,
            outcome=cast("Literal['succeeded', 'failed', 'cancelled', 'uncertain']", outcome),
            confirmation=cast("Literal['none', 'acknowledged', 'verified']", confirmation),
            charge_microusd=charge,
            provider_ref=_label(document, "provider_ref"),
            provider_model=_label(document, "provider_model"),
            upstream_status=status,
            charge_ref=_label(document, "charge_ref"),
        ),
        cast("dict[str, object] | None", result),
    )


def _body(admission: Admission, stream: bool) -> dict[str, object]:
    request = admission.request
    return {
        "attempt_id": request.attempt_id,
        "operation_id": request.operation_id,
        "operation_key": admission.attempt["operation_key"],
        "binding_id": request.binding_id,
        "revision": admission.attempt["revision"],
        "stream": stream,
        "configuration": admission.effective,
        "input": request.payload,
    }


def _cache_ticket(result: dict[str, object] | None) -> dict[str, object] | None:
    if result is None or set(result) != {"ticket_id", "label"}:
        return None
    try:
        if _label(result, "ticket_id") is None or _label(result, "label") is None:
            return None
    except TransportError:
        return None
    return result


class Host:
    """Reserve and record before using the model or ticket API configured by the operator."""

    def __init__(self, store: Store, *, fingerprint_key: bytes, provider: HttpProvider) -> None:
        self.store = store
        self.ledger = Ledger(store, fingerprint_key=fingerprint_key)
        self.provider = provider

    def _record_response(self, admission: Admission, document: dict[str, object]) -> Invocation:
        attempt_id = admission.request.attempt_id
        observation, result = _provider_observation(
            document, attempt_id=attempt_id, operation_key=str(admission.attempt["operation_key"])
        )
        binding = admission.effective or {}
        projection = _cache_ticket(result) if binding.get("effect") == "read" else None
        try:
            attempt = self.ledger.settle(attempt_id, observation, result=projection)
        except LedgerError as exc:
            raise TransportError("Provider observation could not be applied") from exc
        return Invocation("dispatch", attempt, result)

    def _interrupted(self, attempt_id: str, *, cancelled: bool = False) -> Invocation:
        observation = Observation(
            observation_id=f"host.{uuid4().hex}",
            outcome="cancelled" if cancelled else "uncertain",
            confirmation="none",
            reason="client_cancelled" if cancelled else "provider_response_unconfirmed",
        )
        return Invocation("dispatch", self.ledger.settle(attempt_id, observation))

    def _claim(self, admission: Admission) -> Invocation | None:
        if admission.disposition != "dispatch":
            return Invocation(admission.disposition, admission.attempt, admission.result)
        if not self.ledger.mark_dispatched(admission.request.attempt_id):
            attempt = self.ledger.attempt(admission.request.attempt_id)
            return Invocation("denied", attempt)
        return None

    def invoke(self, request: Request) -> Invocation:
        admission = self.ledger.admit(request)
        ready = self._claim(admission)
        if ready is not None:
            return ready
        try:
            return self._record_response(admission, self.provider.dispatch(_body(admission, False)))
        except _UNCONFIRMED_TRANSPORT:
            return self._interrupted(request.attempt_id)

    def stream(self, request: Request, *, cancel: Event | None = None) -> Iterator[StreamEvent]:
        admission = self.ledger.admit(request)
        if admission.disposition == "dispatch" and cancel is not None and cancel.is_set():
            observation = Observation(
                observation_id=f"host.{uuid4().hex}",
                outcome="not_sent",
                confirmation="verified",
                charge_microusd=0,
                reason="client_cancelled_before_dispatch",
            )
            yield StreamEvent(
                "terminal",
                invocation=Invocation(
                    "denied", self.ledger.settle(request.attempt_id, observation)
                ),
            )
            return

        ready = self._claim(admission)
        if ready is not None:
            yield StreamEvent("terminal", invocation=ready)
            return

        completed: Invocation | None = None
        cancelled = False
        try:
            with self.provider.streaming(_body(admission, True)) as records:
                for frame in records:
                    if cancel is not None and cancel.is_set():
                        cancelled = True
                        break
                    delta = frame.get("delta")
                    if frame.get("type") == "chunk" and isinstance(delta, str):
                        yield StreamEvent("chunk", delta=delta)
                        if cancel is not None and cancel.is_set():
                            cancelled = True
                            break
                        continue
                    provider_document = frame.get("observation")
                    if frame.get("type") != "observation" or not isinstance(
                        provider_document, dict
                    ):
                        raise TransportError("Malformed provider stream frame")
                    completed = self._record_response(admission, provider_document)
                    break
        except _UNCONFIRMED_TRANSPORT:
            cancelled = cancel is not None and cancel.is_set()
        except GeneratorExit:
            self._interrupted(request.attempt_id, cancelled=True)
            raise
        if completed is None:
            completed = self._interrupted(request.attempt_id, cancelled=cancelled)
        yield StreamEvent("terminal", invocation=completed)

    def reconcile(self, actor: str, attempt_id: str) -> Invocation:
        with self.store.read() as connection:
            require_role(connection, actor, "reconcile")
        attempt = self.ledger.attempt(attempt_id)
        operation_key = attempt.get("operation_key")
        if not isinstance(operation_key, str) or attempt.get("dispatched_at") is None:
            return Invocation("replay", attempt)
        try:
            observation, _ = _provider_observation(
                self.provider.lookup(operation_key, attempt_id),
                attempt_id=attempt_id,
                operation_key=operation_key,
            )
            return Invocation("reconciled", self.ledger.settle(attempt_id, observation))
        except (*_UNCONFIRMED_TRANSPORT, LedgerError):
            return self._interrupted(attempt_id)

    def compare_charges(
        self,
        actor: str,
        *,
        since: str,
        until: str,
        billing_scope: str,
        provider_total_microusd: int,
    ) -> dict[str, object]:
        from forge_check.governed.verify import compare_charges

        return compare_charges(
            self.store,
            actor,
            since=since,
            until=until,
            billing_scope=billing_scope,
            provider_total_microusd=provider_total_microusd,
        )
