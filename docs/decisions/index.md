# Architecture decisions

Forge’s first governed binding handled support tickets. Moving to inventory exposed an assumption the support example could never catch. A stock lookup takes a SKU, yet both execution paths required a ticket ID. Fixing that raised a second question. Once an agent can write a program that calls the right tool, how does its operator know the program asked *enough* questions?

These records trace those choices through the `forge-os-check` preview. Each accepted decision identifies the preview version that implements it.

| Record | Status | Question |
|---|---|---|
| [0001 · Put each tool’s contract in the revision](0001-binding-contracts.md) | Accepted | Can inventory and invoicing use their own data models while sharing one enforcement point? |
| [0002 · Ship Code Mode as a selectable read path](0002-code-mode.md) | Accepted | When is a generated program worth the extra execution step? |
| [0003 · Require evidence for conditional reads](0003-conditional-evidence.md) | Accepted | When the primary warehouse is nearly empty, what proves the agent also checked reserve stock? |
| [0004 · Show which canary roots reached dispatch](0004-rollout-traffic.md) | Proposed | How much of the traffic assigned to a canary reached the dispatch boundary, and what remains uncertain? |
