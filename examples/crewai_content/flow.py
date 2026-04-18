"""CrewAI Content Team — Forge Example.

A two-agent Crew that drafts a short article:

1. **Researcher** — gathers bullet points on the topic
2. **Writer**    — turns the bullets into a paragraph

The file is designed to be runnable in three ways:

* ``python flow.py`` — always works. Uses the local mock LLM built into
  Forge (:class:`forge_core.testing.MockLLMProvider`) so the demo has
  no network or credential requirements.
* ``forge wrap flow.py --input '{"topic":"AI harnesses"}'`` — the
  viral demo. Forge instruments every call and prints cost breakdown.
* With real CrewAI (``pip install crewai``) and an API key in the env,
  replace the ``run_mock`` path with ``crew.kickoff``.

Ships with the CrewAI package being optional: if ``crewai`` isn't
installed, we still produce a realistic-looking run by calling the mock
provider directly from each "agent" function.
"""

from __future__ import annotations

from forge_core.testing import MockLLMProvider, MockResponse


def _researcher(topic: str, llm: MockLLMProvider) -> list[str]:
    """Produce 3 bullet points of "research" on ``topic``."""
    # A real CrewAI agent would call ``self.llm(...)``; the mock LLM
    # stands in so this example has no network or credential requirement.
    import asyncio

    async def _call() -> str:
        r = await llm.complete(
            prompt=f"List three key insights about: {topic}",
            agent_id="researcher",
            model="mock-model",
        )
        return r.content

    text = asyncio.run(_call())
    return [line.strip("- ") for line in text.splitlines() if line.strip()]


def _writer(topic: str, bullets: list[str], llm: MockLLMProvider) -> str:
    """Fold the bullets into a paragraph."""
    import asyncio

    async def _call() -> str:
        joined = "; ".join(bullets) if bullets else topic
        r = await llm.complete(
            prompt=f"Write a short paragraph about {topic} covering: {joined}",
            agent_id="writer",
            model="mock-model",
        )
        return r.content

    return asyncio.run(_call())


def _build_mock_llm() -> MockLLMProvider:
    """Default mock script — two calls, deterministic output."""
    llm = MockLLMProvider()
    llm.program(
        [
            MockResponse(
                content=(
                    "- Agent harnesses instrument otherwise opaque multi-agent flows\n"
                    "- They make cost, latency, and errors observable per-agent\n"
                    "- Self-evolving harnesses can suggest mutations that reduce cost"
                ),
                input_tokens=42,
                output_tokens=60,
                model="mock-model",
            ),
            MockResponse(
                content=(
                    "Agent harnesses sit between application code and the underlying "
                    "multi-agent framework, capturing every LLM call so teams can see "
                    "per-agent cost and latency. The most ambitious ones also propose "
                    "and evaluate mutations automatically, turning 'observability' into "
                    "'continuous improvement.'"
                ),
                input_tokens=90,
                output_tokens=110,
                model="mock-model",
            ),
        ]
    )
    return llm


def run(topic: str = "AI harnesses", llm: MockLLMProvider | None = None) -> str:
    """Entry point used by ``forge wrap`` and ``__main__``."""
    llm = llm or _build_mock_llm()
    bullets = _researcher(topic, llm)
    article = _writer(topic, bullets, llm)
    return article


# Module-level ``app`` so the GenericCallableAdapter picks this up.
app = run


if __name__ == "__main__":
    import json
    import sys

    args = {}
    if len(sys.argv) > 1:
        try:
            args = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            args = {"topic": sys.argv[1]}

    topic = args.get("topic", "AI harnesses")
    output = run(topic=topic)
    print("\n" + "=" * 60)
    print(output)
    print("=" * 60)
    print(
        "\nFor the full Forge experience, run:\n"
        "  forge wrap flow.py --input '{\"topic\":\"AI harnesses\"}'"
    )
