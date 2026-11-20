# Check conditional inventory evidence

A stock lookup returns 2 units. The workflow calls for a reserve lookup when primary stock falls below 5. An agent can return valid JSON and finish with `completed` after that one lookup. The Gate uses the independent record of reads to decide whether the task met the rule. [Why Forge added the rule](decisions/0003-conditional-evidence.md)

## Run the one-minute demo

From the source checkout on Linux, point `FORGE_WHEEL` to the standalone candidate wheel. Install it with the Code Mode extra in a clean environment. The fixture runs the three cases against a local inventory destination.

```bash
python3 -m venv .venv-conditional
source .venv-conditional/bin/activate
python -m pip install "${FORGE_WHEEL}[code-mode]"
python tests/acceptance/conditional_evidence.py --demo --wheel "$FORGE_WHEEL" \
  --output-dir conditional-receipts
cat conditional-receipts/demo.json
```

The following fields are from the `demo.json` produced by `0.4.0rc2`. Each scenario starts from a fresh scope at generation 1.

```json
[
  {"scenario":"omitted","executor_status":"completed",
   "destination":[{"on_hand":2,"sku":"PRI-A4"}],
   "verdict":"fail","reasons":["reserve_omitted"],
   "alias_before":1,"alias_after":1},
  {"scenario":"complete","executor_status":"completed",
   "destination":[{"on_hand":2,"sku":"PRI-A4"},{"on_hand":14,"sku":"RES-K4"}],
   "verdict":"pass","reasons":[],
   "alias_before":1,"alias_after":2},
  {"scenario":"offline","executor_status":"completed",
   "destination":[{"on_hand":2,"sku":"PRI-A4"}],
   "verdict":"insufficient","reasons":["source_unavailable"],
   "alias_before":1,"alias_after":1}
]
```

The omitted run returned `{"primary": 2}`. The observer had complete coverage, so the missing `RES-K4` lookup blocked promotion. The complete run added `{"reserve": 14}` to its output and promotion applied. In the offline run, the test destination still recorded `PRI-A4` while the observer returned HTTP 503. With no source coverage, the Gate could only report `insufficient` and leave the alias at generation 1.

## Freeze the rule and cases

In each [governed revision manifest](governed.md#initialize-the-host), set the `check` block to:

```json
{
  "rule_id": "stock-complete-v1",
  "verifier": "forge-check/2"
}
```

Put this case document in the `cases` field of the bootstrap configuration. The example associates two distinct inventory lookup IDs with the same business case. `expected` is the existing outcome label from the independent observer. In the demo, it records the base completion check. The conditional rule checks whether the run also covered the reserve obligation.

```json
{
  "schema_version": 2,
  "rule_id": "stock-complete-v1",
  "source": "inventory-journal",
  "conditional_stock": {
    "threshold": 5,
    "max_age_seconds": 120
  },
  "cases": [
    {
      "id": "conditional-01",
      "expected": "complete",
      "stock": {"primary": "PRI-A4", "reserve": "RES-K4"}
    }
  ]
}
```

At bootstrap the host pins the verifier and hashes the protected case set, including the threshold, source, freshness policy and primary–reserve mapping. The proposal identifies the candidate revision and the release report records the rule digest. Registering a revision cannot change this protected policy. Use the same inventory binding for the [direct host](governed.md#call-the-binding) and [Code Mode](code-mode.md#run-the-inventory-quickstart).

## Export observations from the trusted source

For each run, keep `schema_version: 1`, the same `rule_id` and `source`, the run’s `revision`, and its case rows. A row carries the executor's `status`, the observer's existing outcome label in `observed`, and the new `stock_evidence` record. This example is a candidate row for a program that read only `PRI-A4`. Supply your actual immutable revision ID, UTC Unix microseconds and receipt IDs.

```json
{
  "id": "conditional-01",
  "status": "completed",
  "observed": "complete",
  "stock_evidence": {
    "coverage": "complete",
    "since": 1795176000000000,
    "until": 1795176001000000,
    "observations": [
      {
        "case_id": "conditional-01",
        "revision": "r42",
        "source": "inventory-journal",
        "sku": "PRI-A4",
        "on_hand": 2,
        "observed_at": 1795176000500000,
        "kind": "read",
        "receipt": "read-primary-1"
      }
    ]
  }
}
```

The source must correlate the records with the case, revision and trial interval `[since, until)`. Mark coverage `complete` only if the trusted observation covers reads and any accepted cache uses for that trial. A destination journal alone cannot establish the absence of a cache hit when caching is enabled. Export `partial` when coverage is incomplete, and `unavailable` or omit `stock_evidence` when the source cannot attest the window. A logged attempt with an unconfirmed quantity uses `on_hand: null`.

A cache observation uses `kind: "cache"`, the cache-hit receipt, and `origin_receipt` and `origin_observed_at` for the independently verified original read of the same SKU and revision. To admit cache evidence, add `"cache_max_age_seconds": 30` to `conditional_stock`. The original read must satisfy both that limit and the general freshness limit. The observer attests receipt provenance before submitting the export. Give the `evidence` role to that trusted process and keep it separate from agent execution and automatic proposals.

## Evaluate and promote

Use the [governed release commands](governed.md#promote-roll-back-and-inspect) to register and propose the candidate. Supply both run documents and the observation timestamp from the trusted exporter. The observation timestamp must be at or after the end of each evidence window. The Gate checks the age of both runs against that timestamp. The saved comparison uses the latest window end in the input files.

```bash
forge evaluate --db state/governed.sqlite3 --actor inventory-observer \
  --proposal P42 --evidence E42 --generation 1 \
  --observed-at "$OBSERVED_AT_US" --baseline baseline.json \
  --candidate candidate-run.json > evaluation.json

forge promote --db state/governed.sqlite3 --actor release-owner \
  --proposal P42 --evidence E42 --generation 1 --decision D42
```

In `evaluation.json`, read `report.cases[].conditional_stock.candidate` alongside the base outcome and executor status. If primary stock is at least 5, one confirmed read satisfies the requirement. Below 5, a fresh, matching reserve observation satisfies it. With complete coverage, a confirmed omission returns `fail`. Missing or unconfirmed evidence returns `insufficient` unless the candidate already has a proven violation. The report identifies the subjects, coverage, window, and discarded observations so the operator can locate the records behind the verdict.

The Gate evaluates saved records without dispatching a provider call. Promotion with `fail` or `insufficient` leaves `estable` at the same revision and generation. A `pass` still requires a fresh evidence snapshot, a current alias generation and an authorized approver. The promotion window closes at the earliest applicable evidence expiry. Use `forge check` with the same cases and saved runs to review the evidence outside the ledger. [Check exit codes](check.md#add-one-step-to-your-ci)

## Roll back a release

Use `forge rollback --db state/governed.sqlite3 --actor release-owner --decision R42 --generation 2` after an applied promotion from generation 1. The next generation points to the previous compatible revision. Tasks already started retain their revision pins. To restore RC1 itself, use the retained RC1 wheel and a compatible scope initialized without this rule. A scope set up with verifier `forge-check/2` requires a verifier that understands its evidence contract.
