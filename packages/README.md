# Earlier Forge agent harness

The `packages/` directory contains the multi-package harness. [Forge Check](https://pypi.org/project/forge-os-check/) is the standalone product for evaluating saved agent runs in CI. The [harness quickstart](../docs/quickstart.md) uses a separate environment because both CLIs install the `forge` executable.

| PyPI distribution | Source | Purpose |
| --- | --- | --- |
| [forge-os-core](https://pypi.org/project/forge-os-core/) | [forge-core](./forge-core/) | Orchestration and shared types |
| [forge-os-adapters](https://pypi.org/project/forge-os-adapters/) | [forge-adapters](./forge-adapters/) | Framework adapters |
| [forge-os-cli](https://pypi.org/project/forge-os-cli/) | [forge-cli](./forge-cli/) | Harness CLI |
| [forge-os-memory](https://pypi.org/project/forge-os-memory/) | [forge-memory](./forge-memory/) | Cross-run memory |
| [forge-os-observe](https://pypi.org/project/forge-os-observe/) | [forge-observe](./forge-observe/) | Metrics, cost tracking and traces |
| [forge-os-mcp](https://pypi.org/project/forge-os-mcp/) | [forge-mcp](./forge-mcp/) | MCP tool policies and routing |
| [forge-os](https://pypi.org/project/forge-os/) | [forge-os](./forge-os/) | Meta-package for the harness |
| [forge-rules](https://pypi.org/project/forge-rules/) | [forge-rules](./forge-rules/) | Rule packs and scope matching |
| [forge-skills](https://pypi.org/project/forge-skills/) | [forge-skills](./forge-skills/) | Skill loading and dispatch |
| [forge-spec](https://pypi.org/project/forge-spec/) | [forge-spec](./forge-spec/) | Run verification reports |
| [forge-review](https://pypi.org/project/forge-review/) | [forge-review](./forge-review/) | Review policies |
