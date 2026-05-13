"""Tests for forge_rules.glob_intersect — scope intersection detection."""

from __future__ import annotations

from forge_rules.glob_intersect import (
    _glob_to_regex,
    find_intersections,
    paths_intersect,
)


class TestGlobToRegex:
    def test_literal_path(self) -> None:
        r = _glob_to_regex("src/main.py")
        assert r.match("src/main.py")
        assert not r.match("src/other.py")

    def test_single_star_no_slash(self) -> None:
        r = _glob_to_regex("src/*.py")
        assert r.match("src/main.py")
        assert r.match("src/foo.py")
        assert not r.match("src/sub/main.py")

    def test_double_star_matches_any_depth(self) -> None:
        r = _glob_to_regex("**/*.py")
        assert r.match("main.py")
        assert r.match("src/main.py")
        assert r.match("src/core/main.py")
        assert r.match("a/b/c/d.py")

    def test_question_mark(self) -> None:
        r = _glob_to_regex("src/?.py")
        assert r.match("src/a.py")
        assert not r.match("src/ab.py")
        assert not r.match("src/a/b.py")

    def test_double_star_prefix(self) -> None:
        r = _glob_to_regex("**/core/*.py")
        assert r.match("core/main.py")
        assert r.match("src/core/main.py")
        assert r.match("a/b/core/x.py")


class TestPathsIntersect:
    def test_same_pattern_intersects(self) -> None:
        hit, example = paths_intersect("**/*.py", "**/*.py")
        assert hit
        assert example.endswith(".py") or ".py" in example

    def test_overlapping_glob_patterns(self) -> None:
        # src/**/*.py and src/core/*.py both match src/core/foo.py
        hit, _ = paths_intersect("src/**/*.py", "src/core/*.py")
        assert hit

    def test_non_overlapping_extensions(self) -> None:
        # .py and .js can't match the same path
        hit, _ = paths_intersect("**/*.py", "**/*.js")
        assert not hit

    def test_different_directories_no_overlap(self) -> None:
        # src/ and tests/ are disjoint top-level dirs
        hit, _ = paths_intersect("src/main.py", "tests/main.py")
        assert not hit

    def test_broad_and_narrow_intersect(self) -> None:
        hit, _ = paths_intersect("**/*.py", "src/api/views.py")
        assert hit


class TestFindIntersections:
    def test_no_intersections_empty(self) -> None:
        scope_map = {
            "rule-a": ["**/*.py"],
            "rule-b": ["**/*.js"],
        }
        results = list(find_intersections(scope_map))
        assert results == []

    def test_detects_intersection(self) -> None:
        scope_map = {
            "rule-a": ["src/**/*.py"],
            "rule-b": ["src/core/*.py"],
        }
        results = list(find_intersections(scope_map))
        assert len(results) >= 1
        ids = {(r[0], r[1]) for r in results}
        assert ("rule-a", "rule-b") in ids

    def test_no_self_intersection(self) -> None:
        scope_map = {"rule-a": ["**/*.py"]}
        results = list(find_intersections(scope_map))
        assert results == []

    def test_example_path_is_string(self) -> None:
        scope_map = {
            "rule-x": ["**/*.py"],
            "rule-y": ["src/**/*.py"],
        }
        for _, _, _, _, example in find_intersections(scope_map):
            assert isinstance(example, str)
            assert len(example) > 0
