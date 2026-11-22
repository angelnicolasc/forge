# Check conditional inventory evidence

A stock lookup returns 2 units. The workflow calls for a reserve lookup when primary stock falls below 5. An agent can return valid JSON and finish with `completed` after that one lookup. The Gate uses the independent record of reads to decide whether the task met the rule. [Why Forge added the rule](decisions/0003-conditional-evidence.md)

<a id="run-the-one-minute-demo"></a>

## Run the inventory demo

From the repository root on Linux x86_64, download the published wheel and install its Code Mode extra in a dedicated Python 3.11+ environment. The fixture starts a local inventory service and reads its journal through a separate observer. No model credentials are needed.

```bash
DEMO_DIR=$(mktemp -d)
python3 -m venv "$DEMO_DIR/venv"
. "$DEMO_DIR/venv/bin/activate"
python -m pip download --only-binary=:all: --no-deps --dest "$DEMO_DIR" "forge-os-check==0.4.0"
FORGE_WHEEL="$DEMO_DIR/forge_os_check-0.4.0-py3-none-any.whl"
python -m pip install "${FORGE_WHEEL}[code-mode]"
python tests/acceptance/conditional_evidence.py --demo --wheel "$FORGE_WHEEL" \
  --output-dir "$DEMO_DIR/receipts"
cat "$DEMO_DIR/receipts/demo.json"
```

A `wall_limit` error means the program worker exceeded its execution deadline before the fixture obtained a completed run. Only a completed fixture produces the three Gate decisions.

These fields appear under `rows` in the report. Each scenario starts from generation 1. The published 0.4.0 wheel reproduces the three decisions originally demonstrated with 0.4.0rc2.

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

A cache observation uses `kind: "cache"`, the cache-hit receipt, and `origin_receipt` and `origin_observed_at` for the independently verified original read of the same SKU and revision. To admit cache evidence, add `"cache_max_age_seconds": 30` to `conditional_stock`. For RC3, export the host's absolute expiry as `cache_valid_until` on each hit, in UTC Unix microseconds. The hit is usable strictly before the earliest of the host expiry, the age limit on cache evidence, and the general freshness deadline. If the exporter omits the host expiry, the Gate returns `insufficient` when that hit is needed. Distinct hits may refer to the same original read if their provenance agrees. The observer attests receipt provenance before submitting the export. Give the `evidence` role to that trusted process and keep it separate from agent execution and automatic proposals.

## Evaluate and promote

Use the [governed release commands](operations.md#promote-roll-back-and-inspect) to register and propose the candidate. Supply both run documents and the observation timestamp from the trusted exporter. The observation timestamp must be at or after the end of each evidence window. The Gate checks the age of both runs against that timestamp. The saved comparison uses the latest window end in the input files.

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

### Read the verdict in CI

A candidate can return the same outcome label as its reference while skipping a required read. For the omitted-reserve demo the terminal diagnostic includes the actionable difference:

```text
candidate stock [fail] reserve_omitted | primary PRI-A4=2, threshold 5, reserve RES-K4, coverage complete
```

The JSON report preserves the supporting observations. Identical repeats of the same receipt in a paginated export are safe. The Gate discards observations whose receipt ID carries conflicting facts. An independent, consistent receipt can still establish the required read. If confirmed reserve quantities disagree, the Gate returns `insufficient`. Check the inventory journal, correct the export and evaluate it under a new evidence ID.

### Upgrade an existing RC2 conditional scope

1. Pause release decisions for the scope, drain pending calls and replace every RC2 writer sharing its ledger, including workers that dispatch bindings. Retain a consistent backup using the [governed recovery procedure](operations.md#back-up-restore-and-reconcile). Resume traffic after the writers run RC3. Legacy scopes continue to use their original verifier.
2. Keep the protected manifest on `forge-check/2`. RC3 stamps new conditional evaluations with `forge-check/2.1`. Previous decisions remain inspectable. Replaying an old operation returns its historical result. A fresh promotion or rollout based on an RC2 conditional evaluation returns `evidence_revalidation_required`.
3. Inspect active rollouts. New roots on an affected rollout use stable. An already pinned canary cannot start additional binding calls under the old certificate. A shadow allocation backed by that certificate stops dispatching shadow reads. Drain or stop the old rollout, then register a fresh one if needed.
4. Obtain current evidence from the observer. Repeat `forge evaluate` with the existing protected rule, a fresh trial window and a **new evidence ID**. Use that ID, the current generation and a **new decision ID** to promote. Check the alias and inspect the recorded decision. If the Gate still reports `insufficient`, its reason identifies the observation the operator needs.

A canary window ends at or before its authorizing evidence expires. The binding checks the window again before sending a reserved call. Effects already sent still need reconciliation. An alias promoted before the upgrade retains its revision until an operator rolls it back or promotes another revision. Switching binaries alone cannot reverse a completed dispatch. Retain RC2 for recovery on a compatible, isolated scope, and keep RC3 in charge of conditional scopes that require the stronger semantics.

## Roll back a release

Use `forge rollback --db state/governed.sqlite3 --actor release-owner --decision R42 --generation 2` after an applied promotion from generation 1. The next generation points to the previous compatible revision. Tasks already started retain their revision pins. To restore RC1 itself, use the retained RC1 wheel and a compatible scope initialized without this rule. A scope set up with verifier `forge-check/2` requires a verifier that understands its evidence contract.
