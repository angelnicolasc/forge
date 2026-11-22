"""Coding conversation fixture for the historical harness.

A scripted coder and reviewer return a Python function and review text.
Run this module directly with Python after installing forge-os-core==0.2.0.
It uses MockLLMProvider and does not construct an AutoGen team.
"""

from __future__ import annotations

from forge_core.testing import MockLLMProvider, MockResponse


def _coder(task: str, llm: MockLLMProvider) -> str:
    import asyncio

    async def _call() -> str:
        r = await llm.complete(
            prompt=f"Write a minimal Python function to {task}.",
            agent_id="coder",
            model="mock-model",
        )
        return r.content

    return asyncio.run(_call())


def _reviewer(code: str, llm: MockLLMProvider) -> str:
    import asyncio

    async def _call() -> str:
        r = await llm.complete(
            prompt=f"Suggest one concrete improvement to this code:\n{code}",
            agent_id="reviewer",
            model="mock-model",
        )
        return r.content

    return asyncio.run(_call())


def _build_mock_llm() -> MockLLMProvider:
    llm = MockLLMProvider()
    llm.program(
        [
            MockResponse(
                content=(
                    "def sum_list(xs):\n"
                    "    total = 0\n"
                    "    for x in xs:\n"
                    "        total += x\n"
                    "    return total"
                ),
                input_tokens=30,
                output_tokens=45,
                model="mock-model",
            ),
            MockResponse(
                content=(
                    "The built-in sum(xs) expresses this operation directly. "
                    "If non-numeric entries are possible, consider filtering or "
                    "raising a clear TypeError."
                ),
                input_tokens=55,
                output_tokens=70,
                model="mock-model",
            ),
        ]
    )
    return llm


def run(task: str = "sum a list of integers", llm: MockLLMProvider | None = None) -> dict:
    llm = llm or _build_mock_llm()
    code = _coder(task, llm)
    review = _reviewer(code, llm)
    return {"task": task, "code": code, "review": review}


app = run


if __name__ == "__main__":
    import json
    import sys

    args = {}
    if len(sys.argv) > 1:
        try:
            args = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            args = {"task": sys.argv[1]}

    out = run(task=args.get("task", "sum a list of integers"))
    print("\n=== TASK ===\n" + out["task"])
    print("\n=== CODE ===\n" + out["code"])
    print("\n=== REVIEW ===\n" + out["review"])
