# ADR 0001 · Put each tool’s contract in the revision

**Accepted for the standalone preview · 20 November 2026 · `0.4.0rc1`**

A stock lookup should take a SKU. In Forge 0.3.0, a team could register `inventory.stock` and grant the agent permission to use it, yet both the direct host and Code Mode rejected `{"sku":"A-18"}` before the inventory system saw a request. The support example had shaped how the runtime validated inputs, cached results and selected the fields returned to the caller.

## Decision

A version 2 revision declares each tool’s input, output and caller-visible fields. The binding applies that contract at the point of dispatch. It validates inputs before sending a request, then validates and projects the provider response before returning it or caching a read. Direct calls and JavaScript subcalls use the same host for permission checks, reservations and receipts. Existing version 1 support manifests retain their original identity.

Encoding a SKU in a field called `ticket_id` would have saved an SDK change while making receipts harder for an inventory operator to interpret. Separate inventory and invoice validators would have forced the team to repeat the same changes to both execution paths for each domain. Keeping the contract in the revision lets the operator review a change such as exposing `internal_cost` alongside a change of tool permissions, before either reaches a new task.

## What changed for an operator

In the RC1 acceptance run, the inventory destination received a SKU and returned 9 units for `A-18` on a direct call and 16 for `B-29` from JavaScript. A separate invoice fixture accepted `invoice_id` and returned 2,300 cents and two entries through both routes. The support regression still passed. The field projection kept private provider data out of caller results. Both tested timeout paths retained their pending reservation.

The contract uses a bounded schema dialect. A provider can charge for work even when its response fails validation, so the ledger keeps the verified charge. The inventory quickstart uses its own stock fixture and records all three calls at the destination. [Binding contract and inventory walkthrough](../code-mode.md)

**Implementation evidence.** The acceptance cases live in `tests/acceptance/preview_bindings.py`. The contract implementation lives in `standalone/forge-check/src/forge_check/governed/binding_contract.py`.
