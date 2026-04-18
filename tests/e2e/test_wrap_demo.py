"""End-to-end happy-path test for the ``forge wrap`` stack.

This is the ζ.7 acceptance test. It exercises the full production
pipeline — ``MetaOrchestrator`` → ``GenericCallableAdapter`` →
``instrument_llm_clients`` → ``ForgeLLMInterceptor`` → ``EventBus``
→ ``CostSummary`` → ``print_run_result`` — without needing
credentials or the real Anthropic SDK.

We drive ``MetaOrchestrator`` directly (rather than going through
``typer.testing.CliRunner``) because typer's CliRunner has a
long-standing footgun with sub-Typers that use
``invoke_without_command=True``: it refuses to route positional
arguments to the parent callback. The code path under test is
identical — ``wrap.py::_run_wrap`` is a thin wrapper around the
same orchestrator calls we make here.

The fake ``anthropic`` module is installed by
``tests/e2e/conftest.py``; ``mock-model`` pricing lives in the
production ``DefaultCostModel`` so the assertions run against the
same code path a user would hit in production.
"""

from __future__ import annotations

import asyncio
import io
from decimal import Decimal
from pathlib import Path

import pytest
from rich.console import Console

FIXTURE = Path(__file__).parent / "fixtures" / "llm_flow.py"


@pytest.mark.usefixtures("fake_anthropic_sdk")
def test_wrap_produces_completed_run_with_nonzero_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """`forge wrap` on a minimal LLM flow shows COMPLETED, TOTAL, >$0.00.

    The fixture flow makes two ``anthropic.Anthropic().messages.create``
    calls. The generic adapter's instrumentation scope patches the fake
    module's ``Messages.create``, so the interceptor receives real token
    counts and the cost model (``mock-model`` @ $1/1M tokens) produces a
    non-zero total.
    """
    from forge_adapters.generic import GenericCallableAdapter
    from forge_core.harness import MetaOrchestrator
    from forge_core.types import RunConfig, RunStatus, TaskEnvelope
    from forge_observe.exporters import console as observe_console
    from forge_observe.exporters.console import print_run_result

    # Redirect Rich output into a buffer so assertions can grep it.
    buf = io.StringIO()
    capture_console = Console(file=buf, force_terminal=False, width=200, legacy_windows=False)
    monkeypatch.setattr(observe_console, "_console", capture_console)

    # Load the fixture module so its ``app`` callable is importable.
    import importlib.util

    spec = importlib.util.spec_from_file_location("_forge_e2e_fixture", str(FIXTURE))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    flow_fn = module.app

    async def _drive() -> object:
        orchestrator = MetaOrchestrator()
        adapter = GenericCallableAdapter(fn=flow_fn, adapter_name="e2e-fixture")
        orchestrator._adapter = adapter
        orchestrator._wire_adapter(adapter)
        await adapter.load(str(FIXTURE))
        run_config = RunConfig(
            enable_evolution=False,
            enable_memory=False,
            cost_ceiling=Decimal("100.00"),
        )
        envelope = TaskEnvelope(input={"query": "test"}, config=run_config)
        return await orchestrator.run(envelope)

    run_result = asyncio.run(_drive())

    # Render the Rich dashboard exactly as ``forge wrap`` does.
    print_run_result(run_result, verbose=False)

    out = buf.getvalue()

    assert run_result.status == RunStatus.COMPLETED, (
        f"run status was {run_result.status}, expected COMPLETED\nerrors={run_result.errors}"
    )

    # Status banner — the Rich console prints "✓ COMPLETED"; we relax to
    # the upper-cased word so we're not coupled to glyph rendering.
    assert "COMPLETED" in out, f"no COMPLETED banner in output:\n{out}"

    # Cost panel only renders when total_cost > 0, so its presence is
    # itself proof that the instrumentation path fired.
    assert "TOTAL" in out, f"no TOTAL row in output — cost panel likely suppressed:\n{out}"

    # And it must be a positive dollar amount, not $0.00000.
    assert "$0.00000" not in out.split("TOTAL", 1)[1][:40], (
        f"TOTAL row shows zero cost — instrumentation did not fire:\n{out}"
    )

    # Topology tree renders whenever the adapter reports at least one
    # agent card; the generic adapter always reports one.
    assert "Agent Topology" in out or "generic" in out.lower(), (
        f"no topology section in output:\n{out}"
    )
