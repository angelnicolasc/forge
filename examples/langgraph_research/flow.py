"""LangGraph Research Agent — Forge Example

A multi-agent research flow that:
1. Planner: breaks down the query into research questions
2. Researcher: finds information for each question
3. Synthesizer: combines findings into a coherent answer

This flow is intentionally simple so you can see Forge's value clearly.
After wrapping it, you'll see:
  - Per-agent cost breakdown
  - Token usage (input/output)
  - Agent topology visualization
  - Suggestions for optimization (with --evolution flag)

Usage:
    # Direct Python execution (without Forge)
    python flow.py

    # With Forge instrumentation (the magic)
    forge wrap flow.py --input '{"query": "What are agent harnesses?"}'

    # With evolution enabled
    forge wrap flow.py \\
        --input '{"query": "Best practices for multi-agent systems"}' \\
        --evolution \\
        --dashboard
"""

from __future__ import annotations

from typing import Any, TypedDict

# LangGraph imports
try:
    from langgraph.graph import END, StateGraph
except ImportError as exc:
    raise ImportError(
        "LangGraph is required for this example.\n"
        "Install with: pip install 'forge-adapters[langgraph]' langgraph"
    ) from exc

# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------


class ResearchState(TypedDict, total=False):
    query: str
    sub_questions: list[str]
    findings: dict[str, str]
    answer: str
    errors: list[str]


# ---------------------------------------------------------------------------
# Agent nodes (mock implementations — replace with real LLM calls)
# ---------------------------------------------------------------------------


def planner_node(state: ResearchState) -> ResearchState:
    """Break the query into 2-3 focused research questions."""
    query = state.get("query", "")

    # In production: call an LLM to generate sub-questions
    # For this demo: generate deterministic sub-questions
    sub_questions = [
        f"What is the definition and core concept of: {query}?",
        f"What are the key components and architecture of: {query}?",
        f"What are the practical applications and examples of: {query}?",
    ]

    print(f"  [Planner] Generated {len(sub_questions)} sub-questions for: '{query}'")
    return {"sub_questions": sub_questions}


def researcher_node(state: ResearchState) -> ResearchState:
    """Research each sub-question and collect findings."""
    sub_questions = state.get("sub_questions", [])
    findings: dict[str, str] = {}

    for i, question in enumerate(sub_questions):
        # In production: call search APIs, LLMs, databases
        # For this demo: generate placeholder findings
        findings[question] = (
            f"Finding {i + 1}: Based on research into '{question}', "
            f"the key insight is that this is a rapidly evolving field "
            f"with significant practical implications for enterprise AI deployments. "
            f"Multiple studies confirm that systematic approaches yield 3-5x better outcomes."
        )
        print(f"  [Researcher] Answered question {i + 1}/{len(sub_questions)}")

    return {"findings": findings}


def synthesizer_node(state: ResearchState) -> ResearchState:
    """Synthesize findings into a coherent, well-structured answer."""
    query = state.get("query", "")
    findings = state.get("findings", {})

    # In production: call an LLM to synthesize
    synthesis_parts = [f"## Research Summary: {query}\n"]
    for i, (question, finding) in enumerate(findings.items(), 1):
        synthesis_parts.append(f"### {i}. {question}\n{finding}\n")

    synthesis_parts.append(
        "\n### Conclusion\n"
        "The research indicates strong convergence across multiple dimensions, "
        "suggesting this is a mature area with well-established patterns. "
        "Practitioners should focus on the systematic approaches identified above."
    )

    answer = "\n".join(synthesis_parts)
    print(f"  [Synthesizer] Produced {len(answer)} character synthesis")
    return {"answer": answer}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph() -> Any:
    """Build and compile the research agent graph."""
    graph = StateGraph(ResearchState)

    graph.add_node("planner", planner_node)
    graph.add_node("researcher", researcher_node)
    graph.add_node("synthesizer", synthesizer_node)

    graph.set_entry_point("planner")
    graph.add_edge("planner", "researcher")
    graph.add_edge("researcher", "synthesizer")
    graph.add_edge("synthesizer", END)

    return graph.compile()


# Compile at module level — Forge's adapter looks for `app` or a CompiledGraph
app = build_graph()


# ---------------------------------------------------------------------------
# Direct execution (without Forge)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running research agent directly (without Forge instrumentation)\n")
    print("For the full Forge experience, run:")
    print('  forge wrap flow.py --input \'{"query": "What are agent harnesses?"}\'')
    print('  forge wrap flow.py --input \'{"query": "..."}\'--evolution --dashboard\n')

    result = app.invoke({"query": "What are agent harnesses in AI?"})
    print("\n" + "=" * 60)
    print(result.get("answer", "No answer generated"))
