"""forge_review.policy — PolicyGate: evaluates findings and produces allow/deny decisions."""

from __future__ import annotations

from forge_review.types import (
    Finding,
    FindingSeverity,
    PolicyDecision,
    ReviewConfig,
    ReviewResult,
)


class PolicyGate:
    """Evaluates a ReviewResult and returns a PolicyDecision.

    By default blocks on P0 and P1. Configure via ReviewConfig.block_on.
    """

    def __init__(self, config: ReviewConfig | None = None) -> None:
        self._config = config or ReviewConfig()

    def evaluate(self, review_result: ReviewResult) -> PolicyDecision:
        block_severities = set(self._config.block_on)
        min_confidence = self._config.min_confidence

        blocked = [
            f for f in review_result.findings
            if f.severity in block_severities and f.confidence >= min_confidence
        ]

        if blocked:
            titles = ", ".join(f"'{f.title}'" for f in blocked[:3])
            suffix = f" (+{len(blocked) - 3} more)" if len(blocked) > 3 else ""
            return PolicyDecision(
                allowed=False,
                blocked_by=blocked,
                review_result_id=review_result.id,
                reason=f"{len(blocked)} blocking finding(s): {titles}{suffix}",
            )

        return PolicyDecision(
            allowed=True,
            review_result_id=review_result.id,
        )

    def allows(self, review_result: ReviewResult) -> bool:
        """Convenience wrapper — True when the decision is allowed."""
        return self.evaluate(review_result).allowed
