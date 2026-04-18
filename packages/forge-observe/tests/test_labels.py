"""Tests for LabelSanitizer (fase ε.2)."""

from __future__ import annotations

from forge_observe.labels import DEFAULT_ALLOWLIST, LabelSanitizer


def test_keys_outside_allowlist_are_stripped() -> None:
    sanitizer = LabelSanitizer(allowlist={"run_id", "agent_id"})
    out = sanitizer.sanitize({"run_id": "r1", "agent_id": "a1", "user_email": "x@y.z"})
    assert out == {"run_id": "r1", "agent_id": "a1"}
    assert sanitizer.stats.keys_stripped == 1


def test_default_allowlist_includes_expected_keys() -> None:
    for key in ("run_id", "agent_id", "adapter", "model", "tool_name", "status", "kind"):
        assert key in DEFAULT_ALLOWLIST


def test_none_and_empty_normalized() -> None:
    s = LabelSanitizer(allowlist={"run_id", "model"})
    out = s.sanitize({"run_id": None, "model": "  "})
    assert out == {"run_id": "<none>", "model": "<empty>"}


def test_value_below_cap_passes_through() -> None:
    s = LabelSanitizer(allowlist={"agent_id"}, max_unique_per_key=3)
    for v in ("a", "b", "c", "a", "b"):
        assert s.sanitize({"agent_id": v}) == {"agent_id": v}
    assert s.stats.values_hashed == 0


def test_value_over_cap_is_hashed_to_bucket() -> None:
    s = LabelSanitizer(allowlist={"agent_id"}, max_unique_per_key=2)
    assert s.sanitize({"agent_id": "a"}) == {"agent_id": "a"}
    assert s.sanitize({"agent_id": "b"}) == {"agent_id": "b"}
    # Third distinct value → bucketed.
    out1 = s.sanitize({"agent_id": "c"})
    assert out1["agent_id"].startswith("bucket_")
    assert len(out1["agent_id"]) == len("bucket_") + 8
    # Same overflow value hashes to the same bucket deterministically.
    out2 = s.sanitize({"agent_id": "c"})
    assert out2 == out1
    # Different overflow value maps to a different bucket.
    out3 = s.sanitize({"agent_id": "d"})
    assert out3["agent_id"].startswith("bucket_")
    assert out3 != out1
    assert s.stats.values_hashed >= 2


def test_sanitize_does_not_mutate_input() -> None:
    s = LabelSanitizer(allowlist={"run_id"})
    src = {"run_id": "r1", "banned": "drop"}
    s.sanitize(src)
    assert "banned" in src  # untouched


def test_update_allowlist_takes_effect_immediately() -> None:
    s = LabelSanitizer(allowlist={"run_id"})
    assert s.sanitize({"agent_id": "a1"}) == {}
    s.update_allowlist({"agent_id"})
    assert s.sanitize({"agent_id": "a1"}) == {"agent_id": "a1"}


def test_cap_respects_minimum_of_one() -> None:
    s = LabelSanitizer(allowlist={"k"}, max_unique_per_key=0)
    # First unique value passes; everything else buckets.
    assert s.sanitize({"k": "first"}) == {"k": "first"}
    out = s.sanitize({"k": "second"})
    assert out["k"].startswith("bucket_")
