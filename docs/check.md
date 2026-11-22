# Check saved agent results in CI

Use `forge check` to compare two recorded revisions against a rule your team owns. The report names the cases that need attention and shows how many eligible cases had observations in each revision.

## Try the support example

From the root of a Forge checkout, create a dedicated environment with Python 3.11 or newer. The base `forge-os-check` wheel has no runtime dependencies. The historical harness also installs an executable named `forge`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install "forge-os-check==0.4.0"
.venv/bin/forge --version
.venv/bin/forge check --cases examples/check-support/cases.json \
  --baseline examples/check-support/baseline.json \
  --candidate examples/check-support/candidate-fail.json --output support-check.json
```

In Windows PowerShell, run the following.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install "forge-os-check==0.4.0"
.\.venv\Scripts\forge.exe --version
.\.venv\Scripts\forge.exe check --cases examples/check-support/cases.json --baseline examples/check-support/baseline.json --candidate examples/check-support/candidate-fail.json --output support-check.json
```

All 24 synthetic tickets are marked complete. Eight require a confirmed entry in the human queue. The saved candidate export records six. The command returns `1` and records `fail` for `S07` and `S08`, where the observed route is `self_service`. Replacing `candidate-fail.json` with `candidate-pass.json` returns `0` with all 24 cases comparable. The check uses outcome labels to make its decision. The fixture costs are illustrative metadata. [Support fixtures](https://github.com/angelnicolasc/forge/tree/main/examples/check-support)

On a first run, omit `--baseline` and use `candidate-pass.json`. The result is `insufficient` (exit `2`). A pass establishes that both revisions satisfied the rule over the eligible cases, so it needs a comparable baseline. Add `--save-reference support-reference.json` to save the candidate as a pending reference. Use a new path. Have the owner review that recording before using it as the baseline for a subsequent revision. A confirmed candidate violation still returns `fail` when the baseline is absent.

## Supply a case from your own agent

Freeze the rule, case IDs and expected outcomes with the business process owner. Export the baseline and candidate observations from the system that owns the outcome. For example, read the ticket history to determine which queue received an escalation. Set `status` from the agent run and `observed` from the independent read.

The case file lists the eligible IDs, rule and source.

```json
{
  "schema_version": 1,
  "rule_id": "support-routing-v1",
  "source": "support-queue",
  "cases": [{"id": "S07", "expected": "human"}]
}
```

Each run file uses the same rule and source and its own revision ID. Both revisions must record a completed run with the expected observation for every eligible case to pass.

```json
{
  "schema_version": 1,
  "rule_id": "support-routing-v1",
  "source": "support-queue",
  "revision": "r42",
  "cases": [{"id": "S07", "status": "completed", "observed": "self_service"}]
}
```

Use `null` for an unavailable observation. The case still counts as eligible when a run errors, the observer has no answer, or a run omits the case. A confirmed wrong destination or duplicate effect gets its own outcome label, such as `duplicate_human`.

Use publication-safe identifiers and labels of 1–128 characters. They start with an ASCII letter or digit and can also contain `_`, `.`, `:` and `-`. The output includes those identifiers and labels as supplied. Keep customer content, prompts and credentials out of the inputs.

The report records the verdict, rule, source, revision IDs, coverage and reasons for each case. Use the source and case IDs to find the underlying records. For rules that require a second lookup under certain conditions, see the [conditional inventory example](conditional-evidence.md).

## Add one step to your CI

Add these steps to a Linux GitHub Actions job after checkout and Python 3.11+ setup. Have your exporter write the three inputs under `evaluation/` before the check runs. The first step installs the published wheel in a dedicated environment. The second keeps the JSON report when the check flags a regression or missing evidence.

```yaml
- name: Check support routing
  shell: bash
  run: |
    python -m venv "$RUNNER_TEMP/forge-check"
    "$RUNNER_TEMP/forge-check/bin/python" -m pip install "forge-os-check==0.4.0"
    mkdir -p artifacts
    "$RUNNER_TEMP/forge-check/bin/forge" check \
      --cases evaluation/cases.json \
      --baseline evaluation/baseline.json \
      --candidate evaluation/candidate.json \
      --output artifacts/forge-check.json
- name: Keep the Forge Check report
  if: ${{ always() }}
  uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
  with:
    name: forge-check-report
    path: artifacts/forge-check.json
    if-no-files-found: warn
```

| Exit code | Meaning | Follow-up |
|---|---|---|
| 0 | `pass` | The candidate satisfies the rule across eligible cases with comparable evidence. |
| 1 | `fail` | Investigate the case IDs and confirmed differences in the report. |
| 2 | `insufficient` | Supply the missing baseline or observations and compare again. |
| 3 | Invalid input or execution error | Read the diagnostic, correct the input or runtime error and rerun. |

Make the job a required check once the owner has approved the inputs and the treatment of missing evidence. Only exit code 0 passes the check. A malformed input produces a diagnostic in the JSON report. If Forge cannot write the output path, read the error on stderr.

## Working with other evaluators

A Python test can encode a single routing rule and manage its fixtures. Forge packages the two-revision comparison, coverage calculation, per-case reasons and exit codes so teams can share the contract. Each team still supplies its own evidence source. [Python testing](https://docs.python.org/3/library/unittest.html)

| If you use | What you can already do |
|---|---|
| promptfoo | Use a Python assertion that reads the output and test context and returns a result with a score or reason. [Python assertions](https://www.promptfoo.dev/docs/configuration/expected-outputs/python/) |
| DeepEval | Implement a custom metric in Python and run it in CI, including a metric that requires no model call. [Custom metrics](https://deepeval.com/docs/metrics-custom) |
| Inspect | Write a custom scorer and use `Score.unscored()` to record why a judgment could not be obtained. [Custom scorers](https://inspect.aisi.org.uk/custom-scorers.html) |
| LangSmith | Run a code evaluator against application outputs and dataset references and return metrics for an experiment. [Code evaluators](https://docs.langchain.com/langsmith/code-evaluator-sdk) |

For the support check, a confirmed wrong route blocks release with `fail`. Unavailable evidence blocks it with `insufficient`. A team can implement those decisions in its existing evaluator or export the queue records to Forge.
