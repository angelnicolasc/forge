"""Tests for forge_review.policy — PolicyGate."""

from __future__ import annotations

from forge_review.policy import PolicyGate
from forge_review.types import (
    Finding,
    FindingSeverity,
    HookKind,
    ReviewConfig,
    ReviewResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finding(severity: FindingSeverity, confidence: float = 1.0) -> Finding:
    return Finding(severity=severity, title="t", message="m", confidence=confidence)


def _result(*severities: FindingSeverity, confidences: list[float] | None = None) -> ReviewResult:
    if confidences is None:
        confidences = [1.0] * len(severities)
    findings = [
        Finding(severity=s, title=f"f{i}", message="m", confidence=c)
        for i, (s, c) in enumerate(zip(severities, confidences, strict=False))
    ]
    return ReviewResult(hook=HookKind.PRE_MERGE, findings=findings)


# ---------------------------------------------------------------------------
# Default config (block P0 + P1)
# ---------------------------------------------------------------------------


class TestPolicyGateDefaults:
    def test_no_findings_allows(self):
        decision = PolicyGate().evaluate(_result())
        assert decision.allowed is True
        assert decision.blocked_by == []

    def test_p2_only_allows(self):
        decision = PolicyGate().evaluate(_result(FindingSeverity.P2))
        assert decision.allowed is True

    def test_p3_only_allows(self):
        decision = PolicyGate().evaluate(_result(FindingSeverity.P3))
        assert decision.allowed is True

    def test_p2_and_p3_allows(self):
        decision = PolicyGate().evaluate(_result(FindingSeverity.P2, FindingSeverity.P3))
        assert decision.allowed is True

    def test_p0_blocks(self):
        decision = PolicyGate().evaluate(_result(FindingSeverity.P0))
        assert decision.allowed is False
        assert len(decision.blocked_by) == 1
        assert decision.blocked_by[0].severity == FindingSeverity.P0

    def test_p1_blocks(self):
        decision = PolicyGate().evaluate(_result(FindingSeverity.P1))
        assert decision.allowed is False

    def test_reason_mentions_title(self):
        r = ReviewResult(
            hook=HookKind.PRE_MERGE,
            findings=[Finding(severity=FindingSeverity.P0, title="Critical error", message="m")],
        )
        decision = PolicyGate().evaluate(r)
        assert "Critical error" in decision.reason

    def test_mixed_blocks_when_p0_present(self):
        decision = PolicyGate().evaluate(
            _result(FindingSeverity.P2, FindingSeverity.P0, FindingSeverity.P3)
        )
        assert decision.allowed is False
        assert len(decision.blocked_by) == 1

    def test_multiple_blocking_all_in_blocked_by(self):
        decision = PolicyGate().evaluate(
            _result(FindingSeverity.P0, FindingSeverity.P1, FindingSeverity.P1)
        )
        assert decision.allowed is False
        assert len(decision.blocked_by) == 3

    def test_review_result_id_propagated(self):
        r = _result()
        decision = PolicyGate().evaluate(r)
        assert decision.review_result_id == r.id

    def test_allows_method_true_when_no_blocking(self):
        gate = PolicyGate()
        assert gate.allows(_result(FindingSeverity.P2)) is True

    def test_allows_method_false_when_blocking(self):
        gate = PolicyGate()
        assert gate.allows(_result(FindingSeverity.P0)) is False


# ---------------------------------------------------------------------------
# min_confidence filter
# ---------------------------------------------------------------------------


class TestPolicyGateMinConfidence:
    def test_low_confidence_p0_does_not_block(self):
        config = ReviewConfig(min_confidence=0.8)
        decision = PolicyGate(config).evaluate(_result(FindingSeverity.P0, confidences=[0.5]))
        assert decision.allowed is True

    def test_exactly_at_threshold_blocks(self):
        config = ReviewConfig(min_confidence=0.8)
        decision = PolicyGate(config).evaluate(_result(FindingSeverity.P0, confidences=[0.8]))
        assert decision.allowed is False

    def test_above_threshold_blocks(self):
        config = ReviewConfig(min_confidence=0.8)
        decision = PolicyGate(config).evaluate(_result(FindingSeverity.P0, confidences=[0.9]))
        assert decision.allowed is False


# ---------------------------------------------------------------------------
# Custom block_on
# ---------------------------------------------------------------------------


class TestPolicyGateCustomBlockOn:
    def test_block_only_p0(self):
        config = ReviewConfig(block_on=[FindingSeverity.P0])
        decision = PolicyGate(config).evaluate(_result(FindingSeverity.P1))
        assert decision.allowed is True

    def test_block_p2_too(self):
        config = ReviewConfig(block_on=[FindingSeverity.P0, FindingSeverity.P1, FindingSeverity.P2])
        decision = PolicyGate(config).evaluate(_result(FindingSeverity.P2))
        assert decision.allowed is False

    def test_empty_block_on_always_allows(self):
        config = ReviewConfig(block_on=[])
        decision = PolicyGate(config).evaluate(_result(FindingSeverity.P0, FindingSeverity.P1))
        assert decision.allowed is True
