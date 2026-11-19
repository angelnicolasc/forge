# Support routing example

Use these saved synthetic results with [`forge check`](../../docs/check.md). The case file fixes 24 eligible ticket IDs and assigns eight of them to the human queue.

| Input | Recorded result |
|---|---|
| `cases.json` | An independent rule for each ticket. |
| `baseline.json` | Revision `r41` recorded the expected outcome for all 24 tickets. |
| `candidate-pass.json` | Revision `r43` preserves every expected outcome. |
| `candidate-fail.json` | Revision `r42` reports completion on every ticket. Its recorded queue outcomes miss the required escalations for `S07` and `S08`. |

The records use synthetic IDs and costs. The observations describe effects at the ticket system. `human` means one confirmed escalation to the expected queue. `self_service` means the independent read found a closed ticket with no escalation.
