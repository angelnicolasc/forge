# ADR 0004 · Show which canary roots reached dispatch

**Proposed for the next patch after 0.4.0.**

A canary operator needs to answer two questions before increasing exposure. How many tasks got the candidate, and how many actually tried to call a provider? Forge 0.4.0 reports assignments by cohort. A task can then finish without a call, read from cache, or make several calls through a child. An assignment alone cannot tell the operator which happened.

In a single-host load rehearsal with the published 0.4.0 wheel, 106 of 228 simulated support tasks were assigned to a canary that allowed a 30-second cache. The canary made 33 provider calls and served 73 cache hits. The independent HTTP destination recorded the calls. Reconstructing that distinction required pairing the binding receipts with the destination journal.

## Decision

Add `traffic_by_cohort` to the existing `rollout-status` result. Count distinct allocated roots, roots with at least one dispatch intent, total dispatch intents, and roots still awaiting confirmation. A child contributes to its parent's cohort. Replays and cache hits add no dispatch intent. The operator can see what still needs reconciliation after stopping new assignments. [Field definitions and operating procedure](../operations.md#read-the-traffic-counters)

Read the existing allocation and attempt tables within one SQLite snapshot. This adds query work when operators inspect status and avoids a new event pipeline, service or schema migration. Persisted counters would add writes on dispatch and create another set of values to reconcile after recovery. Revisit that choice if status queries against a representative ledger exceed the operator's latency budget.

## What an operator can conclude

An intent establishes that the host crossed the decision to send. A lost response can leave the outcome or the charge uncertain even if the destination completed the operation. Compare the attempt and operation key with the destination before clearing uncertainty. For shadow traffic, the dispatch belongs to the stable revision. This report describes only operations that traversed the binding.

The existing fields and grant to inspect the rollout keep their semantics. The regression fixture exercises the CLI against shadow, cache hits, replay, children, an unconfirmed dispatch, reconciliation and revocation with an existing root. Run it in the isolated acceptance environment against 0.4.0 and the proposed patch. The fixture is `tests/acceptance/rollout_traffic.py`.
