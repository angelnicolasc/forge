# forge-os

Install the earlier Forge agent harness to run and instrument agent workflows. The standalone CI product is [Forge Check](https://pypi.org/project/forge-os-check/). Use separate Python environments because both products provide the `forge` command.

The harness includes the following distributions.

- [forge-os-core](https://pypi.org/project/forge-os-core/) provides orchestration and shared types.
- [forge-os-cli](https://pypi.org/project/forge-os-cli/) installs the harness CLI.
- [forge-os-observe](https://pypi.org/project/forge-os-observe/) records traces, costs and metrics.
- [forge-os-memory](https://pypi.org/project/forge-os-memory/) provides cross-run memory.
- [forge-os-adapters](https://pypi.org/project/forge-os-adapters/) integrates agent frameworks.

## Install

```bash
pip install forge-os
```

Framework extras:

```bash
pip install "forge-os[langgraph]"
pip install "forge-os[crewai]"
pip install "forge-os[autogen]"
pip install "forge-os[all]"
```

## Using the harness

From a source checkout, follow the [harness quickstart](https://github.com/angelnicolasc/forge/blob/main/docs/quickstart.md). The [test map](https://github.com/angelnicolasc/forge/blob/main/docs/feature-map.md) links selected harness behaviors to their tests. The [architecture guide](https://github.com/angelnicolasc/forge/blob/main/docs/architecture.md) describes how the packages work together.

## License

Apache-2.0
