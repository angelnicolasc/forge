# Run a governed support binding

The standalone `forge-os-check` wheel includes a direct host for one model and one support tool. Install it on Python 3.11 or newer in a dedicated environment. The legacy `forge-os-cli` distribution also provides a `forge` executable. [Wheel installation](check.md#try-the-support-example)

The stable PyPI wheel (`0.4.0`) starts the read-cache TTL when the host settles the result. Dispatch-based expiry starts with the `0.4.1.dev1` development wheel. [Downgrade with a warm ledger](operations.md#return-to-040-with-a-warm-ledger)

## Initialize the host

Use one trusted host and a SQLite database on its local disk. Protect the ledger directory and configuration with operating system permissions. Your integrating application must authenticate callers and derive `--actor` from that identity. Keep the CLI, direct SDK access, provider configuration, and ledger write access inside that boundary. The filesystem administrator controls the stored evidence.

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

## Operating the binding

<a id="promote-roll-back-and-inspect"></a>
<a id="upgrade-from-settlement-based-cache-expiry"></a>
<a id="return-to-040-with-a-warm-ledger"></a>

The operator guide covers [evaluating and promoting a revision](operations.md#promote-roll-back-and-inspect), [restoring the ledger](operations.md#back-up-restore-and-reconcile), [upgrading cache expiry](operations.md#upgrade-from-settlement-based-cache-expiry), and [returning to 0.4.0](operations.md#return-to-040-with-a-warm-ledger).
