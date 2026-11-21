# 0005 · Bound the cost of admitting a canary task

**Status** Proposed. The runtime currently recomputes capacity from the ledger.

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

[Reference calculation](https://github.com/angelnicolasc/forge/blob/main/standalone/forge-check/src/forge_check/governed/rollout.py) · [Root closure](https://github.com/angelnicolasc/forge/blob/main/standalone/forge-check/src/forge_check/governed/control.py) · [Charge reconciliation](https://github.com/angelnicolasc/forge/blob/main/standalone/forge-check/src/forge_check/governed/ledger.py)
