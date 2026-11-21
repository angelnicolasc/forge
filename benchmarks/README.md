# Forge Check benchmarks

## How many calls reach the provider?

The binding benchmark runs identical inventory reads with caching disabled and with a five-minute TTL. Each task checks the inventory value and then replays its operation. The destination keeps an independent HTTP journal, so a retry that crosses the binding appears in the measurement.

On the PyPI `0.4.0` wheel, all three rounds had the same physical call counts. Each arm returned the same sequence of stock values as its control.

| Sequential inventory workload | Provider calls, TTL 0 s → 300 s | Synthetic charges per arm, µUSD | Additional POSTs from 120 exact replays per arm |
|---|---:|---:|---:|
| 120 reads cycling over 8 SKUs | 120 → 8 | 480,000 → 32,000 | 0 |
| 120 reads of 120 distinct SKUs | 120 → 120 | 480,000 → 480,000 | 0 |

The fixture charges 4,000 µUSD per physical POST. The ledger was reconciled against the destination in every arm. The percentages on the project README describe provider requests. Real bills depend on provider pricing and the workload's reuse and freshness requirements. [Stable binding receipt](results/bindings-stable.json) · [Development wheel receipt](results/bindings-candidate.json)

Run from the checkout on a Linux benchmark host with Python 3.11 or later. Install the base wheel in a dedicated environment. To measure the version in this checkout, use the following commands.

```sh
python3 -m venv /tmp/forge-bench-env
/tmp/forge-bench-env/bin/python -m pip install ./standalone/forge-check
/tmp/forge-bench-env/bin/python -I benchmarks/bindings.py \
  --tasks 120 --keys 8 --rounds 3 --output /tmp/forge-bindings.json
```

To repeat the published release measurement, install `forge-os-check==0.4.0` in that environment. Run `python -I benchmarks/readme_table.py --check` from the checkout to compare the README table with its stable receipt. The wheel and receipt digests are recorded in the [run manifest](results/environment.json).

The repeated-key workload cycles through eight SKUs. Its control uses a different SKU for each of the 120 tasks. The stock stays constant in both workloads. Compare physical calls and synthetic charges within the same workload and round. Both policies run through Forge, and the second invocation of a task exercises idempotent replay separately from cache reuse across tasks. [Benchmark source](bindings.py)

## Can a completed run still fail its business rule?

The Gate benchmark runs the installed `forge check` CLI against three 24-ticket exports. All tickets are marked complete. An intact export, two missed human escalations and two missing observations exercise the `pass`, `fail` and `insufficient` outcomes. A control that reads only the completion status would approve all three. The benchmark includes CLI startup in its latency measurement. Run it from the same checkout and environment as the binding benchmark.

```sh
/tmp/forge-bench-env/bin/python -I benchmarks/gate.py \
  --rounds 20 --output /tmp/forge-gate.json
```

| Support export, 24 completed tickets each | Completion-only control | Forge verdict on `0.4.0` | CLI median / p95 |
|---|---|---|---:|
| All routes matched the expected outcome | pass | pass | 137.6 / 146.2 ms |
| Two required human escalations were skipped | pass | fail on S07 and S08 | 137.3 / 142.8 ms |
| The queue supplied no observation for S07 and S08 | pass | insufficient on S07 and S08 | 139.8 / 147.7 ms |

Each timing includes Python and CLI startup and writing the JSON report. Each wheel ran 20 fresh processes per export, cycling the order. These are repetitions of three fixed fixtures. [Stable Gate receipt](results/gate-stable.json) · [Development wheel receipt](results/gate-candidate.json)

[Gate benchmark source](gate.py) · [Support fixture](../examples/check-support)

## What survives a process crash?

In a single-host recovery drill, **36 forced process kills produced zero duplicate provider POSTs**. The destination received four POSTs with four distinct operation keys per round and recorded 28 micro-USD of synthetic charges. Each round interrupted four operations at different points between reservation and ledger settlement. Three rounds ran on each wheel. [Archived measurements and receipt digests](results/hardening-2026-11-21.json)

| Wheel | Process kills | Unique POSTs / physical POSTs across three rounds | First successful new task after the first kill |
|---|---:|---:|---:|
| Published `0.4.0` | 12 | 12 / 12 | 343–411 ms |
| `0.4.1.dev0` | 12 | 12 / 12 | 361–419 ms |
| `0.4.1.dev1` | 12 | 12 / 12 | 410–433 ms |

Uniqueness is counted within each restored ledger. Each round used a fresh ledger and reused the fixture's operation names. The operator script stopped the original writers, restored a snapshot and reconciled pending operations against the destination. A stale snapshot omitted two effects in every round and remained fenced. An inconclusive provider reply kept the reservation in place. Timings cover immediate scripted intervention and a loopback provider on one Linux host. [Restore protocol](../docs/operations.md)

