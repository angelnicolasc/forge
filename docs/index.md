# Forge Check

Use Forge Check to decide whether a recorded candidate satisfies a business rule your team owns. Start with saved results in CI. Add a governed binding when you also need to authorize a call, reserve its budget and record what happened.

## Get your first verdict

The support example follows 24 synthetic tickets. The saved queue observations include two confirmed routing mistakes. With those same observations missing, the check reports insufficient evidence and keeps all 24 tickets in its coverage denominator. The example runs on the base `forge-os-check` wheel using Python 3.11 or newer. It requires no model credentials. [Install 0.4.0 and run the support example](check.md#try-the-support-example)

| What you need to do | Where to start |
|---|---|
| Compare a candidate against a baseline in your own CI | [Export a case and interpret exit codes](check.md#supply-a-case-from-your-own-agent) |
| Authorize a tool call and inspect its receipt | [Run a governed binding on one host](governed.md) |
| Decide whether a conditional step was skipped | [Read the inventory evidence and the Gate's verdict](conditional-evidence.md) |
| Compose multiple approved reads in one program | [Enable the experimental Linux x86_64 Code Mode extra](code-mode.md) |
| Limit a rollout, stop future dispatches or restore the ledger | [Operate a canary and recover](operations.md) |

A **revision** fixes the manifest a task runs under. A **receipt** records a binding decision and the associated call. The **Gate** evaluates the evidence tied to a proposed revision and produces the verdict an operator needs before promotion. [Design decisions](decisions/index.md)

## Historical Forge harness

The historical multi-package harness is no longer actively maintained. It has its own CLI, framework adapters, memory and tracing. Give it a separate environment from Forge Check because both CLIs use the executable name `forge`. [Harness quickstart](quickstart.md) · [Architecture](architecture.md) · [Feature map](feature-map.md)
