# Historical harness quickstart

This guide runs a local LangGraph flow with the `forge-os` 0.2.0 harness. The flow has three nodes and uses fixed responses. It needs no model credentials. For the current tool that checks saved outcomes in CI, use the [Forge Check quickstart](check.md).

## Install the harness

From the repository root, create a separate Python 3.11+ environment. The historical harness and Forge Check both install a command named `forge`. The following commands use a POSIX shell.

```sh
python3 -m venv .venv-harness
. .venv-harness/bin/activate
python -m pip install "forge-os[langgraph]==0.2.0"
python examples/langgraph_research/flow.py
```

The planner generates three questions, the researcher writes a placeholder for each one, and the synthesizer formats them into a report. The fixture makes no search or model calls.

## Run the graph through the adapter

From the same directory and environment, load the graph into `LangGraphAdapter` and attach it to the orchestrator. This selects the adapter explicitly.

```sh
python - <<'PY'
import asyncio

from forge_adapters.langgraph_adapter import LangGraphAdapter
from forge_core.harness import MetaOrchestrator
from forge_core.types import TaskEnvelope


async def main():
    adapter = LangGraphAdapter()
    await adapter.load("examples/langgraph_research/flow.py")
    harness = MetaOrchestrator()
    harness.set_adapter(adapter)
    result = await harness.run(TaskEnvelope(input={"query": "Where did the ticket go?"}))
    print(result.status.value, result.cost.llm_calls, len(result.output["findings"]))


asyncio.run(main())
PY
```

The last line should be `completed 0 3` for the run status, observed model calls and number of fixture findings. When a graph calls a model through LangChain, usage reported to its callback can also appear in the run result.

## Inspect the local API

```sh
forge observe --no-open
```

With the server running, open its [API explorer](http://127.0.0.1:8787/docs). The API stores posted run records in that server process. Starting it after an SDK run does not import the previous run.

The harness also exposes memory ingestion and an evolution loop for applications that supply run history. See the [architecture guide](architecture.md) for the components involved and the [test reference](feature-map.md) for exercised behaviors.
