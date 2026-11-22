"""Run three local LangGraph nodes for the historical Forge harness.

The planner produces three questions, the researcher supplies fixture text,
and the synthesizer formats a report. Run this file directly with Python or
load its graph with the SDK example in docs/quickstart.md.
"""

from __future__ import annotations

from typing import Any, TypedDict

# LangGraph imports
try:
    from langgraph.graph import END, StateGraph
except ImportError as exc:
    raise ImportError(
        "LangGraph is required for this example.\n"
        "Install with: pip install 'forge-os-adapters[langgraph]'"
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
    """Build the three research questions used by the fixture."""
    query = state.get("query", "")

    sub_questions = [
        f"What background would help answer the request {query!r}?",
        f"Which components matter for the request {query!r}?",
        f"Which examples could inform the request {query!r}?",
    ]

    print(f"  [Planner] Generated {len(sub_questions)} sub-questions for: '{query}'")
    return {"sub_questions": sub_questions}


def researcher_node(state: ResearchState) -> ResearchState:
    """Attach one placeholder to each question."""
    sub_questions = state.get("sub_questions", [])
    findings: dict[str, str] = {}

    for i, question in enumerate(sub_questions):
        findings[question] = f"[Sample response {i + 1}]"
        print(f"  [Researcher] Added placeholder {i + 1}/{len(sub_questions)}")

    return {"findings": findings}


def synthesizer_node(state: ResearchState) -> ResearchState:
    """Format the questions and fixture text as Markdown."""
    query = state.get("query", "")
    findings = state.get("findings", {})

    synthesis_parts = [f"## Fixture report for {query}\n"]
    for i, (question, finding) in enumerate(findings.items(), 1):
        synthesis_parts.append(f"### {i}. {question}\n{finding}\n")

    synthesis_parts.append(f"\n### Summary\n{len(findings)} fixture findings.")

    answer = "\n".join(synthesis_parts)
    print(f"  [Synthesizer] Formatted {len(findings)} entries")
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


# Expose the compiled graph for direct execution.
app = build_graph()


# ---------------------------------------------------------------------------
# Direct execution (without Forge)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running the LangGraph fixture directly\n")
    result = app.invoke({"query": "support ticket routing"})
    print("\n" + "=" * 60)
    print(result.get("answer", "No answer generated"))
