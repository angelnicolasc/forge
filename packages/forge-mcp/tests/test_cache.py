"""Tests for forge_mcp.cache — ToolCallCache exact-match TTL cache."""

from __future__ import annotations

import time

from forge_mcp.cache import ToolCallCache


class TestToolCallCache:
    def test_miss_on_empty(self) -> None:
        cache = ToolCallCache()
        result, hit = cache.get("tool", {"a": 1})
        assert not hit
        assert result is None

    def test_hit_after_put(self) -> None:
        cache = ToolCallCache()
        cache.put("tool", {"a": 1}, "result")
        result, hit = cache.get("tool", {"a": 1})
        assert hit
        assert result == "result"

    def test_different_args_miss(self) -> None:
        cache = ToolCallCache()
        cache.put("tool", {"a": 1}, "result-a")
        _, hit = cache.get("tool", {"a": 2})
        assert not hit

    def test_different_tool_miss(self) -> None:
        cache = ToolCallCache()
        cache.put("tool-a", {"a": 1}, "result")
        _, hit = cache.get("tool-b", {"a": 1})
        assert not hit

    def test_expired_entry_is_miss(self) -> None:
        cache = ToolCallCache(default_ttl=0.05)
        cache.put("tool", {"x": 1}, "val")
        time.sleep(0.1)
        _, hit = cache.get("tool", {"x": 1})
        assert not hit

    def test_unexpired_entry_is_hit(self) -> None:
        cache = ToolCallCache(default_ttl=60.0)
        cache.put("tool", {"x": 1}, "val")
        _, hit = cache.get("tool", {"x": 1})
        assert hit

    def test_custom_ttl_per_entry(self) -> None:
        cache = ToolCallCache(default_ttl=60.0)
        cache.put("tool", {"x": 1}, "val", ttl=0.05)
        time.sleep(0.1)
        _, hit = cache.get("tool", {"x": 1})
        assert not hit

    def test_stats_tracks_hits_and_misses(self) -> None:
        cache = ToolCallCache()
        cache.put("tool", {"a": 1}, "v")
        cache.get("tool", {"a": 1})  # hit
        cache.get("tool", {"a": 2})  # miss
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["entries"] == 1

    def test_invalidate_clears_all(self) -> None:
        cache = ToolCallCache()
        cache.put("tool-a", {"x": 1}, "a")
        cache.put("tool-b", {"x": 1}, "b")
        removed = cache.invalidate()
        assert removed == 2
        assert cache.stats()["entries"] == 0

    def test_put_overwrite(self) -> None:
        cache = ToolCallCache()
        cache.put("tool", {"k": 1}, "v1")
        cache.put("tool", {"k": 1}, "v2")
        result, _ = cache.get("tool", {"k": 1})
        assert result == "v2"

    def test_args_order_agnostic(self) -> None:
        """JSON sort_keys=True means argument order doesn't affect the cache key."""
        cache = ToolCallCache()
        cache.put("tool", {"b": 2, "a": 1}, "v")
        result, hit = cache.get("tool", {"a": 1, "b": 2})
        assert hit
        assert result == "v"
