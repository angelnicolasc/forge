"""Generic-callable flow fixture that makes a fake ``anthropic`` call.

``forge wrap`` on this file selects the
:class:`~forge_adapters.generic.GenericCallableAdapter`, which wraps the
call in :func:`forge_observe.llm_clients.instrument_llm_clients`. The
patched ``anthropic.resources.messages.Messages.create`` reports real
token counts to the interceptor, which in turn populates
``RunResult.cost`` so the Rich output shows a non-zero TOTAL.

The fake module is installed by :mod:`tests.e2e.conftest` before
``forge wrap`` runs; here we just ``import anthropic`` normally.
"""

from __future__ import annotations

import anthropic


def run(inp: dict) -> dict:
    client = anthropic.Anthropic()
    planner = client.messages.create(
        model="mock-model",
        messages=[{"role": "user", "content": f"Plan for: {inp.get('query', '')}"}],
        max_tokens=256,
    )
    researcher = client.messages.create(
        model="mock-model",
        messages=[{"role": "user", "content": "Research the plan"}],
        max_tokens=512,
    )
    plan_text = planner.content[0].text if planner.content else ""
    research_text = researcher.content[0].text if researcher.content else ""
    return {"plan": plan_text, "research": research_text}


# Generic adapter looks for module-level ``app`` or ``main`` callable.
app = run
