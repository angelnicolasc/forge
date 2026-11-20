"""Process entry point for the optional JavaScript sandbox."""

from __future__ import annotations

import os
import resource
import sys
from importlib import import_module
from math import ceil
from typing import TYPE_CHECKING

from forge_check.governed._code_wire import (
    MAX_ARGUMENT_BYTES,
    MAX_FRAME_BYTES,
    WireError,
    decode,
    document,
    encode,
    write_frame,
)

if TYPE_CHECKING:
    from typing import BinaryIO

_EVALUATE = """(function (source, names, data, limit) {
    'use strict';
    const rpc = globalThis.__forge_rpc;
    Reflect.deleteProperty(globalThis, '__forge_rpc');
    const parse = JSON.parse;
    const stringify = JSON.stringify;
    const tools = Object.create(null);
    for (const name of names) {
        tools[name] = args => parse(rpc(name, stringify(args)));
    }
    const run = Function('tools', 'input', '"use strict";\\n' + source);
    const value = stringify(run(Object.freeze(tools), parse(data)));
    if (value === undefined) throw new TypeError('Expected a JSON result');
    return value.length > limit ? null : value;
})"""


def _read_frame(stream: BinaryIO) -> dict[str, object]:
    data = stream.readline(MAX_FRAME_BYTES + 2)
    if not data.endswith(b"\n") or len(data) > MAX_FRAME_BYTES + 1:
        raise WireError("Invalid incoming frame")
    return document(data)


def _limit_process(seconds: int) -> None:
    elapsed = resource.getrusage(resource.RUSAGE_SELF)
    # RLIMIT_CPU is process-wide and counts setup time. Preserve the hard cap.
    budget = ceil(elapsed.ru_utime + elapsed.ru_stime) + seconds
    _, existing = resource.getrlimit(resource.RLIMIT_CPU)
    maximum = min(budget + 1, existing) if existing != resource.RLIM_INFINITY else budget + 1
    resource.setrlimit(resource.RLIMIT_CPU, (min(budget, maximum), maximum))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _integer(frame: dict[str, object], key: str, minimum: int, maximum: int) -> int:
    value = frame.get(key)
    if type(value) is not int or not minimum <= value <= maximum:
        raise WireError("Invalid executor limit")
    return value


def _execute(stream: BinaryIO, config: dict[str, object]) -> None:
    # The core wheel imports no optional runtime. The worker owns the engine and its JIT.
    try:
        engine = import_module("quickjs_rs")
    except ImportError:
        write_frame(stream, {"type": "abort", "reason": "runtime_unavailable"})
        return

    limits = config["limits"]
    if not isinstance(limits, dict):
        raise WireError("Invalid executor limits")
    cpu = _integer(limits, "cpu_seconds", 1, 10)
    memory = _integer(limits, "memory_bytes", 1024 * 1024, 64 * 1024 * 1024)
    output = _integer(limits, "output_bytes", 32, 16 * 1024)
    wall = limits.get("wall_seconds")
    if not isinstance(wall, (int, float)) or isinstance(wall, bool) or not 0.1 <= wall <= 20:
        raise WireError("Invalid executor timeout")
    _limit_process(cpu)

    def invoke(binding: str, raw: str) -> str:
        if not isinstance(binding, str) or not isinstance(raw, str):
            raise WireError("Invalid capability arguments")
        if len(raw.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise WireError("Capability arguments exceed their limit")
        write_frame(stream, {"type": "call", "binding": binding, "args": document(raw)})
        response = _read_frame(sys.stdin.buffer)
        return encode(response).decode("ascii")

    arguments = [config["source"], config["bindings"], encode(config["inputs"]).decode(), output]
    # Source and arguments enter the trusted wrapper as JSON values. QuickJS gets no loader,
    # paths, environment, network, or direct host callback apart from the bounded RPC.
    script = _EVALUATE + "(" + ",".join(encode(value).decode("ascii") for value in arguments) + ")"
    try:
        with (
            engine.Runtime(
                memory_limit=memory, transform_flags=engine.SourceTransform.NONE, clock=lambda: 0
            ) as runtime,
            runtime.new_context(timeout=wall) as context,
        ):
            context.function(invoke, name="__forge_rpc")
            serialized = context.eval(script)
    except engine.TimeoutError:
        write_frame(stream, {"type": "abort", "reason": "wall_limit"})
        return
    except engine.MemoryLimitError:
        write_frame(stream, {"type": "abort", "reason": "memory_limit"})
        return
    except Exception:
        # Exceptions and stacks can contain source, input values, and provider details.
        write_frame(stream, {"type": "abort", "reason": "program_error"})
        return
    if not isinstance(serialized, str) or len(serialized.encode("utf-8")) > output:
        write_frame(stream, {"type": "abort", "reason": "output_limit"})
        return
    result = decode(serialized)
    try:
        encode(result, output)
    except WireError:
        write_frame(stream, {"type": "abort", "reason": "output_limit"})
        return
    write_frame(stream, {"type": "result", "value": result})


def main() -> int:
    # WASI diagnostics inherit the worker's output descriptors. Detach them from RPC.
    with os.fdopen(os.dup(1), "wb") as protocol, open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        try:
            _execute(protocol, _read_frame(sys.stdin.buffer))
        except (KeyError, OSError, ValueError, RecursionError):
            write_frame(protocol, {"type": "abort", "reason": "worker_error"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
