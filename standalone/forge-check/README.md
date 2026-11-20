# Forge Check

`forge check` compares saved agent results against a business rule and writes a JSON verdict for CI. The base `forge-os-check` wheel requires Python 3.11 or newer and has no runtime dependencies.

For the stable 0.4.0 release from PyPI, use a dedicated environment because legacy `forge-os-cli` also provides a `forge` executable.

```bash
python -m venv .venv-forge-check
source .venv-forge-check/bin/activate
python -m pip install "forge-os-check==0.4.0"
```

[Get your first verdict and add a CI check](https://github.com/angelnicolasc/forge/blob/main/docs/check.md#try-the-support-example)

Version 0.4.0 includes conditional inventory checks and a single-host runtime with SQLite receipts and authorized revision changes. Code Mode is an experimental Linux x86_64 extra for explicitly enabled read bindings. [Conditional evidence](https://github.com/angelnicolasc/forge/blob/main/docs/conditional-evidence.md) · [Governed bindings](https://github.com/angelnicolasc/forge/blob/main/docs/governed.md) · [Code Mode quickstart](https://github.com/angelnicolasc/forge/blob/main/docs/code-mode.md)
