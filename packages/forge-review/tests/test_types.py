"""Tests for forge_review.types."""

from __future__ import annotations

from forge_review.types import (
    Finding,
    FindingSeverity,
    HookKind,
    PolicyDecision,
    ReviewConfig,
    ReviewContext,
    ReviewResult,
)


# ---------------------------------------------------------------------------
# FindingSeverity
# ---------------------------------------------------------------------------


class TestFindingSeverity:
    def test_values(self):
        assert FindingSeverity.P0 == "p0"
        assert FindingSeverity.P1 == "p1"
        assert FindingSeverity.P2 == "p2"
        assert FindingSeverity.P3 == "p3"

    def test_ordering_by_string(self):
        sevs = list(FindingSeverity)
        assert sevs[0] == FindingSeverity.P0


# ---------------------------------------------------------------------------
# HookKind
# ---------------------------------------------------------------------------


class TestHookKind:
    def test_values(self):
        assert HookKind.ON_PLAN == "on_plan"
        assert HookKind.PRE_APPLY == "pre_apply"
        assert HookKind.PRE_STOP == "pre_stop"
        assert HookKind.PRE_MERGE == "pre_merge"


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


class TestFinding:
    def test_defaults(self):
        f = Finding(severity=FindingSeverity.P2, title="t", message="m")
        assert f.source == ""
        assert f.location == ""
        assert f.confidence == 1.0
        assert f.tags == []
        assert len(f.id) == 12

    def test_unique_ids(self):
        f1 = Finding(severity=FindingSeverity.P0, title="t", message="m")
        f2 = Finding(severity=FindingSeverity.P0, title="t", message="m")
        assert f1.id != f2.id

    def test_full_fields(self):
        f = Finding(
            severity=FindingSeverity.P1,
            title="Cost exceeded",
            message="cost 11.00 >= ceiling 10.00",
            source="cost_ceiling",
            location="run_result.cost",
            confidence=0.95,
            tags=["cost"],
        )
        assert f.severity == FindingSeverity.P1
        assert f.confidence == 0.95
        assert "cost" in f.tags

    def test_roundtrip_json(self):
        import json
        f = Finding(severity=FindingSeverity.P3, title="info", message="all good")
        data = json.loads(f.model_dump_json())
        f2 = Finding.model_validate(data)
        assert f2.id == f.id
        assert f2.severity == f.severity


# ---------------------------------------------------------------------------
# ReviewResult
# ---------------------------------------------------------------------------


def _make_result(*severities: FindingSeverity, hook: HookKind = HookKind.PRE_MERGE) -> ReviewResult:
    findings = [
        Finding(severity=s, title=f"finding {i}", message="m")
        for i, s in enumerate(severities)
    ]
    return ReviewResult(hook=hook, findings=findings)


class TestReviewResult:
    def test_empty_result(self):
        r = _make_result()
        assert r.findings == []
        assert not r.has_blocking
        assert r.blocking_findings == []

    def test_summary_counts(self):
        r = _make_result(FindingSeverity.P0, FindingSeverity.P1, FindingSeverity.P2, FindingSeverity.P3)
        s = r.summary
        assert s["p0"] == 1
        assert s["p1"] == 1
        assert s["p2"] == 1
        assert s["p3"] == 1

    def test_summary_all_zero_when_empty(self):
        r = _make_result()
        assert all(v == 0 for v in r.summary.values())

    def test_has_blocking_p0(self):
        r = _make_result(FindingSeverity.P0)
        assert r.has_blocking

    def test_has_blocking_p1(self):
        r = _make_result(FindingSeverity.P1)
        assert r.has_blocking

    def test_no_blocking_p2_p3(self):
        r = _make_result(FindingSeverity.P2, FindingSeverity.P3)
        assert not r.has_blocking

    def test_blocking_findings_filters_correctly(self):
        r = _make_result(FindingSeverity.P0, FindingSeverity.P2, FindingSeverity.P1)
        assert len(r.blocking_findings) == 2
        for f in r.blocking_findings:
            assert f.severity in (FindingSeverity.P0, FindingSeverity.P1)

    def test_reviewers_run_list(self):
        r = ReviewResult(hook=HookKind.ON_PLAN, reviewers_run=["error_rate", "cost_ceiling"])
        assert "error_rate" in r.reviewers_run

    def test_unique_ids(self):
        r1 = _make_result()
        r2 = _make_result()
        assert r1.id != r2.id


# ---------------------------------------------------------------------------
# PolicyDecision
# ---------------------------------------------------------------------------


class TestPolicyDecision:
    def test_allowed(self):
        d = PolicyDecision(allowed=True)
        assert d.allowed
        assert d.blocked_by == []
        assert d.reason == ""

    def test_blocked(self):
        f = Finding(severity=FindingSeverity.P0, title="t", message="m")
        d = PolicyDecision(allowed=False, blocked_by=[f], reason="critical finding")
        assert not d.allowed
        assert len(d.blocked_by) == 1


# ---------------------------------------------------------------------------
# ReviewConfig
# ---------------------------------------------------------------------------


class TestReviewConfig:
    def test_defaults(self):
        c = ReviewConfig()
        assert FindingSeverity.P0 in c.block_on
        assert FindingSeverity.P1 in c.block_on
        assert FindingSeverity.P2 not in c.block_on
        assert c.min_confidence == 0.0
        assert c.cascade_on_blocking is True

    def test_custom_block_on(self):
        c = ReviewConfig(block_on=[FindingSeverity.P0])
        assert FindingSeverity.P1 not in c.block_on

    def test_custom_min_confidence(self):
        c = ReviewConfig(min_confidence=0.8)
        assert c.min_confidence == 0.8


# ---------------------------------------------------------------------------
# ReviewContext
# ---------------------------------------------------------------------------


class TestReviewContext:
    def test_dataclass(self):
        ctx = ReviewContext(hook=HookKind.ON_PLAN, run_id="r1", data={"plan": {}})
        assert ctx.hook == HookKind.ON_PLAN
        assert ctx.run_id == "r1"
        assert "plan" in ctx.data

    def test_defaults(self):
        ctx = ReviewContext(hook=HookKind.PRE_STOP)
        assert ctx.run_id == ""
        assert ctx.data == {}
