# Run and recover a governed rollout

Use one trusted writer domain and a protected SQLite ledger on local disk. Start with the [governed binding guide](governed.md) to initialize the scope, register a proposal and evaluate it against the independent ticket source.

## Shadow and canary

An operator starts a rollout using the active generation and fresh passing evidence. Freeze the traffic source, seed, assignment share, deadline, concurrency limit and exposure first. Use UTC Unix microseconds for `ENDS_AT_US`.

```bash
forge rollout-start --db state/governed.sqlite3 --actor release-owner \
  --rollout shadow-24 --proposal C24 --evidence E24 --generation 1 \
  --mode shadow --source direct --seed trial-24 --basis-points 10000 \
  --ends-at "$ENDS_AT_US" --max-concurrent 4 --budget-microusd 5000000
forge rollout-status --db state/governed.sqlite3 --actor release-owner --rollout shadow-24
forge rollout-stop --db state/governed.sqlite3 --actor release-owner --rollout shadow-24 --generation 1
```

Shadow records candidate selection without dispatching candidate work. Check the independent ticket log before starting a new rollout in `--mode canary`. The host assigns a stable bucket to each eligible root and accounts for its revision's maximum root budget. A task and its children retain their assigned revision. Use `forge task-finish --task ID` to close a root and account for resolved effects and charges. Uncertain exposure continues to occupy rollout capacity.

`rollout-stop` changes future assignments. Use `forge rollback` to reverse a promotion at the current generation. Use `forge revision-revoke --event ID --revision DIGEST` to cut future dispatches under an unsafe revision. The ticket system retains prior effects and controls any compensation. `forge verify` reports observed traffic. `rollout-status` gives the observed eligible root count for the configured source.

## Alias leases and dispatch authority

Get a short actor-scoped lease with `forge alias-lease --lease lease-1 --ttl-seconds 15`. Pass its ID with `forge wrap --alias-lease lease-1` when the alias reader is unavailable. The writer checks the lease and its expiry, along with current dispatch permissions and budget. Alias changes or revision revocation can invalidate it. A host with an independent ledger can attest only its own spending and traffic.

## Back up, restore and reconcile

Write snapshots and manifests to protected storage. The SQLite backup includes committed state from WAL. Keep the expected snapshot digest in the operator's evidence store.

```bash
forge backup --db state/governed.sqlite3 --actor release-owner \
  --output backups/ledger.sqlite3 > backups/ledger.manifest.json
forge restore --db recovery/ledger.sqlite3 --snapshot backups/ledger.sqlite3 \
  --manifest backups/ledger.manifest.json
forge recovery-status --db recovery/ledger.sqlite3 --actor release-owner
```

The restored writer starts fenced. Quiesce the previous writer. Reconcile the interval from the snapshot through restoration against provider and control records. Reconcile claimed work using the exact attempt and logical operation key. Once the previous writer has stopped, a provably unclaimed reservation can be released with a verified `not_sent` observation through the operator SDK. An uncertain gap keeps the fence in place. If the gap contains effects, charges or lost control changes, recover a newer authoritative snapshot.

`forge recovery-resume` takes a recovery ID, audit reference, covered-through UTC timestamp in microseconds, attestation that the source was quiesced, and a zero count of post-snapshot effects or control changes. Reopen one writer and check the first new provider receipt. `forge export` returns minimized evidence for inspection and retention.

Quiesce old clients before `forge migrate --backup backups/pre-upgrade.sqlite3`. The migration preserves a version 1 backup and upgrades the live ledger to version 2. Older clients reject version 2. Keep the original wheel and version 1 backup for offline inspection. A client rollback that resumes writing needs a reconciled version 1 authority and a single active writer. Schema downgrade is unsupported.

## Experimental Code Mode

Install the optional `code-mode` extra for the Linux JavaScript executor. The base wheel includes the direct binding. Enable Code Mode explicitly for a read-only binding allowlist and start tasks using `Control.start_task` with `source="code_mode"`.

```python
from forge_check.governed.code_mode import CodeMode

executor = CodeMode(host, enabled=True, bindings=frozenset({"support.lookup"}))
run = executor.run(
    actor,
    task_id,
    "support-read-1",
    "return tools['support.lookup']({ticket_id: input.ticket_id}).result;",
    inputs={"ticket_id": "S07"},
)
```

QuickJS WebAssembly runs in a separate bounded process. The host applies the normal admission, budget and receipt path for each subcall. The worker receives explicit input and normalized responses. Limits cover CPU, wall time, memory, output and calls. A program ID is single-use across processes. The watchdog can stop the worker while an already claimed call completes under the host's provider timeout. Its receipt and reservation remain in the ledger. Reconcile uncertain work before choosing a new operation. The local executor keeps credentials and binding authority in the host. A hosted executor adds a provider and a metered runtime to the trial. [QuickJS capabilities](https://github.com/langchain-ai/quickjs-rs/tree/e086fb48f9f2b56ff1ca14de8484a32208192bba#security) · [Wasmtime security model](https://github.com/bytecodealliance/wasmtime/blob/main/docs/security.md)

Code Mode stays opt-in. The synthetic comparison measures the runtime mechanism. A release decision requires held-out evidence about program generation, the paired cost and success thresholds, and the pilot's latency SLO.
