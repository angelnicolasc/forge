"""forge_mcp.cache — Exact-match TTL cache for tool call results.

Cache semantics mirror Anthropic's prompt caching: identical input →
identical output served from cache, with no upstream round-trip. No vector
similarity — purely content-hash equality (SHA-256 over JSON-serialized
tool name + arguments).

Thread-safe for concurrent async callers via a threading.Lock (the lock is
held only for dict operations, not during upstream calls).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any


class ToolCallCache:
    """Exact-match, TTL-based cache for MCP tool call results."""

    def __init__(self, default_ttl: float = 300.0) -> None:
        self._default_ttl = default_ttl
        # key → (result, expires_at)
        self._entries: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Key computation
    # ------------------------------------------------------------------

    @staticmethod
    def _key(tool_name: str, arguments: dict[str, Any]) -> str:
        payload = json.dumps(
            {"t": tool_name, "a": arguments},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, tool_name: str, arguments: dict[str, Any]) -> tuple[Any, bool]:
        """Return (result, hit).

        If the entry is present and unexpired, returns (result, True).
        Expired entries are evicted on read. Returns (None, False) on miss.
        """
        key = self._key(tool_name, arguments)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None, False
            result, expires_at = entry
            if time.monotonic() > expires_at:
                del self._entries[key]
                self._misses += 1
                return None, False
            self._hits += 1
            return result, True

    def put(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        ttl: float | None = None,
    ) -> None:
        """Store *result* under the (tool_name, arguments) key."""
        key = self._key(tool_name, arguments)
        expires_at = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            self._entries[key] = (result, expires_at)

    def invalidate(self, tool_name: str | None = None) -> int:
        """Evict cache entries.

        If *tool_name* is given, performs a full clear (per-tool eviction
        requires storing the tool name inside the entry — deferred as DT-2).
        Returns the number of entries removed.
        """
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
            }
