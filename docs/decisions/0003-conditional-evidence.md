# ADR 0003 · Require evidence for conditional reads

**Accepted for the standalone preview · 20 November 2026 · `0.4.0rc2`**

In RC1 (`0.4.0rc1`, `1d03290`), trial `conditional-01` read **2 units** at `PRI-A4`. The case required a read of `RES-K4`, which held **14 units**. The generated program returned only the primary quantity and the executor reported `completed`. The destination recorded one lookup, and the independent trial evaluator caught the omission. For a team deciding whether it can fill an order, that second quantity can change the decision.

We reproduced the omission on the original RC1 wheel with deterministic JavaScript. The executor returned `{"primary": 2}` and marked the program `completed`. The destination logged only `PRI-A4`, while the complete reference logged both lookups. RC1 returned `insufficient` when no normalized outcome was available. After the external oracle supplied the label `reserve-omitted`, RC1 returned `fail`. Both evaluations made zero new provider calls. This decision lets the Gate derive the conditional verdict from observations the operator already has.

## Decision

Let the workflow owner attach the conditional stock verifier to a revision and freeze its rule at host bootstrap. For example, when confirmed primary stock falls below the owner’s threshold, require an observation of reserve stock for the reserve lookup ID mapped to the case, from the trusted source and within the evaluation window. Evaluate the requirement at the Gate for both direct and programmatic execution. Preserve the executor status and receipt references alongside the business verdict. The report binds the frozen rule digest to the proposal, revision pair and alias generation, so operators can trace the criteria used for their decision.

The existing SDK can validate the shape of a returned object. Making reserve stock mandatory in every output would also require it when primary stock is sufficient. A prompt reminder leaves the decision with the same model whose omission prompted this proposal. A conditional rule gives the operator control over the extra read and a way to check the outcome independently. Forge already uses three verdicts for saved observations, which gives this proposal a familiar interface. [Saved-result verdicts](../check.md) · [Executor diagnostics](../code-mode.md)

| Evidence for the case | Gate treatment |
|---|---|
| Confirmed primary stock meets or exceeds the threshold | The rule is satisfied by the primary observation. |
| Primary stock is below threshold and trustworthy reserve evidence matches the case | The rule is satisfied. The Gate can pass once the remaining required checks pass. |
| The trigger or the required reserve observation is unknown | Return `insufficient` and name the missing evidence. |
| A source with complete coverage for the case confirms the required read was skipped | Return `fail`, even if the program finished and its output has the expected shape. |

## Validation and scope

The Linux lab installed the same candidate wheel in clean environments and exercised the public evaluation and promotion path. The independent destination journal confirmed a single read at or above the threshold, both reads when reserve was needed, and the missing lookup in the valid JSON control. Both execution routes passed the held-out matrix. The cases covered a partial or unavailable source, missing or unconfirmed stock, mismatched subjects and attribution, observations outside the window, and fresh and expired cache evidence. Evaluating and replaying the saved evidence produced zero provider dispatches.

Promotion preserved the alias on `fail` and `insufficient`. A fresh `pass` promoted only with the right evidence, generation and approver. The compatibility checks exercised the previous case format and a ledger created without this rule. The demo shows how the Gate's report distinguishes a confirmed omission from incomplete coverage. [Run the demo and read its output](../conditional-evidence.md#run-the-one-minute-demo)

The rule covers one primary–reserve relationship per case and one threshold per protected case set. The observer must attest trial coverage and cache provenance from records it controls. Direct calls remain the default. Code Mode remains an explicit Linux option.

Keep the previous compatible revision available for `forge rollback`. It changes future starts, while existing tasks retain their original revision pins. For a deployment rollback to the RC1 wheel, use a scope initialized with the legacy verifier and case format. [Rollback commands](../conditional-evidence.md#roll-back-a-release)

**Implementation evidence.** The `0.4.0rc2` wheel was built from `9bf9b396d5d2d44f79bf4aa3c94767560338888d`. Its SHA-256 is `da2e99069084178ca8b85ebcf9504e0ab7be8755bc92b8f355d0eddd043713ec`. The test driver is `tests/acceptance/conditional_evidence.py`.

## RC3 amendment · Revalidate evidence when its meaning changes

Two reserve receipts can disagree about the quantity available to fill an order. A cache can also expire at the host before the review window closes. RC3 treats both as operational decisions. It checks receipt identity across the cases of a run, requires consistent reserve quantities and honors the earliest actual expiry. The terminal report names the required stock location and the reason a check blocked.

A certificate created by RC2 records a real decision made with the earlier rules. RC3 keeps it available for audit and requires a fresh evaluation before it grants a new promotion or canary. This preserves the operator's history and avoids applying stronger guarantees retroactively. A prepared rollout can fall back to stable during the upgrade. Supporting this boundary costs one brief writer cutover for existing conditional scopes. [Operator procedure](../conditional-evidence.md#upgrade-an-existing-rc2-conditional-scope)

RC2's release tip `f695fd230a069d8023972c4f6524d96ea499d646` had the same standalone package tree as its build commit. Under RC3, the release manifest identifies a single build commit and package tree for the tested wheel and the bundle binds the conditional matrix to that wheel. The release owner can take the exact reviewed bytes into staging. The tests use synthetic inventory destinations and a real prior wheel. The preview makes no claim about adoption, live inventory accuracy or model-generated program quality.
