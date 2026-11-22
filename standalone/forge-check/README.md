# Forge Check

`forge check` compares a baseline and a candidate against a rule you supply. For example, a ticket that went to the wrong queue fails the check. A missing queue observation gets its own verdict, `insufficient`. The JSON report names the affected tickets. The base wheel requires Python 3.11 or newer and has no runtime dependencies.

Install the stable release from PyPI in a dedicated environment. The historical `forge-os-cli` package also installs a `forge` executable. In Bash, run the following commands.

```bash
python -m venv .venv-forge-check
source .venv-forge-check/bin/activate
python -m pip install "forge-os-check==0.4.0"
```

[Run the support example on Windows or Linux](https://github.com/angelnicolasc/forge/blob/main/docs/check.md#try-the-support-example)

The inventory example shows a different rule. When primary stock runs low, the agent must check the reserve before a candidate can be promoted. A single-host runtime records the calls in SQLite. Code Mode, an experimental Linux x86_64 extra, lets a program call explicitly enabled read bindings. [Inventory example](https://github.com/angelnicolasc/forge/blob/main/docs/conditional-evidence.md) · [Binding guide](https://github.com/angelnicolasc/forge/blob/main/docs/governed.md) · [Code Mode](https://github.com/angelnicolasc/forge/blob/main/docs/code-mode.md)