## Does cached inventory stay fresh after an upgrade?

The origin held a reply for **2.153 s** while stock fell from 11 to 3. The cache TTL was **2 s**. On the next task, the released `0.4.0` wheel returned 11 from its cache. With a copy of the same warm ledger, `0.4.1.dev1` dispatched a new read and returned **3**. The candidate measures reuse from the original dispatch. An in-flight read can still return the snapshot it captured, and inventory can change within the allowed TTL. [Upgrade fixture](../tests/acceptance/cache_freshness_upgrade.py) · [Measured values](results/hardening-2026-11-21.json)

## How much history does an admission read?

`capacity.py` times the installed calculator on two copies of the same ledger. Three canary roots stay open with charges of 0, 15,000 and 101,000 µUSD, each with an allocated cap of 100,000 µUSD. The fixture keeps 10,000 additional, closed canary roots in both copies. Only their rollout assignment changes. Every sample verifies the number of active roots, recognized charges and exposure against independent integer totals. The historical rows have no provider calls or receipts.

```sh
/tmp/forge-bench-env/bin/python -I benchmarks/capacity.py \
  --history 10000 --repetitions 31 --output /tmp/forge-capacity.json
```

| Placement of the 10,000 closed roots on `0.4.0` | Capacity query median | Capacity query p95 |
|---|---:|---:|
| Previous rollout | 0.022 ms | 0.038 ms |
| Active rollout | 40.536 ms | 46.415 ms |

Times cover a warm SQLite query on an already-open connection. Each arm has 31 samples ordered in alternating pairs after warm-up. The separate load drill below includes arbitration for the writer, task execution, HTTP, and queuing. Its history mixes stable and canary cohorts. [Calculator benchmark](capacity.py) · [Stable receipt](results/capacity-stable.json) · [Development receipt](results/capacity-candidate.json)

To compare the projection on a leased Linux lab host, verify the wheel hashes and install the reference `0.4.1.dev1` and the candidate into separate environments. Set `FORGE_VAST_LAB_ID` to the verified lease ID and use a new receipt path.

```sh
"$CANDIDATE_PYTHON" -I benchmarks/capacity_paired.py \
  --reference-python "$FORGE_PYTHON" --output "$RUN_DIR/capacity-paired.json"
```

The reference wheel seeds and verifies all source ledgers before migration. Within each reconstruction the query arms use two copies of the same migrated ledger, with the canonical calculator and projection selected separately. Each pair varies its order and verifies exact totals outside the timing window. Actual admissions, charges, closures and operator calls use separate copies of the matching fixtures in the two installed wheels. The first cut contends with an independent SQLite writer that holds the lock for 15 ms. The receipt includes raw samples, query plans, writer wait and transaction durations, and the first admission after the cut. [Paired benchmark](capacity_paired.py)

## Where does a single ledger saturate?

An open-loop run offered **60 tasks/s** to 12 workers and scheduled all 1,200 arrivals irrespective of backlog. On `0.4.1.dev1`, all 1,200 completed, including 364 cache hits and 836 provider dispatches. The run drained at **37.80 tasks/s**, with **11.08 s p95** and **11.77 s** between the last scheduled arrival and the last completion. Latency runs from each scheduled arrival through root completion. Sustained 60 tasks/s on this configuration remains an open target. [Extended burst](results/hardening-2026-11-21.json)

The cardinality drill isolated the cost of calculating available canary capacity. Two fixtures each contained 10,000 historical roots, then received 120 new tasks at 60/s. Two roots were created through the API and 9,998 entries per table were copied to set the starting cardinality. All 120 measured tasks per fixture went through the binding.

| Location of the 10,000 roots on `0.4.1.dev0` | Warm capacity query, mean | Completed tasks/s, including drain | Task p95 |
|---|---:|---:|---:|
| Previous rollout | 0.012 ms | 44.84 | 0.92 s |
| Active rollout | 22.570 ms | 13.05 | 7.40 s |

The capacity check reads the active rollout's canary allocations and sums their attempts. This measurement motivates tracking capacity incrementally and comparing the result against a full ledger calculation before changing admission. The drill used one run per placement. The four populated tables model ledger shape, while the effects and charges from the 120 new tasks were checked against the destination. [Cardinality measurements](results/hardening-2026-11-21.json)

The archived drills ran on 2026-11-21 UTC using a Linux x86-64 host, Python 3.12.16, SQLite 3.40.1 and a CPU quota of 17.28 cores. They used simulated traffic and a loopback HTTP provider. The public binding benchmark above measures provider-call reuse in sequential tasks. The archive records the separate load, recovery and upgrade experiments. Each excerpt identifies its original receipt by name and SHA-256. [Machine-readable archive](results/hardening-2026-11-21.json)
