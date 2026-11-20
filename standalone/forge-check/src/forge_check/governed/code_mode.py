"""Opt-in JavaScript orchestration over the same governed read binding."""

from __future__ import annotations

import hashlib
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from threading import Event, Timer
from typing import TYPE_CHECKING, Literal

from forge_check.governed._code_wire import (
    MAX_ARGUMENT_BYTES,
    MAX_FRAME_BYTES,
    MAX_INPUT_BYTES,
    MAX_SOURCE_BYTES,
    WireError,
    document,
    encode,
    write_frame,
)
from forge_check.governed.control import now_micros, require_role
from forge_check.governed.db import StoreError
from forge_check.governed.ledger import LedgerError, Request
from forge_check.governed.revision import Revision, identifier

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import IO

    from forge_check.governed.host import Host, Invocation

SCHEMA = (
    """CREATE TABLE code_programs (
        id TEXT PRIMARY KEY NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id),
        actor TEXT NOT NULL REFERENCES principals(id), started_at INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running','completed','aborted')),
        reason TEXT, finished_at INTEGER, calls INTEGER NOT NULL DEFAULT 0
    )""",
)

_WORKER = "forge_check.governed._code_worker"
_WORKER_REASONS = frozenset(
    {
        "runtime_unavailable",
        "wall_limit",
        "memory_limit",
        "output_limit",
        "program_error",
        "worker_error",
    }
)


