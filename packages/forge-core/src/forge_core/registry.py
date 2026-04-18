"""Global registry for adapters, memory backends, and mutators.

The registry uses a singleton pattern with thread-safe access.
Plugins register themselves via Python entry points or explicit
calls to register().
"""

from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING, Any

import structlog

from forge_core.errors import AdapterNotFoundError

if TYPE_CHECKING:
    from pathlib import Path

    from forge_core.protocols import Adapter, MemoryBackend, Mutator

logger = structlog.get_logger()

_ENTRY_POINT_GROUP_ADAPTERS = "forge.adapters"
_ENTRY_POINT_GROUP_MEMORY = "forge.memory_backends"
_ENTRY_POINT_GROUP_MUTATORS = "forge.mutators"


class Registry:
    """Central registry for all pluggable Forge components."""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}
        self._memory_backends: dict[str, MemoryBackend] = {}
        self._mutators: dict[str, Mutator] = {}
        self._loaded_entry_points = False

    # -- Adapters ----------------------------------------------------------

    def register_adapter(self, adapter: Adapter) -> None:
        """Register an adapter by its name."""
        self._adapters[adapter.name] = adapter
        logger.info("adapter.registered", name=adapter.name)

    def get_adapter(self, name: str) -> Adapter:
        """Get a registered adapter by name."""
        self._ensure_entry_points()
        if name not in self._adapters:
            raise AdapterNotFoundError(name)
        return self._adapters[name]

    def detect_adapter(self, source: str | Path) -> Adapter:
        """Auto-detect which adapter can handle the given source."""
        self._ensure_entry_points()
        source_str = str(source)
        for adapter in self._adapters.values():
            if adapter.detect(source_str):
                logger.info("adapter.detected", name=adapter.name, source=source_str)
                return adapter
        raise AdapterNotFoundError(source_str)

    def list_adapters(self) -> list[str]:
        """List all registered adapter names."""
        self._ensure_entry_points()
        return list(self._adapters.keys())

    # -- Memory Backends ---------------------------------------------------

    def register_memory_backend(self, backend: MemoryBackend) -> None:
        """Register a memory backend by its type."""
        self._memory_backends[backend.backend_type] = backend
        logger.info("memory_backend.registered", type=backend.backend_type)

    def get_memory_backend(self, backend_type: str) -> MemoryBackend:
        """Get a registered memory backend by type."""
        self._ensure_entry_points()
        if backend_type not in self._memory_backends:
            raise KeyError(f"Memory backend '{backend_type}' not registered.")
        return self._memory_backends[backend_type]

    def list_memory_backends(self) -> list[str]:
        """List all registered memory backend types."""
        self._ensure_entry_points()
        return list(self._memory_backends.keys())

    # -- Mutators ----------------------------------------------------------

    def register_mutator(self, mutator: Mutator) -> None:
        """Register a mutation strategy."""
        self._mutators[mutator.mutation_kind] = mutator
        logger.info("mutator.registered", kind=mutator.mutation_kind)

    def get_mutator(self, kind: str) -> Mutator:
        """Get a registered mutator by kind."""
        self._ensure_entry_points()
        if kind not in self._mutators:
            raise KeyError(f"Mutator '{kind}' not registered.")
        return self._mutators[kind]

    def list_mutators(self) -> list[str]:
        """List all registered mutator kinds."""
        self._ensure_entry_points()
        return list(self._mutators.keys())

    # -- Entry point discovery ---------------------------------------------

    def _ensure_entry_points(self) -> None:
        """Lazy-load entry points from installed packages."""
        if self._loaded_entry_points:
            return
        self._loaded_entry_points = True
        self._load_entry_points(_ENTRY_POINT_GROUP_ADAPTERS, self._adapters)
        self._load_entry_points(_ENTRY_POINT_GROUP_MEMORY, self._memory_backends)
        self._load_entry_points(_ENTRY_POINT_GROUP_MUTATORS, self._mutators)

    def _load_entry_points(self, group: str, target: dict[str, Any]) -> None:
        """Load all entry points for a given group."""
        try:
            eps = importlib.metadata.entry_points(group=group)
        except Exception:
            return
        for ep in eps:
            try:
                obj = ep.load()
                if callable(obj):
                    instance = obj()
                    key = (
                        getattr(instance, "name", None)
                        or getattr(instance, "backend_type", None)
                        or getattr(instance, "mutation_kind", ep.name)
                    )
                    target[str(key)] = instance
                    logger.debug("entry_point.loaded", group=group, name=ep.name)
            except Exception as exc:
                logger.warning("entry_point.load_failed", group=group, name=ep.name, error=str(exc))


# Singleton instance
_registry = Registry()


def get_registry() -> Registry:
    """Get the global Forge registry."""
    return _registry
