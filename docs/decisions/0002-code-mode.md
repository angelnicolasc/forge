# ADR 0002 · Ship Code Mode as a selectable read path

**Accepted for the standalone preview · 20 November 2026 · `0.4.0rc1`**

An agent looking up one SKU can call the binding directly. An agent reconciling several warehouses might save model turns by writing one program that reads, branches and combines results. Both users need the same guarantees about which tools they can call, what they can spend and what data they can see.

## Decision

Ship a first-party Code Mode API in the Linux SDK. The operator installs the optional executor and explicitly enables the read bindings it can use. A generated program runs in a bounded JavaScript environment. Every tool call returns to the host for authorization, budget and a receipt. The base wheel keeps the saved-result CI check free of executor dependencies. Direct calls remain the default. [Code Mode SDK](../code-mode.md) · [Base-wheel CI check](../check.md)

## Why give operators the choice?

We compared three ways to solve the same 18 held-out, synthetic tasks on RC1 (`1d03290`). There were six single reads, six batches and six conditional reads. Both model-driven routes used the same Qwen2.5-Coder-0.5B Q4_K_M weights on CPU. The direct route could batch independent reads in one model turn. The fixed recipe encoded the workflow in advance.

| Route | Correct tasks | Model turns | Provider dispatches | End-to-end p95 |
|---|---:|---:|---:|---:|
| Direct calls with batching | 18/18 | 22 | 31 | 1.59 s |
| Fixed recipe | 18/18 | 0 | 31 | 0.07 s |
| Model-generated Code Mode | 15/18 | 18 | 28 | 3.19 s |

Generated code handled all 12 single and batch tasks. In three conditional tasks it returned after the primary lookup and skipped the required reserve lookup. Those three missing reads account for the entire difference in provider dispatches. Counting fewer calls as savings would have rewarded incomplete work.

The fixed recipe completed all 18 tasks with zero model turns. Direct calls remain the default, with Code Mode available for applications that supply a program. We would change the default for a particular workflow after a paired, held-out trial against its best direct or fixed implementation shows at least 20% lower cost per correct task, a positive lower bound on savings at 95% confidence, success within two percentage points, zero critical false successes and p95 inside the agreed latency target. The RC1 trial covered one small model and six cases per family.

[How a skipped read led to a conditional evidence rule](0003-conditional-evidence.md)
