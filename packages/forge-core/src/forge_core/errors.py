"""Forge error hierarchy.

All Forge-specific exceptions inherit from ForgeError for easy catching.
"""

from __future__ import annotations


class ForgeError(Exception):
    """Base exception for all Forge errors."""


class AdapterNotFoundError(ForgeError):
    """No adapter could handle the given source."""

    def __init__(self, source: str) -> None:
        super().__init__(
            f"No adapter found for '{source}'. "
            "Install the appropriate extra (e.g., forge-adapters[langgraph]) "
            "or register a custom adapter."
        )
        self.source = source


class AdapterLoadError(ForgeError):
    """Adapter failed to load the source."""

    def __init__(self, adapter: str, source: str, reason: str) -> None:
        super().__init__(f"Adapter '{adapter}' failed to load '{source}': {reason}")
        self.adapter = adapter
        self.source = source
        self.reason = reason


class CostCeilingExceededError(ForgeError):
    """Run exceeded the configured cost ceiling."""

    def __init__(self, current: float, ceiling: float) -> None:
        super().__init__(
            f"Cost ceiling exceeded: ${current:.4f} > ${ceiling:.4f}. "
            "Increase config.cost_ceiling or optimize your flow."
        )
        self.current = current
        self.ceiling = ceiling


class EvolutionError(ForgeError):
    """Error during the self-evolution loop."""


class MemoryError(ForgeError):
    """Error in the Living Collaborative Memory."""


class TimeoutError(ForgeError):
    """Run exceeded the configured timeout."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(f"Run timed out after {timeout_seconds}s.")
        self.timeout_seconds = timeout_seconds
