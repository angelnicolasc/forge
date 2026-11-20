# Forge Check

`forge check` evaluates saved agent results against a business rule and writes a JSON report for CI. It uses Python 3.11 or newer and has no runtime dependencies.

Install the `forge-os-check` wheel in a dedicated environment. The legacy `forge-os-cli` package uses the same `forge` executable name.

[Quickstart, input formats, and CI example](https://github.com/angelnicolasc/forge/blob/main/docs/check.md)

The same wheel includes a single-host runtime for explicit model and support bindings, with SQLite receipts and authorized revision changes. [Run a governed support binding](https://github.com/angelnicolasc/forge/blob/main/docs/governed.md)
