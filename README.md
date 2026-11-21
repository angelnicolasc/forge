# Forge Check

An agent can complete every task and still miss the work that matters. Forge Check compares recorded agent runs against a business rule and tells your CI which cases passed, failed, or need more evidence.

In the support example, the agent marked all 24 tickets complete. Eight needed a human. The queue recorded six escalations. Forge names the two tickets the team would otherwise miss.

[Install from PyPI](https://pypi.org/project/forge-os-check/0.4.0/) · [Add a CI check](docs/check.md#add-one-step-to-your-ci) · [Govern tool calls](docs/governed.md)

## Catch two missed escalations

Start in the root of this checkout with Python 3.11 or newer and a POSIX shell. Use a dedicated environment because the older `forge-os-cli` distribution also installs a command named `forge`. The base wheel has no runtime dependencies. This example reads saved, synthetic queue observations and makes zero model calls.

```sh
python3 -m venv .venv-forge-check
. .venv-forge-check/bin/activate
python -m pip install 'forge-os-check==0.4.0'

forge check \
  --cases examples/check-support/cases.json \
  --baseline examples/check-support/baseline.json \
  --candidate examples/check-support/candidate-fail.json \
  --output support-check.json
```

The command deliberately exits with `1`. Look for these two cases in the output.

```text
S07 [fail] expected human, candidate self_service (completed), baseline human (completed)
S08 [fail] expected human, candidate self_service (completed), baseline human (completed)
```

Run the corrected candidate in the same shell.

```sh
forge check \
  --cases examples/check-support/cases.json \
  --baseline examples/check-support/baseline.json \
  --candidate examples/check-support/candidate-pass.json \
  --output support-pass.json
```

The second command exits with `0`. To use this in your own repo, choose a process outcome, freeze the eligible case IDs and export observations from the system that owns the outcome. Each run produces a JSON report that you can keep with the PR. [Input format and CI example](docs/check.md)

## How many provider calls does Forge avoid?

We sent the same inventory reads through the published `0.4.0` binding with caching off and with a five-minute TTL. The destination counted physical HTTP requests. Both settings returned identical stock values in every round.

<!-- bindings-benchmark:start -->

| Inventory workload | Cache off | Cache on | Fewer provider calls |
|---|---:|---:|---:|
| 120 reads, 8 recurring SKUs | 120 | 8 | 93.3% |
| 120 reads, 120 different SKUs | 120 | 120 | 0% |

<!-- bindings-benchmark:end -->

The table shows calls per round. All three rounds returned the same counts. Stock stayed constant during the benchmark. For rules that require the latest stock, use a fresh lookup. [Commands, results and the single-ledger load limit](benchmarks/README.md)

## Govern a release

When a team also needs to control the calls an agent can make, the same distribution includes a single-host runtime. A trusted host routes explicitly configured operations through bindings and stores receipts in SQLite.

| Concept | What an operator can answer |
|---|---|
| Revision | What did we authorize? An immutable manifest identifies the tool contracts, budgets and checks. A task starts on a pinned revision. |
| Receipt | What happened at the binding? The receipt identifies the call, its revision and its recorded disposition. The destination's own records can confirm a business effect. |
| Gate | Can we promote this change? The Gate evaluates saved evidence against the frozen rule. An authorized operator can promote with a fresh pass or retain the current revision when evidence is missing. |

A stock check illustrates the distinction. If primary inventory falls below the threshold, the rule requires a reserve lookup. A complete journal can establish that the lookup was skipped. If the observer is offline, Forge reports `insufficient`. Both outcomes hold the current revision. [Conditional inventory example](docs/conditional-evidence.md)

- **Direct bindings** enforce permissions and reserve budget before dispatch. Start here to put one agent operation under control. [Governed binding guide](docs/governed.md)
- **Code Mode** is an opt-in, experimental Linux x86_64 extra. A program can compose approved reads through the same bindings. [SDK quickstart](docs/code-mode.md)
- **Rollouts** let operators route a bounded canary, inspect observed revisions and roll back future starts. Previously dispatched calls still need to be reconciled with the destination. [Rollout and recovery](docs/operations.md)

## Project

Forge Check `0.4.0` is available on PyPI. The release workflow published the approved wheel and verified its digest. [Release artifact](https://pypi.org/project/forge-os-check/0.4.0/) · [Publication run](https://github.com/angelnicolasc/forge/actions/runs/44823397253) · [Architecture decisions](docs/decisions/index.md)

The repository also contains the earlier multi-package Forge harness, including framework adapters, memory and tracing. Its installation and feature map are documented separately. [Harness quickstart](docs/quickstart.md) · [Harness architecture](docs/architecture.md) · [Harness feature map](docs/feature-map.md) · [Historical README and roadmap](https://github.com/angelnicolasc/forge/blob/6fc58c30ca50d3d9bda1e14e0764e77bb123df56/README.md)

[Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [Apache 2.0 license](LICENSE)
