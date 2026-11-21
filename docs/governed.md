# Run a governed support binding

The standalone `forge-os-check` wheel includes a direct host for one model and one support tool. Install it on Python 3.11 or newer in a dedicated environment. The legacy `forge-os-cli` distribution also provides a `forge` executable. [Wheel installation](check.md#try-the-support-example)

The stable PyPI wheel (`0.4.0`) starts the read-cache TTL when the host settles the result. Dispatch-based expiry starts with the `0.4.1.dev1` development wheel. [Downgrade with a warm ledger](#return-to-040-with-a-warm-ledger)

## Initialize the host

Use one trusted host and a SQLite database on its local disk. Protect the ledger directory and configuration with operating system permissions. The host authenticates callers and supplies `--actor` from that identity. Keep the CLI, direct SDK access, provider configuration, and ledger write access inside that boundary. The filesystem administrator controls the stored evidence.

Create `bootstrap.json` with these fields.

| Field | Contents |
|---|---|
| `scope` | A stable identifier for the project and environment, such as `support.test`. |
| `initial` | The initial revision manifest. Use the same scope as the host. |
| `cases` | The version 1 [business case document](check.md#supply-a-case-from-your-own-agent), with at least one eligible case and an independent evidence source. |
| `principals` | A map from authenticated IDs to `roles` and `bindings` arrays. Workers need `invoke` and their exact binding IDs. Assign `propose`, `propose_automatic`, `evidence`, `approve`, `inspect`, `reconcile`, and `admin` to the corresponding host identities. Automatic proposers can register and propose revisions. Approval, evidence submission, and administration require separate authority. |
| `evidence_ttl_seconds` | The release evidence lifetime in seconds. The default is 3,600. |

Use a version 1 manifest like this for `initial`. This fixture declares a million micro-USD of budget per root task. The tool is a reading operation with a fixed provider cap and the option of caching a minimized result.

```json
{
  "schema_version": 1,
  "scope": "support.test",
  "budget_microusd": 1000000,
  "policy": {"allowed_bindings": ["support.model", "support.lookup"]},
  "check": {"rule_id": "support-routing-v1", "verifier": "forge-check/1"},
  "bindings": {
    "support.model": {
      "kind": "model", "version": "1", "contract": {}, "tariff": "fixture-v1",
      "reserve_microusd": 600000, "provider": "fixture", "model": "fixture-model-v1",
      "prompt": "Route the support ticket.", "max_output_tokens": 128
    },
    "support.lookup": {
      "kind": "tool", "version": "1", "contract": {}, "tariff": "fixture-v1",
      "reserve_microusd": 50000, "server": "tickets", "method": "lookup",
      "effect": "read", "idempotency": true, "cache_ttl_seconds": 30
    }
  }
}
```

```bash
install -d -m 700 state
forge bootstrap --db state/governed.sqlite3 --config bootstrap.json
```

The host freezes the case set, verifier and implemented binding catalog at bootstrap. A registration validates the scope and catalog and derives a SHA-256 revision ID from the canonical, defaulted manifest. Amounts use integer micro-USD, and canonical numbers stay within the safe JSON integer range. Bindings derive the model, inline prompt, output limit and tool method from that revision. The requested model alias and any provider-confirmed model version appear separately in the evidence.

## Call the binding

Configure `FORGE_PROVIDER_ORIGIN` in the trusted host. The reference provider protocol uses `POST /dispatch` with JSON, NDJSON for streaming, and `GET /operations/{operation_key}` to reconcile an uncertain effect or charge. The provider must honor the configured price cap for the strict budget guarantee. Supply `FORGE_FINGERPRINT_KEY` as a stable hex-encoded key of at least 32 bytes. For an authenticated provider, also supply `FORGE_PROVIDER_TOKEN` through the host's secret configuration.

Each terminal provider observation echoes the `attempt_id` and `operation_key` from the request. Reconciliation sends the physical attempt in `X-Forge-Attempt`. The host keeps the reservation when an observation refers to a different attempt. The charge field represents the final metered amount for that attempt. A partial estimate leaves the final charge unknown.

```bash
forge wrap --db state/governed.sqlite3 --actor support-worker \
  --task ticket-s07 --worker worker-a --case S07 \
  --binding support.lookup --operation lookup --attempt lookup-1 \
  --provider "$FORGE_PROVIDER_ORIGIN" --input '{"ticket_id":"S07"}'
```

Use `--input-file` to read the JSON payload from a protected file. For a model call, select `support.model` and supply model input such as `{"ticket_id":"S07","text":"Route this support ticket."}` with a distinct operation and attempt ID. Reuse the task ID and its start metadata to keep the original `estable` generation across subcalls. Use `--parent` with a new task ID to share the original root budget and revision. `wrap` reports the attempt as JSON and returns `0` for a cache hit or a verified success with a final charge, `1` for denial or a known failure, `2` for an unresolved outcome, and `3` for invalid input or ledger failure. A provider may report `succeeded` before confirmation or final billing. Forge preserves that report in the attempt, withholds the result, and returns `2`. Use `forge reconcile` with the same attempt to settle it.

The tool accepts `ticket_id`. The model accepts `ticket_id` and `text`. The host supplies the effective model, prompt, output limit, and tool method separately from those inputs.

The equivalent installed SDK integration runs at the framework's actual model or tool dispatch point.

```python
import os

from forge_check.governed.control import Control
from forge_check.governed.db import Store
from forge_check.governed.host import Host, HttpProvider
from forge_check.governed.ledger import Request

store = Store("state/governed.sqlite3", create=False)
host = Host(
    store,
    fingerprint_key=bytes.fromhex(os.environ["FORGE_FINGERPRINT_KEY"]),
    provider=HttpProvider(os.environ["FORGE_PROVIDER_ORIGIN"],
                          token=os.environ.get("FORGE_PROVIDER_TOKEN")),
)
actor = "support-worker"  # Supplied by the trusted caller boundary.
Control(store).start_task(actor, "ticket-s07", worker="worker-a", case_id="S07")
attempt = host.invoke(Request("ticket-s07", actor, "lookup", "lookup-1",
                              "support.lookup", {"ticket_id": "S07"}))
```

| Operation | Evidence and limits |
|---|---|
| Run | The host checks permissions and records admission and a monetary reserve before external I/O. A missing price cap blocks strict admission. |
| Stream | Add `--stream` or call `Host.stream(request)`. Chunk frames precede a terminal attempt. A missing terminal provider observation leaves an unresolved attempt. |
| Cancel | Pass a `threading.Event` to `Host.stream`. A cancellation before dispatch confirms zero provider exposure. After dispatch, reconcile uncertain cost and effect through `forge reconcile` with the original attempt ID. |
| Cache | An explicitly cacheable read can return a minimized result within its TTL and the same revision, actor, and binding scope. The TTL starts at settlement in `0.4.0` and at durable dispatch intent in `0.4.1.dev1`. At the exact expiry the entry is already ineligible. A hit records the original attempt, its exclusive expiry and zero provider charge. |
| Retry | Use a new attempt ID with `--retry-of` and the same logical operation and input. Forge checks eligibility and the provider's idempotency capability. Replaying an existing attempt ID returns its recorded state. |

For example, take a two-second TTL, a dispatch at T and a verified result settled at T+1 s. On `0.4.0` that entry can serve a new read until T+3 s. On `0.4.1.dev1` it expires at T+2 s. A slow verified response can still reach its original caller. The source can also change within either window. For a new provider lookup on each new authorized operation, set `cache_ttl_seconds` to zero on the binding in the pinned revision. Use a source version or timestamp when the business rule requires independent freshness.

The root budget covers recognized charges and unsettled obligations. A lost response retains its reserve. Reconciliation adds provider observations and deduplicates billing references. A reported charge above its cap is recorded and suspends the binding. Receipts record identifiers, configuration fingerprints and confirmation levels. Payloads and provider responses flow through the host and can appear on `wrap` stdout, so protect the calling process and output. A receipt can establish what the host observed. The independent ticket system establishes the business effect.

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

**Schema v3 (`0.4.1.dev2`).** Keep the v3 writer on the current ledger. To undo the capacity optimization, an admin can switch the calculator to `reference`. Receipts, balances and in-flight work stay on that ledger. [Revert the calculator on v3](operations.md#revert-the-capacity-calculator-on-v3)

The `0.4.0` wheel rejects v3 even after all cache entries expire. If you need that binary, restore a sealed v2 snapshot to a separate, fenced ledger with a v2-compatible tool. Before handing over, drain and stop the v3 writers and their supervisors. Reconcile the snapshot through the final v3 write against the independent journal, including effects, charges and control changes. Open a single v2 writer only when the recovery checks pass. If a gap remains, keep the v2 copy fenced and recover forward on v3. [Fenced recovery](operations.md#back-up-restore-and-reconcile)

**Schema v2 (`0.4.1.dev1` to `0.4.0`).** The steps below return the older wheel to the same v2 ledger. It trusts the expiry stored on each row, so an entry originally written by `0.4.0` can become eligible again after `0.4.1.dev1` would have rejected it. The `forge rollback` command changes the alias for future tasks and keeps the installed wheel.

1. Stop new admissions and hold freshness-dependent promotions and canaries. Drain pinned tasks, finish in-flight calls and reconcile pending effects. Stop all hosts, background writers and restart supervisors that share this ledger.
2. On the drained ledger, recheck the schema and find the last stored expiry across all entries in the same read-only snapshot. Proceed only if the first line is `2` and the command succeeds.

    ```bash
    sqlite3 -readonly -bail -list -noheader "$LEDGER" \
      'BEGIN; PRAGMA user_version; SELECT MAX(expires_at) AS latest_expiry_us FROM read_cache; COMMIT;'
    ```

3. Keep writers stopped until the host's UTC clock in Unix microseconds reaches or exceeds `latest_expiry_us`. An empty cache returns `NULL` (a blank second line in the CLI) and requires no wait. At the boundary every pre-switch entry has expired. Restart the host with the `0.4.0` wheel on the same v2 ledger.
4. New entries written by `0.4.0` again expire relative to settlement. If a workflow requires dispatch-bounded freshness, keep it on hold or route new operations through an approved revision with `cache_ttl_seconds: 0` on each relevant binding. Check the revision actually pinned to the root, including canary assignments. Children inherit that pin, and replaying an attempt ID returns its recorded state. Re-evaluate freshness-dependent approvals with fresh observations and a new evidence ID before resuming them.

## Promote, roll back, and inspect

`estable` starts at generation `1`. Register a proposed manifest with `forge register --manifest candidate.json`, then create a proposal with `forge propose --proposal P42 --revision "$CANDIDATE_DIGEST"`. For an automatic proposer, set `--source automatic` and use the actor with the `propose_automatic` role. All of these commands take `--db` and the authenticated `--actor`.

An actor with `evidence` uses `forge evaluate --proposal P42 --evidence E42 --generation 1 --observed-at "$OBSERVED_AT_US" --baseline baseline.json --candidate candidate-run.json`. Use Unix UTC microseconds for the observation time and the exact immutable revision digests in both run documents. Have the trusted observer read the independent source frozen at bootstrap, such as the ticket queue. Evidence binds the proposal, revision pair, generation, cases, verifier and observation window. Omitting the baseline gives insufficient evidence unless an independent observation already proves a violation.

An actor with `approve` uses `forge promote --proposal P42 --evidence E42 --decision D42 --generation 1`. A fresh `pass` can move the alias to generation `2`. A stale generation yields a conflict. Replaying `D42` with the same arguments returns the recorded decision. `forge rollback --decision R42 --generation 2` creates a new generation pointing to the previous revision. New tasks receive the current revision. Existing tasks keep their original pin. Use `forge pause` to stop admissions of new roots and `forge revoke` to remove a principal's permission for future dispatches.

```bash
forge verify --db state/governed.sqlite3 --actor release-owner --decision R42 \
  --since 2026-11-19T20:00:00Z --until 2026-11-19T21:00:00Z
```

Use a half-open UTC window that includes the traffic under review. The report groups starts and observed tasks by revision, generation and source, with unresolved cases and the selected decision. For a coverage denominator, provide an independently attested `--roster` with `source`, `since`, `until`, and `workers` for the same window. The roster requires both `inspect` and `evidence` permissions. A silent worker has unknown effective state. Calls outside the explicit binding are unknown, and alias rollback affects future starts. The ticket system owns compensation for any prior effect.

[Conditional inventory evidence](conditional-evidence.md) · [Rollout, recovery and experimental Code Mode](operations.md)
