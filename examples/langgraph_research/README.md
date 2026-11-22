# LangGraph research fixture

This example runs three deterministic LangGraph nodes. The planner turns its input into three questions, the researcher supplies a placeholder for each, and the synthesizer formats the results. The example runs locally without model credentials.

From the root of a Forge checkout, use a dedicated Python 3.11+ environment.

```sh
python3 -m venv .venv-harness
. .venv-harness/bin/activate
python -m pip install "forge-os[langgraph]==0.2.0"
python examples/langgraph_research/flow.py
```

The output shows each node running and includes three labeled placeholders. To inspect the run status, number of model calls and findings through the harness, use the [SDK example in the historical quickstart](../../docs/quickstart.md#run-the-graph-through-the-adapter).

The `planner_node`, `researcher_node` and `synthesizer_node` functions in [flow.py](flow.py) are the extension points for your own data source. If you add a model call, record its actual usage through the callback supported by the framework. The included flow reports zero model calls.
