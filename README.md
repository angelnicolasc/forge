# Forge Check

In this support example, an agent marks all 24 tickets complete. Eight needed a human. The queue export shows that two of those eight, S07 and S08, went to self-service. Give `forge check` the routing rule and the queue observations for both revisions. It names the two missed escalations and exits with code 1.

What if those two queue records are missing? Forge reports `insufficient`. All three synthetic exports below use the same baseline and the same 24 eligible tickets.

| Candidate queue observations | Completion-only check | Forge Check |
|---|---|---|
| All tickets reached the expected destination | Pass | `pass`, 24/24 comparable |
| S07 and S08 ended in self-service | Pass | `fail` on S07 and S08, 24/24 comparable |
| The queue observations for S07 and S08 are missing | Pass | `insufficient` on S07 and S08, 22/24 comparable |

## Try the support check

With Git and Python 3.11 or newer, including venv and pip, get the fixtures and choose the block for your shell. The example reads saved observations and makes zero model calls.

```sh
git clone --depth 1 https://github.com/angelnicolasc/forge.git
cd forge
```

**Linux (tested on Ubuntu x86_64 with Bash 5.2 and Python 3.12)**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install "forge-os-check==0.4.0"
.venv/bin/forge --version
.venv/bin/python benchmarks/gate.py --rounds 1 --output support-check.json
```

**Windows (tested with PowerShell 7.6 and Python 3.11)**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install "forge-os-check==0.4.0"
.\.venv\Scripts\forge.exe --version
.\.venv\Scripts\python.exe benchmarks/gate.py --rounds 1 --output support-check.json
```

The version should read `forge 0.4.0`. The script runs the installed `forge check` three times and exits successfully when all three decisions match the table. Open `support-check.json` and look under `scenarios` for each exit code, the observed ticket count and the affected IDs. Choose a new output filename when repeating the demo. [Try a single check or add it to CI](docs/check.md)

## Why use this instead of a script?

A script that checks the destination is enough to catch a missed escalation. The extra work starts when a ticket disappears from an export. Forge keeps it in the denominator, records which revision is missing the observation, and gives missing evidence its own CI exit code. You can read the case IDs and reasons from the JSON report.

## Where does this fit if we already use promptfoo?

Keep the evaluator that exercises your agent. Promptfoo supports custom Python assertions. DeepEval, Inspect and LangSmith also let you supply evaluation logic. Use Forge when a release depends on evidence from a system your team controls. Your team exports the observations and decides which outcomes count as success. [Evaluator comparison](docs/check.md#working-with-other-evaluators)

For a working example with a live local destination, the inventory demo records each lookup and shows what happens to promotion when a reserve lookup is skipped or the observer goes offline. It uses the experimental Code Mode extra on Linux x86_64. The single-host runtime also supports direct bindings. [Inventory demo](docs/conditional-evidence.md) · [Binding guide](docs/governed.md)

Across three recovery drills on 0.4.0, the provider recorded zero duplicate POSTs after 12 process kills. [Recovery measurements](benchmarks/README.md#what-survives-a-process-crash)

An experimental change sped up a SQLite query. At 60 offered tasks per second, all three trials hit the queue cutoff. Canonical accounting remains the default. [Design decision](docs/decisions/0005-rollout-capacity.md)

The historical `forge-os` harness also installs a `forge` executable. Use a separate environment if you work on both projects. [Historical harness](packages/README.md) · [PyPI](https://pypi.org/project/forge-os-check/0.4.0/) · [Security](SECURITY.md) · [License](LICENSE)
