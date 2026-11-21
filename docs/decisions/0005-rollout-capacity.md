# 0005 · Bound the cost of admitting a canary task

**Status** The closed-charge projection is eligible for review. Canonical accounting is the default. Sustained capacity at 60 tasks/s is on hold. [F3/F4 measurements and receipt digests](https://github.com/angelnicolasc/forge/blob/e5989b9c2bf229ab6fc1f5fd4bb2652a3b253498/benchmarks/results/adr-0005.md)

## Decision to make

Two rollouts can have the same remaining budget but very different admission latency. In a single-host lab, moving 10,000 historical roots from a closed rollout into the rollout being evaluated raised a warm capacity query from 0.012 ms to 22.57 ms. At 60 offered tasks/s, end-to-end p95 went from 0.92 s to 7.40 s. The first two historical roots were real API transactions, the other 9,998 reproduced their table shape, and 120 new tasks in each arm went through the binding. These figures identify the cost of scanning an active rollout. [Benchmark results](https://github.com/angelnicolasc/forge/tree/main/benchmarks)

## Two candidates, one accounting rule

Only canary roots contribute to the canary's budget. The charge for a root includes its child tasks. An open root consumes a concurrency slot and exposes the larger of its allocated cap and its recognized charges. A closed root exposes its recognized charges. For example, an open root allocated 100 µUSD with 70 µUSD charged contributes 100 µUSD of exposure. On closure its exposure falls to 70 µUSD. If the recognized charge reaches 120 µUSD while open, its exposure is already 120 µUSD.

1. **Closed spend plus indexed open roots.** Prototype this first. Add the root's recognized charge to a total when it first closes, atomically with the close. Maintain an indexed set of open canary roots and calculate the live part from those roots and their attempts. This removes closed history from the admission path and concentrates new accounting at allocation and closure. Its cost still depends on open roots and their fan-out. It relies on the existing rule that a root can close only when its effects and charges have been settled. Prove that no accepted event can change a closed root's charge before relying on this invariant.
2. **Totals updated on every recognized change.** If the open set remains expensive, compare a projection of active roots, recognized spend and exposure per rollout. Apply the root's before/after difference in the same SQLite write transaction as allocation, accepted charge or closure. A repeated observation, a duplicate charge reference or an idempotent close contributes zero additional spend. A timeout keeps exposure until the original attempt is resolved.

Use integer monetary units and preserve the reference calculation's exact range. In either candidate the binding continues to reserve before dispatch. The ledger remains the source for rebuilding and auditing the projection.

## Evidence required for acceptance

| Question | Required evidence |
|---|---|
| Can the projection authorize too much? | Compare every intermediate total and admission decision to a fresh ledger recomputation. Exercise concurrent starts at the last slot and last budget unit, late and above-cap charges, child tasks, repeated or conflicting charges, cache hits, uncertain outcomes and repeated close. Require exact agreement. |
| Can a crash split the accounting? | Kill the writer on either side of commit, restore a snapshot with open roots, and repeat reconciliation. Verify the original attempt IDs, destination journal, outstanding funds, and balances before opening the writer. |
| Can one maintainer upgrade and reverse it? | Backfill on a populated ledger with an active canary. Exercise every supported migration and restore path and drain or fence older writers. Switch back to the reference calculation on the same up-to-date ledger and verify decisions. |
| Does it remove the measured bottleneck? | Compare the current implementation and both applicable prototypes on matching ledgers. Hold global cardinality fixed and vary active-rollout history, concurrent open roots and attempts per root. Run 12/40 tasks/s controls and at least three ten-minute trials at 60/s. Record arrivals, completions, backlog trend, drain time, admission/status/stop p95 and p99, CPU, and destination charges. Agree on the latency and drain limits before running. |

First compare the proposed calculation in the lab while the existing calculator makes every authorization decision. A mismatch blocks promotion. Before making a capacity claim, require bounded backlog through the sustained window, the agreed latency limits, exact monetary agreement, and zero duplicate effects. If a different path then dominates, measure per-root balance with many attempts and the status projection under the writer during rollout stop.

## Implementation and rollback

The schema v3 writer defaults to the canonical calculator. Every canary allocation
adds one indexed open root. Closure moves its contribution to the closed charge
total of the original rollout in the same SQLite transaction. Open roots still
include the charges of their child tasks. Settlement keeps its existing duplicate
and conflict handling. The storage guard preserves the monetary contribution of a
finished root.

Before migrating a ledger, drain older processes that may be between durable
dispatch intent and the provider call. Keep the original attempt identifiers and
reconcile outstanding observations with the provider. The explicit migration
takes a backup and serializes backfill and schema fencing with other SQLite
writers. The v3 fence rejects later writes from a running older process. It
cannot revoke a provider call that an earlier transaction had already authorized.

On an isolated lab ledger, select `compare` to evaluate both calculators in the
authorization snapshot. Select `projection` only after the parity checks pass.
From a compatible v3 process, `rollout.set_capacity_mode(conn, "reference")` inside
`Store.write()` switches the same ledger back to canonical accounting. Preserve
the current ledger and its receipts. Restore an earlier snapshot only through
fenced recovery and reconcile the independent provider journal before resuming.

## Laboratory evidence

The 0.4.1.dev2 wheel was measured on one Linux Vast instance with a CPU
quota of 15.36 cores, Python 3.12.16, SQLite 3.40.1 and local SQLite WAL. The
0.4.1.dev1 control reproduced the expected charges on both history placements.
The comparison exercised eight exactness cases, independent writer races and
seeded transitions. Migration from 0.4.0 and 0.4.1.dev1, the v1 upgrade path,
old-writer fencing, rollback and the independent provider recovery drill passed.

| Operation at 10,000 closed roots | Reference | Closed charge and indexed open roots |
| --- | ---: | ---: |
| Warm capacity median across three rebuilds, three open roots | 51.66–52.19 ms | 0.067–0.102 ms |
| Warm capacity p95 across three rebuilds | 52.59–52.86 ms | 0.109–0.148 ms |
| First admission p95, 20 independent copies per arm | 67.08 ms | 5.78 ms |
| State p95, 20 independent copies per arm | 116.95 ms | 46.47 ms |
| First cut p95 with a contending writer, 20 independent copies per arm | 139.96 ms | 64.56 ms |
| Root close p95, 20 independent copies per arm | 3.97 ms | 3.60 ms |
| Charge recognition p95, 20 independent copies per arm | 3.68 ms | 4.46 ms |

At 32 open roots and 64 attempts per root, projected capacity p95 was at most
1.04 ms across the same three rebuilds. Migration with backup on three 10,000-root
fixtures took 286–371 ms and grew the ledger file by 49,152 bytes per copy.
The candidate had a 56.33 ms state p99, a 6.08 ms admission p99 and a 65.67 ms
maximum first cut. Under the cut writer, the rollout view occupied a median of
41.99 ms out of 42.41 ms in the transaction body.

## Sustained load decision

The 12 and 40 tasks/s controls passed the admission and close p95 guardrails.
The three primary trials used 10,000 closed roots in the active rollout, 12
workers, a nominal 5 Hz status poll, a 50% target cohort and four canary slots.
Each candidate trial reached the prescribed queue cutoff at 90 seconds. Out
of 36,000 planned arrivals per trial, it emitted and reconciled 5,208, 5,080
and 5,208 respectively. The remaining 22–24 seconds after cutoff drained the
emitted work. All three reference trials reached the cutoff at 60 seconds
and still had pending work at their drain deadline. These censored trials
provide saturation evidence and leave the full-run latency and relative
throughput estimates open.

At 12 tasks/s, a fixed-mixture replay produced exactly 60 canary and 180 stable
roots, with 17 canary and 180 stable physical dispatches and identical charges
in each arm. This supports the comparison at that rate. The 60 tasks/s trial
with minimal instrumentation also exceeded the latency objective. Its 3,600
correct tasks had p95 end-to-end latency of 20.81 seconds.

In the slowest 5% of the same roots in each candidate trial, mean pool wait
was 22.40–23.73 seconds and cumulative mean writer acquisition was 256–296 ms.
The mean capacity work nested in the writer was 0.63–0.70 ms. The next capacity
investigation should isolate SQLite writer contention and the cost of the rollout
state view as the ledger grows. Revisit transition totals if open-root accounting
becomes a dominant cost.

[Reference calculation](https://github.com/angelnicolasc/forge/blob/main/standalone/forge-check/src/forge_check/governed/rollout.py) · [Root closure](https://github.com/angelnicolasc/forge/blob/main/standalone/forge-check/src/forge_check/governed/control.py) · [Charge reconciliation](https://github.com/angelnicolasc/forge/blob/main/standalone/forge-check/src/forge_check/governed/ledger.py)
