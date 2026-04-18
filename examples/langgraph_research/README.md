# LangGraph Research Agent — Forge Example

A 3-agent research pipeline demonstrating Forge's core value in 47 seconds.

## What this demonstrates

| Without Forge | With Forge |
|---|---|
| Run, see output | Run, see cost per agent |
| No visibility into token usage | Full token breakdown |
| No topology visualization | ASCII agent graph |
| No memory of past runs | Cross-run knowledge |
| No optimization | Auto-evolution proposals |

## Setup

```bash
pip install 'forge-os[langgraph]' langgraph
```

## Run with Forge (recommended)

```bash
# Basic run with cost dashboard
forge wrap flow.py --input '{"query": "What are agent harnesses?"}'

# With memory (stores results for future runs)
forge wrap flow.py --input '{"query": "Best multi-agent patterns 2026"}'

# With evolution (after 3+ runs, Forge proposes optimizations)
forge wrap flow.py \
    --input '{"query": "Latest RAG improvements"}' \
    --evolution

# Full experience: dashboard + memory + evolution
forge wrap flow.py \
    --input '{"query": "How do self-evolving agents work?"}' \
    --evolution \
    --dashboard

# Set a cost budget
forge wrap flow.py \
    --input '{"query": "Agent harness patterns"}' \
    --budget 0.50
```

## Run directly (without Forge)

```bash
python flow.py
```

## The agents

1. **Planner** — Breaks the query into 2-3 focused research questions
2. **Researcher** — Finds information for each question (replace with real LLM + search)
3. **Synthesizer** — Combines findings into a coherent answer

## Customize with real LLM calls

Replace the mock implementations in `flow.py` with real API calls:

```python
import anthropic

client = anthropic.Anthropic()

def researcher_node(state: ResearchState) -> ResearchState:
    for question in state.get("sub_questions", []):
        response = client.messages.create(
            model="claude-haiku-4-20250514",  # Use haiku for cost efficiency
            max_tokens=1024,
            messages=[{"role": "user", "content": f"Research: {question}"}]
        )
        findings[question] = response.content[0].text
    return {"findings": findings}
```

Forge will automatically track the token usage and cost for each call.
