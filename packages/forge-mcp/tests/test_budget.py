"""Tests for forge_mcp.budget — ToolBudget accounting."""

from __future__ import annotations

import pytest

from forge_mcp.budget import BudgetExceededError, ToolBudget
from forge_mcp.types import ToolPolicy


def _policy(max_calls: int = 3) -> ToolPolicy:
    return ToolPolicy(allowed=True, max_calls_per_run=max_calls)


class TestToolBudget:
    def test_fresh_budget_allows_calls(self) -> None:
        budget = ToolBudget()
        budget.check("tool", _policy(max_calls=5))  # should not raise

    def test_exceeds_limit_raises(self) -> None:
        budget = ToolBudget()
        policy = _policy(max_calls=2)
        budget.record("tool")
        budget.record("tool")
        with pytest.raises(BudgetExceededError) as exc_info:
            budget.check("tool", policy)
        assert exc_info.value.tool_name == "tool"
        assert exc_info.value.limit == 2

    def test_exactly_at_limit_raises(self) -> None:
        budget = ToolBudget()
        policy = _policy(max_calls=1)
        budget.record("tool")
        with pytest.raises(BudgetExceededError):
            budget.check("tool", policy)

    def test_one_below_limit_ok(self) -> None:
        budget = ToolBudget()
        policy = _policy(max_calls=3)
        budget.record("tool")
        budget.record("tool")
        budget.check("tool", policy)  # 2 < 3, should not raise

    def test_different_tools_independent(self) -> None:
        budget = ToolBudget()
        policy = _policy(max_calls=1)
        budget.record("tool-a")
        with pytest.raises(BudgetExceededError):
            budget.check("tool-a", policy)
        budget.check("tool-b", policy)  # tool-b untouched, should not raise

    def test_snapshot_reflects_usage(self) -> None:
        budget = ToolBudget()
        budget.record("tool", tokens=100)
        budget.record("tool", tokens=200)
        snap = budget.snapshot()
        assert snap.calls_by_tool["tool"] == 2
        assert snap.tokens_by_tool["tool"] == 300
        assert snap.total_calls == 2
        assert snap.total_tokens == 300

    def test_reset_clears_state(self) -> None:
        budget = ToolBudget()
        budget.record("tool")
        budget.reset()
        snap = budget.snapshot()
        assert snap.total_calls == 0
        budget.check("tool", _policy(max_calls=1))  # should not raise after reset

    def test_snapshot_immutable_copy(self) -> None:
        budget = ToolBudget()
        budget.record("tool")
        snap = budget.snapshot()
        budget.record("tool")
        assert snap.total_calls == 1  # snap is a copy
