# Check saved agent results in CI

Use `forge check` to compare two recorded revisions against a rule your team defines. Each case has a stable ID and an expected outcome. The result names the cases that need attention and shows how many eligible cases had observations in each revision.

## Try the support example

Use a fresh Python 3.11+ environment on your runner and install the candidate wheel from the revision you are reviewing. The standalone `forge-os-check` wheel provides the `forge` command and has no runtime dependencies. Give it its own environment because the legacy `forge-os-cli` wheel provides the same command.

```bash
python -m venv "$RUNNER_TEMP/forge-check"
source "$RUNNER_TEMP/forge-check/bin/activate"
python -m pip install --no-index --no-deps "$FORGE_CHECK_WHEEL"
```

Set `FORGE_CHECK_WHEEL` to the absolute path of the wheel supplied to your CI job. For a runner outside GitHub Actions, use an equivalent temporary directory in place of `RUNNER_TEMP`.

From the source checkout, use the [saved support fixtures](https://github.com/angelnicolasc/forge/tree/main/examples/check-support). The agent marked all 24 tickets complete. Eight required one confirmed entry in the human queue. The cheaper candidate has only six. For the other two, an independent queue read confirmed the absence of the required entry.

```bash
forge check \
  --cases examples/check-support/cases.json \
  --baseline examples/check-support/baseline.json \
  --candidate examples/check-support/candidate-fail.json \
  --output support-check.json
```

The command returns `1` and records `fail` for `S07` and `S08`. Each expected `human` and was observed as `self_service`. The denominator is 24, even though only six escalations occurred. Replace `candidate-fail.json` with `candidate-pass.json` to see `pass` with the same denominator. The costs in the fixtures are illustrative metadata. The check evaluates the recorded outcomes and makes zero model calls.

On a first run, omit `--baseline`. With `candidate-pass.json`, the result is `insufficient` and the process returns `2`. Add `--save-reference support-reference.json` to save the candidate as a pending reference. Use a new path because Forge preserves any file already there. Have the owner review the reference before supplying it as a baseline for a new candidate. An independently confirmed violation returns `fail` even when the baseline is missing.

## Supply a case from your own agent

Freeze the rule, the case IDs and expected outcomes with the person who owns the business process. Export the candidate and baseline observations from a source that independently confirms those outcomes. For example, use a ticket-system read to confirm escalation. An agent's claim that it escalated a case cannot establish the queue state.

The case file lists the eligible IDs and the rule and source that apply to them.

```json
{
  "schema_version": 1,
  "rule_id": "support-routing-v1",
  "source": "support-queue",
  "cases": [{"id": "S07", "expected": "human"}]
}
```

Each run file uses the same rule and source, with its own revision ID. For a comparison to pass, the baseline and candidate must both record a completed run with the expected observation for each eligible case. Put the agent's run status in `status` (`completed` or `error`) and the normalized evidence from the independent source in `observed`.

```json
{
  "schema_version": 1,
  "rule_id": "support-routing-v1",
  "source": "support-queue",
  "revision": "r42",
  "cases": [{"id": "S07", "status": "completed", "observed": "self_service"}]
}
```

Use `null` for an unavailable observation. The case still counts as eligible when the run errors, the observer has no answer, or a run omits the case. A confirmed wrong destination or duplicate effect gets its own outcome label, such as `duplicate_human`.

Use publication-safe identifiers and labels of 1–128 characters. They start with an ASCII letter or digit and can also contain `_`, `.`, `:` and `-`. The output includes those identifiers and labels as supplied. Keep customer content, prompts and credentials out of the inputs.

The JSON report records the verdict, the source and rule IDs, the revision IDs, coverage for both runs, and the decision and reasons for each eligible case. Review the candidate observations alongside the expected outcomes and use the source and case IDs to find the underlying records.

## Add one step to your CI

After installing the wheel and exporting the three input files, add a step like this to your existing job. Collect `artifacts/forge-check.json` in the job's artifact upload step even when this step fails.

```yaml
- name: Check support routing
  shell: bash
  run: |
    mkdir -p artifacts
    "${RUNNER_TEMP}/forge-check/bin/forge" check \
      --cases evaluation/cases.json \
      --baseline evaluation/baseline.json \
      --candidate evaluation/candidate.json \
      --output artifacts/forge-check.json
```

| Exit code | Meaning | Follow-up |
|---|---|---|
| 0 | `pass` | The candidate satisfies the rule across the eligible cases with comparable evidence. |
| 1 | `fail` | Review the listed case IDs, expected outcomes and candidate observations. |
| 2 | `insufficient` | Supply the missing baseline or observations and compare again. |
| 3 | Invalid input or execution | Fix the input or the reported execution error and rerun. |

Start with `continue-on-error: true` on the step and examine its report. Remove that setting and make the job a required check once the owner has approved the inputs and the treatment of missing evidence. In required mode, only exit code 0 passes the check.