@dataclass(frozen=True, slots=True)
class CodeLimits:
    wall_seconds: float = 5.0
    cpu_seconds: int = 3
    memory_bytes: int = 16 * 1024 * 1024
    output_bytes: int = 4096
    max_calls: int = 8

    def __post_init__(self) -> None:
        if type(self.wall_seconds) not in (float, int) or not 0.1 <= self.wall_seconds <= 20:
            raise ValueError("Wall limit must be from 0.1 to 20 seconds")
        for value, low, high in (
            (self.cpu_seconds, 1, 10),
            (self.memory_bytes, 1024 * 1024, 64 * 1024 * 1024),
            (self.output_bytes, 32, 16 * 1024),
            (self.max_calls, 0, 32),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Executor limit is outside the supported range")


@dataclass(frozen=True, slots=True)
class CodeRun:
    """Program execution outcome; the linked receipts describe each provider effect."""

    status: Literal["completed", "aborted", "replay"]
    reason: str | None = None
    output: object = None
    calls: int = 0
    attempt_ids: tuple[str, ...] = ()


class _ProgramAbortError(Exception):
    """Internal fixed reason returned when a program must stop."""


def _prefix(task_id: str, program_id: str) -> str:
    return "program." + hashlib.sha256(f"{task_id}\0{program_id}".encode()).hexdigest()


def _allowed_result(invocation: Invocation, requested_ticket: str) -> dict[str, object]:
    status = invocation.attempt.get("status", "denied")
    confirmation = invocation.attempt.get("confirmation", "none")
    projection: dict[str, str] | None = None
    result = invocation.result
    if result is not None and status in ("succeeded", "cached"):
        try:
            ticket_id = identifier(result.get("ticket_id"), "ticket ID")
            label = identifier(result.get("label"), "ticket label")
            if ticket_id == requested_ticket:
                projection = {"ticket_id": ticket_id, "label": label}
        except ValueError:
            pass
    return {"status": status, "confirmation": confirmation, "result": projection}


class _Worker:
    def __init__(self, process: subprocess.Popen[bytes], deadline: float) -> None:
        self.process = process
        self.deadline = deadline
        self.buffer = bytearray()
        self.selector = selectors.DefaultSelector()
        self.expired = Event()
        self.watchdog = Timer(max(0, deadline - time.monotonic()), self._expire)
        self.watchdog.daemon = True
        if process.stdin is None or process.stdout is None:
            raise _ProgramAbortError("worker_error")
        self.stdin: IO[bytes] = process.stdin
        self.stdout: IO[bytes] = process.stdout
        self.selector.register(self.stdout, selectors.EVENT_READ)
        self.watchdog.start()

    def _expire(self) -> None:
        self.expired.set()
        self.process.kill()

    def check(self) -> None:
        if self.expired.is_set() or time.monotonic() >= self.deadline:
            raise _ProgramAbortError("wall_limit")

    def send(self, value: object) -> None:
        self.check()
        write_frame(self.stdin, value)

    def receive(self) -> dict[str, object]:
        while True:
            self.check()
            boundary = self.buffer.find(b"\n")
            if boundary >= 0:
                frame = bytes(self.buffer[:boundary])
                del self.buffer[: boundary + 1]
                return document(frame)
            if len(self.buffer) > MAX_FRAME_BYTES:
                raise _ProgramAbortError("protocol_limit")
            events = self.selector.select(max(0, self.deadline - time.monotonic()))
            if not events:
                raise _ProgramAbortError("wall_limit")
            block = os.read(self.stdout.fileno(), MAX_FRAME_BYTES + 1 - len(self.buffer))
            if not block:
                code = self.process.wait(timeout=max(0, self.deadline - time.monotonic()))
                self.check()
                reason = "cpu_limit" if code == -signal.SIGXCPU else "worker_error"
                raise _ProgramAbortError(reason)
            self.buffer.extend(block)

    def close(self) -> None:
        self.watchdog.cancel()
        self.selector.close()
        self.process.kill()
        self.watchdog.join()


class CodeMode:
    """Run one fresh sandbox per program on a task explicitly assigned to Code Mode."""

    def __init__(
        self,
        host: Host,
        *,
        enabled: bool = False,
        bindings: Iterable[str] = (),
    ) -> None:
        self.host = host
        self.enabled = enabled
        self.bindings = frozenset(identifier(binding, "binding") for binding in bindings)

    def run(
        self,
        actor: str,
        task_id: str,
        program_id: str,
        source: str,
        *,
        inputs: object = None,
        limits: CodeLimits | None = None,
    ) -> CodeRun:
        if not self.enabled:
            return CodeRun("aborted", "disabled")
        if sys.platform != "linux":
            return CodeRun("aborted", "runtime_unavailable")
        identifier(actor, "actor")
        identifier(task_id, "task ID")
        identifier(program_id, "program ID")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Expected a JavaScript function body")
        encode(source, MAX_SOURCE_BYTES)
        encode(inputs, MAX_INPUT_BYTES)
        configured = CodeLimits() if limits is None else limits
        prefix = _prefix(task_id, program_id)
        claim = self._claim(actor, task_id, program_id, prefix)
        if isinstance(claim, CodeRun):
            return claim
        result = self._run_worker(actor, task_id, prefix, source, inputs, configured, claim)
        with self.host.store.write() as connection:
            connection.execute(
                """UPDATE code_programs SET status = ?, reason = ?, finished_at = ?, calls = ?
                   WHERE id = ? AND status = 'running'""",
                (result.status, result.reason, now_micros(), result.calls, program_id),
            )
        return result

    def _claim(
        self,
        actor: str,
        task_id: str,
        program_id: str,
        prefix: str,
    ) -> list[str] | CodeRun:
        from forge_check.governed.db import require_dispatch_authority

        with self.host.store.write() as connection:
            require_role(connection, actor, "invoke")
            row = connection.execute(
                """SELECT t.actor, t.source, t.revision, r.canonical
                   FROM tasks t JOIN revisions r ON r.digest = t.revision WHERE t.id = ?""",
                (task_id,),
            ).fetchone()
            if row is None or row["actor"] != actor or row["source"] != "code_mode":
                raise PermissionError("Code Mode requires a task owned by that actor and source")
            current = connection.execute(
                "SELECT task_id, actor, status, reason, calls FROM code_programs WHERE id = ?",
                (program_id,),
            ).fetchone()
            if current is not None:
                if current["task_id"] != task_id or current["actor"] != actor:
                    raise ValueError("Program ID already belongs to a different task")
                attempts = connection.execute(
                    "SELECT id FROM attempts WHERE task_id = ? AND id LIKE ? ORDER BY rowid",
                    (task_id, prefix + ".%"),
                )
                reason = (
                    "incomplete_program" if current["status"] == "running" else current["reason"]
                )
                known = tuple(str(attempt["id"]) for attempt in attempts)
                return CodeRun(
                    "replay", reason, calls=max(current["calls"], len(known)), attempt_ids=known
                )

            require_dispatch_authority(connection)
            revision = Revision.from_canonical(bytes(row["canonical"]))
            if revision.digest != row["revision"]:
                raise StoreError("Pinned revision digest mismatch")
            methods = [
                binding
                for binding, entry in revision.document["bindings"].items()
                if binding in self.bindings
                and entry["kind"] == "tool"
                and entry["effect"] == "read"
            ]
            connection.execute(
                """INSERT INTO code_programs(id, task_id, actor, started_at, status)
                   VALUES (?, ?, ?, ?, 'running')""",
                (program_id, task_id, actor, now_micros()),
            )
            return sorted(methods)

    def _run_worker(
        self,
        actor: str,
        task_id: str,
        prefix: str,
        source: str,
        inputs: object,
        limits: CodeLimits,
        bindings: list[str],
    ) -> CodeRun:
        started = time.monotonic()
        calls = 0
        attempts: list[str] = []
        reason = "worker_error"
        try:
            with subprocess.Popen(
                [sys.executable, "-I", "-B", "-m", _WORKER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                close_fds=True,
                cwd="/",
                env={"LANG": "C.UTF-8", "RAYON_NUM_THREADS": "1"},
                start_new_session=True,
            ) as process:
                worker = _Worker(process, started + limits.wall_seconds)
                try:
                    worker.send(
                        {
                            "source": source,
                            "inputs": inputs,
                            "bindings": bindings,
                            "limits": asdict(limits),
                        }
                    )
                    while True:
                        frame = worker.receive()
                        tag = frame.get("type")
                        if tag == "result" and set(frame) == {"type", "value"}:
                            encode(frame["value"], limits.output_bytes)
                            return CodeRun(
                                "completed",
                                output=frame["value"],
                                calls=calls,
                                attempt_ids=tuple(attempts),
                            )
                        if tag == "abort" and isinstance(code := frame.get("reason"), str):
                            raise _ProgramAbortError(
                                code if code in _WORKER_REASONS else "worker_error"
                            )
                        if tag != "call" or set(frame) != {"type", "binding", "args"}:
                            raise _ProgramAbortError("protocol_limit")
                        if calls >= limits.max_calls:
                            raise _ProgramAbortError("call_limit")
                        calls += 1
                        operation_id = f"{prefix}.{calls}"
                        worker.check()
                        self._invoke(
                            worker, actor, task_id, operation_id, frame, bindings, attempts
                        )
                finally:
                    worker.close()
        except _ProgramAbortError as exc:
            reason = str(exc)
        except subprocess.TimeoutExpired:
            reason = "wall_limit"
        except (WireError, OSError):
            if time.monotonic() - started >= limits.wall_seconds:
                reason = "wall_limit"
        return CodeRun("aborted", reason, calls=calls, attempt_ids=tuple(attempts))

    def _invoke(
        self,
        worker: _Worker,
        actor: str,
        task_id: str,
        operation_id: str,
        frame: dict[str, object],
        bindings: list[str],
        attempts: list[str],
    ) -> None:
        binding, arguments = frame["binding"], frame["args"]
        if not isinstance(binding, str) or binding not in bindings:
            raise _ProgramAbortError("method_unavailable")
        if not isinstance(arguments, dict):
            raise _ProgramAbortError("invalid_arguments")
        encode(arguments, MAX_ARGUMENT_BYTES)
        try:
            ticket = identifier(arguments.get("ticket_id"), "ticket ID")
        except ValueError:
            raise _ProgramAbortError("invalid_arguments") from None
        # Keep the full bounded input for the host's validation. The worker never receives
        # the admission snapshot or full provider response.
        attempts.append(operation_id)
        try:
            response = self.host.invoke(
                Request(task_id, actor, operation_id, operation_id, binding, arguments)
            )
        except (LedgerError, StoreError, sqlite3.Error, PermissionError):
            raise _ProgramAbortError("subcall_unresolved") from None
        if response.attempt.get("status") in ("uncertain", "dispatching", "reserved"):
            raise _ProgramAbortError("subcall_unresolved")
        worker.send(_allowed_result(response, ticket))
