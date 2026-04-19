# Quickstart

Get from zero to a live, instrumented multi-agent flow in under 5 minutes.

## 1. Install

```bash
pip install forge-os
```

Or with specific framework adapters:

```bash
pip install 'forge-os[langgraph]'   # LangGraph support
pip install 'forge-os[crewai]'      # CrewAI support
pip install 'forge-os[autogen]'     # AutoGen support
pip install 'forge-os[all]'         # Everything
```

## 2. Wrap your existing flow

=== "LangGraph"

    ```python
    # my_flow.py — your existing LangGraph flow
    from langgraph.graph import StateGraph, END
    from typing import TypedDict

    class State(TypedDict):
        query: str
        answer: str

    def researcher(state: State) -> State:
        # ... your LLM call
        return {"answer": f"Research result for: {state['query']}"}

    graph = StateGraph(State)
    graph.add_node("researcher", researcher)
    graph.set_entry_point("researcher")
    graph.add_edge("researcher", END)
    app = graph.compile()
    ```

    ```bash
    forge wrap my_flow.py --input '{"query": "What is agent harness?"}'
    ```

=== "CrewAI"

    ```python
    # my_crew.py
    from crewai import Agent, Crew, Task

    researcher = Agent(role="Researcher", goal="Find facts", backstory="Expert researcher")
    task = Task(description="Research: {topic}", agent=researcher)
    crew = Crew(agents=[researcher], tasks=[task])
    ```

    ```bash
    forge wrap my_crew.py --input '{"topic": "agent harnesses 2026"}'
    ```

=== "AutoGen"

    ```python
    # my_team.py
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.teams import RoundRobinGroupChat

    agent = AssistantAgent("assistant", model_client=...)
    team = RoundRobinGroupChat([agent])
    ```

    ```bash
    forge wrap my_team.py --input '{"task": "Summarize RAG papers"}'
    ```

## 3. Start the observe API

```bash
forge observe
```

This starts the REST + SSE metrics server at `http://localhost:8787` (try
`http://localhost:8787/docs` for the OpenAPI explorer). A web UI on top of
this API is planned for v0.2.0 — see the roadmap in the project README.

## 4. Enable memory

Results are automatically stored in the Living Collaborative Memory.
Query them later:

```bash
forge memory query "What did the researcher find about RAG?"
forge memory status
```

## 5. Enable evolution

After a few runs, let Forge propose optimizations:

```bash
forge evolve my_flow.py --mode suggest
```

Or let it apply them automatically:

```bash
forge evolve my_flow.py --mode auto --runs 10
```

## Next steps

- [Architecture](architecture.md) — understand how Forge works under the hood
- [Feature map](feature-map.md) — every feature claim mapped to the test that proves it
