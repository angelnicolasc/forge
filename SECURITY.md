# Security policy

## Report a vulnerability

Email the maintainer at [nick.dicerutti@gmail.com](mailto:nick.dicerutti@gmail.com). Include the distribution and version, the affected operation, the expected and observed behavior, and enough detail to reproduce it with synthetic data. Keep credentials and customer records out of the report. Use email for the initial disclosure so the maintainer can investigate privately.

## Scope

Forge Check (`forge-os-check`) is the maintained product. Its base CLI compares records supplied by the caller. The workflow owner chooses the eligible cases and the trusted source of observations. A `pass` applies to the evidence and rule in that comparison. [Input contract](docs/check.md)

The optional governed runtime runs on a trusted host. Your integrating application must authenticate callers and supply their actor identity. Forge checks that identity against the configured grants. Protect the SQLite ledger, configuration, backups and provider credentials with operating system permissions. An administrator with write access to the host can change the stored evidence. Use the destination system's records to confirm an external effect. [Host configuration](docs/governed.md) · [Recovery](docs/operations.md)

Code Mode is an opt-in executor for JavaScript on Linux x86_64. The host exposes explicitly enabled read bindings and checks the actor's grants and budget on dispatch. These controls apply to calls routed through the governed runtime. [Code Mode](docs/code-mode.md)

The earlier harness in `packages/` is historical and receives no routine maintenance. In `forge-spec`, assertion criteria evaluate Python expressions. Only run specifications from authors you trust. Include the package name when reporting an issue in the harness. [Historical packages](packages/README.md)
