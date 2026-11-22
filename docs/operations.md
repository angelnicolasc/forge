# Run and recover a governed rollout

Use one trusted writer domain and a protected SQLite ledger on local disk. Start with the [governed binding guide](governed.md) to initialize the scope and authorize a support lookup.

## Ledger storage and durability

The host sets SQLite to write-ahead logging (WAL) with `synchronous=FULL`. Each committed write transaction requires a WAL sync. The time spent waiting for durable writes depends on the storage device and its filesystem. Record the filesystem, mount type, host and SQLite version when comparing benchmark results. A host invocation can include several transactions, so its duration alone cannot establish the cost of a disk sync.

Keep all processes using the ledger on one host and the database files on local disk. SQLite supports concurrent readers and one writer at a time in WAL mode. It requires shared memory for those processes and does not support WAL on a network filesystem. The [recovery drill](https://github.com/angelnicolasc/forge/blob/main/benchmarks/README.md#what-survives-a-process-crash) interrupts the writer process. Its results cover process failure with the host and storage still running. [SQLite WAL](https://sqlite.org/wal.html) · [Durability settings](https://sqlite.org/pragma.html#pragma_synchronous)

## Promote, roll back, and inspect

The ledger’s stable release alias is named `estable`. It starts at generation `1` and identifies the revision assigned to new tasks. Register a proposed manifest with `forge register --manifest candidate.json`, then create a proposal with `forge propose --proposal P42 --revision "$CANDIDATE_DIGEST"`. For an automatic proposer, set `--source automatic` and use the actor with the `propose_automatic` role. All of these commands take `--db` and the authenticated `--actor`.

An actor with `evidence` uses `forge evaluate --proposal P42 --evidence E42 --generation 1 --observed-at "$OBSERVED_AT_US" --baseline baseline.json --candidate candidate-run.json`. Use Unix UTC microseconds for the observation time and the exact immutable revision digests in both run documents. Have the trusted observer read the independent source frozen at bootstrap, such as the ticket queue. Evidence binds the proposal, revision pair, generation, cases, verifier and observation window. Omitting the baseline gives insufficient evidence unless an independent observation already proves a violation.

An actor with `approve` uses `forge promote --proposal P42 --evidence E42 --decision D42 --generation 1`. A fresh `pass` can move the alias to generation `2`. A stale generation yields a conflict. Replaying `D42` with the same arguments returns the recorded decision. `forge rollback --decision R42 --generation 2` creates a new generation pointing to the previous revision. New tasks receive the current revision. Existing tasks keep their original pin. Use `forge pause` to stop admissions of new roots and `forge revoke` to remove a principal's permission for future dispatches.

```bash
forge verify --db state/governed.sqlite3 --actor release-owner --decision R42 \
  --since 2026-11-19T20:00:00Z --until 2026-11-19T21:00:00Z
```

Use a half-open UTC window that includes the traffic under review. The report groups starts and observed tasks by revision, generation and source, with unresolved cases and the selected decision. For a coverage denominator, provide an independently attested `--roster` with `source`, `since`, `until`, and `workers` for the same window. The roster requires both `inspect` and `evidence` permissions. A silent worker has unknown effective state. Calls outside the explicit binding are unknown, and alias rollback affects future starts. The ticket system owns compensation for any prior effect.

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

`rollout-stop` changes future assignments. Use `forge rollback` to reverse a promotion at the current generation. Use `forge revision-revoke --event ID --revision DIGEST` to block new dispatch intents under an unsafe revision. An intent already recorded at the time of revocation can still reach the destination. Reconcile it by attempt ID. The ticket system retains prior effects and controls any compensation. `forge verify` reports observed traffic. `rollout-status` gives the observed eligible root count for the configured source.

### Read the traffic counters

In builds that include `traffic_by_cohort`, `forge rollout-status` returns one row per observed cohort. A rollout starts with an empty list. The counters cover its lifetime, including activity from existing roots after assignment stops. Each child belongs to its root's cohort.

| Field | Meaning |
| --- | --- |
| `allocated_roots` | Distinct roots assigned to this cohort, including roots with no calls. |
| `roots_with_dispatch_intent` | Roots for which the binding recorded at least one dispatch intent. |
| `dispatch_intents` | Distinct attempts that reached the dispatch boundary. A replay or cache hit introduces no additional intent. |
| `roots_pending_confirmation` | Roots with a dispatch in flight, an uncertain outcome, or missing confirmation of its outcome or charge. A reservation awaiting its first dispatch intent appears in the root's budget. |

An intent records a local decision to send. Use the destination journal to confirm arrival. A `shadow` row counts traffic served by the stable revision. The rollout's `exposure_microusd` covers canary roots. A root can have several attempts and contributes at most one to each root count. In 0.4.0, use the allocation report, binding receipts and destination journal to establish these counts.

## Alias leases and dispatch authority

Get a short actor-scoped lease with `forge alias-lease --lease lease-1 --ttl-seconds 15`. Pass its ID with `forge wrap --alias-lease lease-1` when the alias reader is unavailable. The writer checks the lease and its expiry, along with current dispatch permissions and budget. Alias changes or revision revocation can invalidate it. A host with an independent ledger can attest only its own spending and traffic.

## Back up, restore and reconcile

Write snapshots and manifests to protected storage. The SQLite backup includes committed state from WAL. Keep the expected snapshot digest in the operator's evidence store.

```bash
set -eC
umask 077
mkdir -p backups recovery
forge backup --db state/governed.sqlite3 --actor release-owner \
  --output backups/ledger.sqlite3 > backups/ledger.manifest.json
forge restore --db recovery/ledger.sqlite3 --snapshot backups/ledger.sqlite3 \
  --manifest backups/ledger.manifest.json
forge recovery-status --db recovery/ledger.sqlite3 --actor release-owner
```

The restored writer starts fenced. Quiesce the previous writer. Reconcile the interval from the snapshot through restoration against provider and control records. Reconcile claimed work using the exact attempt and logical operation key. Once the previous writer has stopped, a provably unclaimed reservation can be released with a verified `not_sent` observation through the operator SDK. An uncertain gap keeps the fence in place. If the gap contains effects, charges or lost control changes, recover a newer authoritative snapshot.

`forge recovery-resume` takes a recovery ID, audit reference, covered-through UTC timestamp in microseconds, attestation that the source was quiesced, and a zero count of post-snapshot effects or control changes. Reopen one writer and check the first new provider receipt. `forge export` returns minimized evidence for inspection and retention.

## Migrate a governed ledger

Choose the wheel for the destination schema. The version in the backup manifest describes the source.

| Installed wheel | New ledger | Explicit migration |
| --- | --- | --- |
| Published `forge-os-check==0.4.0` | v2 | v1 → v2 |
| ADR 0005 checkout, `0.4.1.dev2` | v3, reference calculator | v1 → v3 or v2 → v3. A v1 source passes through v2 in the same transaction. |

Stop admission and drain every worker with an outstanding dispatch intent. A call authorized before migration can still reach its provider. Record outstanding attempt IDs and operation keys, compare them with the destination journal, and stop all old writers and their restart supervisors. Keep the original wheel. The migration takes the SQLite writer lock, snapshots committed WAL state, and backfills capacity before committing v3.

For the v3 upgrade, activate the candidate wheel's virtual environment on the trusted host. Confirm `python -m pip show forge-os-check` reports `0.4.1.dev2` and `command -v forge` points into that environment. Supply the live ledger path and the authenticated principals with `admin` and `inspect` roles. The restore below rehearses recovery in a fresh, fenced copy.

```bash
set -e
umask 077
LEDGER=${LEDGER:-state/governed.sqlite3}
ADMIN=${ADMIN:-release-owner}
INSPECTOR=${INSPECTOR:-release-owner}
BACKUP_ROOT=${BACKUP_ROOT:-backups}
mkdir -p "$BACKUP_ROOT"
RUN=$(mktemp -d "$BACKUP_ROOT/migrate-XXXXXX")
forge migrate --db "$LEDGER" --actor "$ADMIN" \
  --backup "$RUN/before.sqlite3" > "$RUN/before.manifest.json"
forge recovery-status --db "$LEDGER" --actor "$INSPECTOR" > "$RUN/live.json"
forge restore --db "$RUN/drill.sqlite3" --snapshot "$RUN/before.sqlite3" \
  --manifest "$RUN/before.manifest.json" > "$RUN/restore.json"
forge recovery-status --db "$RUN/drill.sqlite3" --actor "$INSPECTOR" > "$RUN/drill.json"
cat "$RUN/before.manifest.json" "$RUN/live.json" "$RUN/drill.json"
```

With the candidate, both status reports should say `schema_version: 3`. Check the scope, alias generation and receipt high-water mark against the manifest while writers are quiesced. The drill should report `fenced: true` and the manifest's SHA-256 as `snapshot_sha256`. Retain the sealed snapshot, manifest and their provenance in protected storage. Review unresolved attempts before reopening one compatible writer on the live ledger. Keep the drill fenced. For an actual restore, follow [Back up, restore and reconcile](#back-up-restore-and-reconcile).

After an interrupted migration, keep writers stopped and inspect `PRAGMA user_version` through a read-only SQLite connection. An aborted backfill rolls back the live transaction but can leave a sealed snapshot behind. Use a fresh output directory for a retry from v1 or v2. If v3 committed, inspect its status and reconcile against the provider before reopening admission.

### Revert the capacity calculator on v3

The candidate uses canonical accounting by default. If a lab enabled the projected calculator, switch the same current ledger back to `reference` with the candidate environment and the trusted host's admin identity. This preserves receipts, in-flight tasks and current rollout state.

```bash
python - "$LEDGER" "$ADMIN" <<'PY'
import sys
from pathlib import Path

from forge_check.governed import rollout
from forge_check.governed.control import require_role
from forge_check.governed.db import Store

store = Store(Path(sys.argv[1]), create=False)
with store.write() as conn:
    require_role(conn, sys.argv[2], "admin")
    rollout.set_capacity_mode(conn, "reference")
PY
```

Check `forge rollout-status --db "$LEDGER" --actor "$INSPECTOR" --rollout "$ROLLOUT_ID"` for the affected rollout. Match `consumed_microusd` to the charges for all canary roots and their children. Each closed root contributes its charges to `exposure_microusd`. Each open root contributes the larger of its allocated cap and its charges.

A pre-v3 wheel rejects the current v3 ledger. Keep older wheels and their matching snapshots for offline inspection. Returning to an older writer requires a separately reconciled authority at its supported schema. If post-snapshot effects, charges or control changes remain unaccounted for, keep that writer offline and recover forward on v3. [Capacity decision and lab results](decisions/0005-rollout-capacity.md)

## Change the cache expiry policy

### Upgrade from settlement-based cache expiry

Drain the `0.4.0` writers before switching their ledger to `0.4.1.dev1`. When the new host reads a warm entry, it uses the earlier of its stored expiry and the original dispatch time plus TTL. Reading the entry leaves its stored expiry intact. Receipts, evidence certificates and release decisions already recorded keep their original expiry or verdict. An alias change affects new tasks. Existing tasks keep their pinned revision and any effects they have already produced.

Apply the following steps to scopes whose promotion or canary checks depend on the freshness of cached reads.

1. Hold new promotions and canary starts while switching hosts. Stop an open canary if further allocations need fresh evidence. Track already pinned tasks to completion and reconcile pending effects by their original attempt ID.
2. Keep pre-upgrade exports and certificates for audit. Remove them from the observer's resubmission queue and from pending approval jobs. A stored certificate can still authorize a change until its original deadline.
3. After the upgrade, have the trusted observer corroborate the relevant reads against the independent destination. For any cache hit it exports, include the matching origin and the new receipt's `valid_until`. Evaluate fresh evidence under a **new evidence ID** for the target revisions and current generation. Resume approvals with that certificate after a fresh `pass`.

### Return to 0.4.0 with a warm ledger

Before stopping admissions or writers, set `LEDGER` to the path used by the running host. Coordinate with the operator responsible for migrations so the schema stays fixed during the rollback. Use the SQLite CLI to inspect the live file in a single read-only transaction.

```bash
sqlite3 -readonly -bail -list -noheader "$LEDGER" \
  'BEGIN; PRAGMA application_id; PRAGMA user_version; PRAGMA journal_mode; COMMIT;'
```

Read the three output lines in order. A live Forge ledger reports application ID `1179603527`, its schema version, and journal mode `wal`. If the command fails, the identity or journal mode differs, or the version is outside the two cases below, leave the writers running and verify the path and installed version.

**Schema v3 (`0.4.1.dev2`).** Keep the v3 writer on the current ledger. To undo the capacity optimization, an admin can switch the calculator to `reference`. Receipts, balances and in-flight work stay on that ledger. [Revert the calculator on v3](#revert-the-capacity-calculator-on-v3)

The `0.4.0` wheel rejects v3 even after all cache entries expire. If you need that binary, restore a sealed v2 snapshot to a separate, fenced ledger with a v2-compatible tool. Before handing over, drain and stop the v3 writers and their supervisors. Reconcile the snapshot through the final v3 write against the independent journal, including effects, charges and control changes. Open a single v2 writer only when the recovery checks pass. If a gap remains, keep the v2 copy fenced and recover forward on v3. [Fenced recovery](#back-up-restore-and-reconcile)

**Schema v2 (`0.4.1.dev1` to `0.4.0`).** The steps below return the older wheel to the same v2 ledger. It trusts the expiry stored on each row, so an entry originally written by `0.4.0` can become eligible again after `0.4.1.dev1` would have rejected it. The `forge rollback` command changes the alias for future tasks and keeps the installed wheel.

1. Stop new admissions and hold freshness-dependent promotions and canaries. Drain pinned tasks, finish in-flight calls and reconcile pending effects. Stop all hosts, background writers and restart supervisors that share this ledger.
2. On the drained ledger, recheck the schema and find the last stored expiry across all entries in the same read-only snapshot. Proceed only if the first line is `2` and the command succeeds.

    ```bash
    sqlite3 -readonly -bail -list -noheader "$LEDGER" \
      'BEGIN; PRAGMA user_version; SELECT MAX(expires_at) AS latest_expiry_us FROM read_cache; COMMIT;'
    ```

3. Keep writers stopped until the host's UTC clock in Unix microseconds reaches or exceeds `latest_expiry_us`. An empty cache returns `NULL` (a blank second line in the CLI) and requires no wait. At the boundary every pre-switch entry has expired. Restart the host with the `0.4.0` wheel on the same v2 ledger.
4. New entries written by `0.4.0` again expire relative to settlement. If a workflow requires dispatch-bounded freshness, keep it on hold or route new operations through an approved revision with `cache_ttl_seconds: 0` on each relevant binding. Check the revision actually pinned to the root, including canary assignments. Children inherit that pin, and replaying an attempt ID returns its recorded state. Re-evaluate freshness-dependent approvals with fresh observations and a new evidence ID before resuming them.

## Experimental Code Mode

The [Code Mode guide](code-mode.md) describes the optional Linux executor, task-scoped discovery, the model response format and the result boundary. Each subcall uses the active dispatch authority for its pinned task. The direct binding remains available as its own route. Reconcile unresolved work before choosing a new operation.

Code Mode stays opt-in. Default selection requires held-out evidence about the model and task family, the paired cost and success thresholds, and the pilot's latency SLO.
