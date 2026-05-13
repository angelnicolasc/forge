"""forge_mcp.budget — Per-run tool call accounting and enforcement.

The budget is scoped to a single run (or MetaMCPServer lifetime) and resets
via reset(). Thread-safe for concurrent coroutines via a threading.Lock.
"""

from __future__ import annotations

import threading

from forge_mcp.types import BudgetSnapshot, ToolPolicy


class BudgetExceededError(RuntimeError):
    """Raised when a tool has consumed all its allowed calls for this run."""

    def __init__(self, tool_name: str, limit: int) -> None:
        super().__init__(
            f"Tool '{tool_name}' has reached its call limit of {limit} per run."
        )
        self.tool_name = tool_name
        self.limit = limit


class ToolBudget:
    """Thread-safe accumulator for per-run tool call and token usage."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}
        self._tokens: dict[str, int] = {}

    def check(self, tool_name: str, policy: ToolPolicy) -> None:
        """Raise BudgetExceededError if tool has reached its call-count ceiling."""
        with self._lock:
            used = self._calls.get(tool_name, 0)
        if used >= policy.max_calls_per_run:
            raise BudgetExceededError(tool_name, policy.max_calls_per_run)

    def record(self, tool_name: str, tokens: int = 0) -> None:
        """Increment counters after a successful call."""
        with self._lock:
            self._calls[tool_name] = self._calls.get(tool_name, 0) + 1
            self._tokens[tool_name] = self._tokens.get(tool_name, 0) + tokens

    def reset(self) -> None:
        """Clear all counters (start of a new run)."""
        with self._lock:
            self._calls.clear()
            self._tokens.clear()

    def snapshot(self) -> BudgetSnapshot:
        """Return an immutable view of current usage."""
        with self._lock:
            calls = dict(self._calls)
            tokens = dict(self._tokens)
        return BudgetSnapshot(
            calls_by_tool=calls,
            tokens_by_tool=tokens,
            total_calls=sum(calls.values()),
            total_tokens=sum(tokens.values()),
        )
