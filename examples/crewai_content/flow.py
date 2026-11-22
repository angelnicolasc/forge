"""Content-writing fixture for the historical harness.

A scripted researcher and writer produce notes and a paragraph.
Run this module directly with Python after installing forge-os-core==0.2.0.
It uses MockLLMProvider and does not construct a CrewAI crew.
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
                    "- The example runs a researcher and a writer\n"
                    "- The researcher returns three scripted notes\n"
                    "- The writer receives those notes along with the topic"
                ),
                input_tokens=42,
                output_tokens=60,
                model="mock-model",
            ),
            MockResponse(
                content=(
                    "The researcher supplies three notes to the writer. Both use "
                    "scripted responses from the local mock provider."
                ),
                input_tokens=90,
                output_tokens=110,
                model="mock-model",
            ),
        ]
    )
    return llm


def run(topic: str = "AI harnesses", llm: MockLLMProvider | None = None) -> str:
    """Run the two scripted steps."""
    llm = llm or _build_mock_llm()
    bullets = _researcher(topic, llm)
    article = _writer(topic, bullets, llm)
    return article


# Alias available to callers that load the example as a module.
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
