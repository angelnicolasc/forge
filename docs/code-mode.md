# Compose governed reads with Code Mode

Code Mode is an experimental Linux SDK profile for composing explicitly enabled read bindings in JavaScript. The base wheel supports governed calls through `Host.invoke`. Install the optional executor in the environment that will run programs.

```bash
python -m pip install "${FORGE_WHEEL}[code-mode]"
```

## Describe an inventory operation

Use a version 2 revision for new operation contracts. Add `inventory.stock` to the revision's `bindings`, its admission policy, and the approved actor's binding grants. The subject of the business rule and the trusted observation source are configured when you [initialize the host](governed.md#initialize-the-host).

```json
{
  "kind": "tool",
  "version": "stock-1",
  "server": "inventory",
  "method": "stock",
  "effect": "read",
  "tariff": "stock-1",
  "reserve_microusd": 40,
  "cache_ttl_seconds": 30,
  "contract": {
    "schema_version": 1,
    "input": {
      "type": "object",
      "properties": {
        "sku": {"type": "string", "format": "identifier"}
      },
      "required": ["sku"],
      "additionalProperties": false
    },
    "output": {
      "type": "object",
      "properties": {
        "sku": {"type": "string", "format": "identifier"},
        "on_hand": {"type": "integer", "minimum": 0},
        "internal_cost": {"type": "integer", "minimum": 0}
      },
      "required": ["sku", "on_hand"],
      "additionalProperties": false
    },
    "visible": ["sku", "on_hand"]
  }
}
```

The host rejects undeclared input fields before dispatch. It validates and projects the response before exposing it to the caller or placing it in the read cache. This example returns `sku` and `on_hand` on both routes. The provider may record `internal_cost` in the destination while the binding keeps that field out of caller results. A verified upstream charge can remain recorded when the response fails the output contract.

## Run the inventory quickstart

From the repository root, run the example in the environment with the installed wheel and the `code-mode` extra. It starts a loopback inventory provider and initializes a temporary SQLite ledger. It uses a fixed model response fixture and requires no service credentials.

```bash
python examples/governed_inventory.py
```

The direct task reads `A-18` through `Host.invoke`. The program task discovers `inventory.stock` and passes a versioned response to `CodeMode.run_model`. Its program reads `A-18` and `B-42` within the same task and returns the sum. The example checks the projected direct result, the typed program output and the three calls recorded at the destination.

```json
{
  "direct": {"on_hand": 7, "sku": "A-18"},
  "program": {"total_on_hand": 9},
  "dispatches": ["A-18", "A-18", "B-42"]
}
```

`capabilities` returns the pinned revision, configured limits, and the input and projected output schemas of the read operations the actor can currently discover. Querying it performs no provider calls and takes no reservation. Permissions, revision revocation and budget are checked again at each dispatch.

## Request a program from a model

Use `model_program_schema()` or the `model_program_schema` entry in the capability catalog to describe the response. The model returns one JSON object with `schema_version` set to the integer `1` and `source` containing a JavaScript function body. Markdown fences, surrounding prose and extra envelope fields are format errors.

The body receives the supplied JSON as `input`. Call `tools['binding.id'](arguments)` synchronously for a `{status, confirmation, result}` envelope. Read its projected `result` before using its contents. Return JSON data explicitly. The application owns `output_schema` and passes it separately when it needs a particular result shape. Applications providing trusted source directly can use `CodeMode.run` with the same inputs and optional output schema.

| Failure phase | Example | Stable reason |
|---|---|---|
| `format` | Decorated output, an unknown envelope version or malformed JSON | `invalid_model_program` |
| `compilation` | Invalid JavaScript syntax | `compilation_error` |
| `output_contract` | The returned JSON has the wrong shape or type | `invalid_program_output` |
| `output_contract` | A subcall's provider result fails the binding contract | `subcall_output_contract` |
| `output_contract` | The result cannot be serialized as JSON or exceeds its size bound | `output_serialization` or `output_limit` |
| `runtime` | The program throws, an operation remains uncertain, or a runtime bound is reached | `program_error`, `subcall_unresolved`, or the violated limit |

An accepted program ID is single-use. `CodeRun` reports the execution phase and links its attempts through `attempt_ids`. There is no automatic repair or fallback. Reconcile uncertain calls before choosing another operation. Completion describes the program lifecycle. The trusted application and the evidence gate decide business correctness.

The executor keeps provider credentials and dispatch authority in the host. QuickJS runs in a fresh, bounded Wasmtime process with the selected read capabilities and explicit input. The watchdog can stop the worker while a call already handed to the host completes under the provider timeout. Its receipt and reserve remain in the ledger. [QuickJS sandbox design](https://github.com/langchain-ai/quickjs-rs/tree/e086fb48f9f2b56ff1ca14de8484a32208192bba#security) · [Wasmtime security model](https://github.com/bytecodealliance/wasmtime/blob/main/docs/security.md)
