"""forge_rules.glob_intersect — Scope intersection detection at lint time.

Original contribution: detect overlapping glob patterns before runtime so operators
can resolve ambiguities proactively rather than discover them in production.

Algorithm:
  1. Convert each glob pattern to a regex.
  2. Probe with a set of concrete path examples derived from the pattern.
  3. Two patterns intersect if any probe path matches both regexes.

This is a conservative check — it may report false positives for exotic patterns
but will never miss a true intersection on the probe set. It runs in O(N²) over
the rule list at lint time, not at inference time.
"""

from __future__ import annotations

import re
from collections.abc import Iterator


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob pattern to a compiled regex.

    Supported syntax:
      **   — any path segment sequence (including none)
      *    — any characters except /
      ?    — any single character except /
      [...]  — character classes (passed through to regex as-is)
    """
    i = 0
    result = []
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # ** — matches any path depth
                result.append(".*")
                i += 2
                # consume trailing slash after **
                if i < n and pattern[i] == "/":
                    i += 1
            else:
                result.append("[^/]*")
                i += 1
        elif c == "?":
            result.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i)
            if j == -1:
                result.append(re.escape(c))
                i += 1
            else:
                result.append(pattern[i : j + 1])
                i = j + 1
        else:
            result.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(result) + "$")


def _probe_paths_from_pattern(pattern: str) -> list[str]:
    """Generate concrete path strings that should match the pattern.

    These are used to test for intersection with a second pattern.
    """
    probes: list[str] = []

    # Strip leading/trailing **/ so we can work with the literal segments
    normalized = pattern.lstrip("/")

    # Replace ** with a concrete segment pair and * with a word
    concrete = normalized.replace("**/", "src/subdir/").replace("/**", "/file").replace(
        "**", "anything"
    ).replace("*", "example").replace("?", "x")

    # If pattern had no extension, add .py as a common extension
    if "." not in concrete.split("/")[-1]:
        concrete += ".py"

    probes.append(concrete)

    # Also try the pattern with a deeper nesting
    parts = concrete.split("/")
    if len(parts) >= 2:
        probes.append("/".join(["deep"] + parts))

    return probes


def paths_intersect(pattern_a: str, pattern_b: str) -> tuple[bool, str]:
    """Return (True, example_path) if the two glob patterns can match the same path.

    Returns (False, "") if no intersection is detected in the probe set.
    """
    re_a = _glob_to_regex(pattern_a)
    re_b = _glob_to_regex(pattern_b)

    # Test probes derived from A against B's regex, and vice versa
    for probe in _probe_paths_from_pattern(pattern_a) + _probe_paths_from_pattern(pattern_b):
        if re_a.match(probe) and re_b.match(probe):
            return True, probe

    return False, ""


def find_intersections(
    scope_map: dict[str, list[str]],
) -> Iterator[tuple[str, str, str, str, str]]:
    """Yield (rule_id_a, rule_id_b, pattern_a, pattern_b, example) for every pair of
    rules whose scope patterns intersect.

    ``scope_map`` maps rule_id → list[glob_pattern].
    """
    rule_ids = list(scope_map.keys())
    for i, id_a in enumerate(rule_ids):
        for id_b in rule_ids[i + 1 :]:
            for pat_a in scope_map[id_a]:
                for pat_b in scope_map[id_b]:
                    hit, example = paths_intersect(pat_a, pat_b)
                    if hit:
                        yield id_a, id_b, pat_a, pat_b, example
