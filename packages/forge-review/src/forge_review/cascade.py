"""forge_review.cascade — CascadingReviewer: heuristics first, LLM only when clean."""

from __future__ import annotations

from typing import Any

from forge_review.agents import run_parallel
from forge_review.types import Finding, ReviewConfig, ReviewContext


class CascadingReviewer:
    """Two-stage review pipeline that short-circuits LLM reviewers when heuristics block.

    Stage 1 — heuristic reviewers run in parallel (cheap, fast, no model calls).
    Stage 2 — LLM reviewers run only if stage 1 produced no blocking findings
               AND ``config.cascade_on_blocking`` is True (the default).

    This keeps latency low for the common case (heuristics catch the issue)
    and reserves LLM budget for runs that are worth deeper analysis.
    """

    def __init__(
        self,
        heuristic_reviewers: list[Any],
        llm_reviewers: list[Any] | None = None,
        *,
        config: ReviewConfig | None = None,
    ) -> None:
        self._heuristic = heuristic_reviewers
        self._llm = llm_reviewers or []
        self._config = config or ReviewConfig()

    async def review(self, context: ReviewContext) -> list[Finding]:
        findings = await run_parallel(self._heuristic, context)

        if self._llm:
            should_skip = self._config.cascade_on_blocking and self._has_blocking(findings)
            if not should_skip:
                llm_findings = await run_parallel(self._llm, context)
                findings.extend(llm_findings)

        return findings

    # ------------------------------------------------------------------

    def _has_blocking(self, findings: list[Finding]) -> bool:
        block_sev = set(self._config.block_on)
        return any(f.severity in block_sev for f in findings)
