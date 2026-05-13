"""Tests for forge_review.cascade — CascadingReviewer."""

from __future__ import annotations

import asyncio

from forge_review.cascade import CascadingReviewer
from forge_review.types import Finding, FindingSeverity, HookKind, ReviewConfig, ReviewContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> ReviewContext:
    return ReviewContext(hook=HookKind.PRE_MERGE)


class _FixedReviewer:
    def __init__(self, name: str, findings: list[Finding]) -> None:
        self.name = name
        self._findings = findings

    async def review(self, context: ReviewContext) -> list[Finding]:
        return list(self._findings)


def _finding(severity: FindingSeverity, title: str = "t") -> Finding:
    return Finding(severity=severity, title=title, message="m")


# ---------------------------------------------------------------------------
# No LLM reviewers
# ---------------------------------------------------------------------------


class TestCascadingNoLLM:
    def test_empty_heuristics_returns_empty(self):
        cr = CascadingReviewer(heuristic_reviewers=[])
        findings = asyncio.run(cr.review(_ctx()))
        assert findings == []

    def test_heuristic_findings_returned(self):
        cr = CascadingReviewer(
            heuristic_reviewers=[_FixedReviewer("h1", [_finding(FindingSeverity.P2)])],
        )
        findings = asyncio.run(cr.review(_ctx()))
        assert len(findings) == 1
        assert findings[0].severity == FindingSeverity.P2

    def test_multiple_heuristics_combined(self):
        cr = CascadingReviewer(
            heuristic_reviewers=[
                _FixedReviewer("h1", [_finding(FindingSeverity.P2)]),
                _FixedReviewer("h2", [_finding(FindingSeverity.P3)]),
            ],
        )
        findings = asyncio.run(cr.review(_ctx()))
        assert len(findings) == 2


# ---------------------------------------------------------------------------
# Cascading with LLM reviewers
# ---------------------------------------------------------------------------


class TestCascadingWithLLM:
    def test_llm_runs_when_heuristics_clean(self):
        cr = CascadingReviewer(
            heuristic_reviewers=[_FixedReviewer("h1", [])],
            llm_reviewers=[_FixedReviewer("llm1", [_finding(FindingSeverity.P3, "llm-info")])],
        )
        findings = asyncio.run(cr.review(_ctx()))
        assert len(findings) == 1
        assert findings[0].title == "llm-info"

    def test_llm_skipped_when_heuristics_block_p0(self):
        cr = CascadingReviewer(
            heuristic_reviewers=[_FixedReviewer("h1", [_finding(FindingSeverity.P0)])],
            llm_reviewers=[_FixedReviewer("llm1", [_finding(FindingSeverity.P3, "llm-info")])],
        )
        findings = asyncio.run(cr.review(_ctx()))
        assert len(findings) == 1
        assert findings[0].severity == FindingSeverity.P0

    def test_llm_skipped_when_heuristics_block_p1(self):
        cr = CascadingReviewer(
            heuristic_reviewers=[_FixedReviewer("h1", [_finding(FindingSeverity.P1)])],
            llm_reviewers=[_FixedReviewer("llm1", [_finding(FindingSeverity.P3, "llm")])],
        )
        findings = asyncio.run(cr.review(_ctx()))
        assert len(findings) == 1

    def test_llm_runs_when_heuristic_finds_only_p2(self):
        cr = CascadingReviewer(
            heuristic_reviewers=[_FixedReviewer("h1", [_finding(FindingSeverity.P2)])],
            llm_reviewers=[_FixedReviewer("llm1", [_finding(FindingSeverity.P3, "llm")])],
        )
        findings = asyncio.run(cr.review(_ctx()))
        assert len(findings) == 2

    def test_cascade_disabled_llm_always_runs(self):
        config = ReviewConfig(cascade_on_blocking=False)
        cr = CascadingReviewer(
            heuristic_reviewers=[_FixedReviewer("h1", [_finding(FindingSeverity.P0)])],
            llm_reviewers=[_FixedReviewer("llm1", [_finding(FindingSeverity.P3, "llm")])],
            config=config,
        )
        findings = asyncio.run(cr.review(_ctx()))
        assert len(findings) == 2

    def test_combined_findings_order(self):
        cr = CascadingReviewer(
            heuristic_reviewers=[_FixedReviewer("h1", [_finding(FindingSeverity.P2, "heur")])],
            llm_reviewers=[_FixedReviewer("llm1", [_finding(FindingSeverity.P3, "llm")])],
        )
        findings = asyncio.run(cr.review(_ctx()))
        assert findings[0].title == "heur"
        assert findings[1].title == "llm"

    def test_no_llm_reviewers_arg(self):
        cr = CascadingReviewer(
            heuristic_reviewers=[_FixedReviewer("h1", [])],
            llm_reviewers=None,
        )
        findings = asyncio.run(cr.review(_ctx()))
        assert findings == []
