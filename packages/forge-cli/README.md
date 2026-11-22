# forge-os-cli

The CLI for the earlier Forge agent harness inspects recorded runs and evolution history, manages local memory, and starts the observe API. The standalone CI tool is [Forge Check](https://pypi.org/project/forge-os-check/). Install each in its own Python environment because both distributions provide the `forge` command.

## Installation

With Python 3.11 or later, install the harness CLI and inspect the available commands.

```bash
pip install forge-os-cli
forge --help
forge doctor
```

The [forge-os meta-package](https://pypi.org/project/forge-os/) also installs this CLI and brings in the runtime packages. `forge doctor` checks the Python version and imports. Its optional `--network` flag tests HTTP reachability of the Anthropic and OpenAI API hosts.

## Running a flow

The published 0.2.0 harness looks for registered adapter entry points when loading a flow through the CLI. Its bundled framework adapters are missing those registrations. To run the example with this release, select the adapter explicitly in Python. The [LangGraph example](https://github.com/angelnicolasc/forge/tree/main/examples/langgraph_research) shows the SDK path. The [release characterization](https://github.com/angelnicolasc/forge/blob/main/tests/characterization/p00/README.md) documents the behavior observed through each entry point.

## Inspecting the harness

| Command | Result |
| --- | --- |
| `forge observe --no-open` | Starts the REST API. Open `/docs` for the API reference or use `/api/v1/stream` for server-sent events. |
| `forge memory status` | Reports the status of the local memory backends. |
| `forge evolve status` | Reads the evolution journal and summarizes its entries. |
| `forge rules lint --help` | Shows how to check a rule pack for possible scope overlaps. |

## Tests

From a checkout with the workspace test dependencies installed, run `pytest packages/forge-cli/tests`. The tests cover the environment diagnostics and the journal view.
