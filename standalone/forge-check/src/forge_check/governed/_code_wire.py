"""Bounded JSON frames shared by the isolated executor and the host."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from typing import IO

MAX_FRAME_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 16 * 1024
MAX_INPUT_BYTES = 8 * 1024
MAX_ARGUMENT_BYTES = 4 * 1024


class WireError(ValueError):
    """A protocol message exceeded its scope or could not be represented."""


def encode(value: object, limit: int = MAX_FRAME_BYTES) -> bytes:
    try:
        payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise WireError("Invalid JSON value") from exc
    result = payload.encode("ascii")
    if len(result) > limit:
        raise WireError("JSON size limit exceeded")
    return result


def _invalid_constant(name: str) -> object:
    raise WireError("Non-finite JSON number")


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WireError("Duplicate JSON field")
        result[key] = value
    return result


def decode(raw: bytes | str) -> object:
    try:
        return json.loads(raw, parse_constant=_invalid_constant, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as exc:
        raise WireError("Invalid JSON frame") from exc


def document(raw: bytes | str) -> dict[str, object]:
    value = decode(raw)
    if not isinstance(value, dict):
        raise WireError("Expected a JSON object")
    return cast("dict[str, object]", value)


def write_frame(stream: IO[bytes], value: object) -> None:
    pending = memoryview(encode(value) + b"\n")
    while pending:
        written = stream.write(pending)
        if not written:
            raise WireError("Frame write stopped")
        pending = pending[written:]
    stream.flush()
