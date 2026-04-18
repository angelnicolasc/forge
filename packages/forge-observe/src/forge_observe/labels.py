"""Metric label cardinality control (fase ε.2).

Prometheus-style metric backends quietly melt under high-cardinality
labels: a unique ``agent_id`` per run means one time series per run,
which at 10k runs/day adds up to cardinality explosions that blow
through every tier of a metrics vendor.

:class:`LabelSanitizer` is the hot-path filter between Forge's event
bus and the metrics exporter. It enforces:

1. **Allowlist**. Only label keys the operator explicitly whitelists
   reach the exporter. Anything else is stripped.
2. **Unique-value cap per label**. Once a label has emitted N distinct
   values within the process lifetime, further values are bucketed
   into a stable hash prefix — preserving observability (you can still
   distinguish series) while keeping the total bounded.
3. **Value normalization**. Empty strings become a literal ``<empty>``;
   None is treated as absent. These are consistent across exporters
   and make dashboards readable.

Design notes worth defending
----------------------------

*   State is process-local. A multi-process deployment will independently
    converge on its own allowlist/hash prefixes; that's fine because
    Prometheus stitches by label tuples, and two processes producing the
    same hash for the same input stays coherent.
*   The hash is deterministic — the first 8 hex chars of SHA-1 over the
    raw value. Collisions are astronomically rare at the volumes we
    care about and don't cause data loss (they just merge two series),
    which is the failure mode we want vs. unbounded growth.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = structlog.get_logger()


DEFAULT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "run_id",
        "adapter",
        "agent_id",
        "model",
        "tool_name",
        "status",
        "kind",
    }
)


@dataclass
class LabelSanitizerStats:
    """Observability into the sanitizer itself — a dashboard for the dashboard."""

    keys_stripped: int = 0
    values_hashed: int = 0
    # Map of label_key → set of distinct raw values seen.
    seen_values: dict[str, set[str]] = field(default_factory=dict)


class LabelSanitizer:
    """Enforce allowlist + per-key cardinality caps on metric labels."""

    def __init__(
        self,
        allowlist: Iterable[str] | None = None,
        *,
        max_unique_per_key: int = 100,
    ) -> None:
        self._allowlist = frozenset(allowlist) if allowlist is not None else DEFAULT_ALLOWLIST
        self._max_unique = max(1, int(max_unique_per_key))
        self._seen: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self._stats = LabelSanitizerStats(seen_values=self._seen)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def stats(self) -> LabelSanitizerStats:
        return self._stats

    def sanitize(self, labels: dict[str, str | None]) -> dict[str, str]:
        """Return a new dict containing only allowed, cardinality-capped labels.

        The input is never mutated — exporters frequently reuse the
        source dict, and surprise mutation has burned more than one
        production debugging session.
        """
        cleaned: dict[str, str] = {}
        with self._lock:
            for key, raw in labels.items():
                if key not in self._allowlist:
                    self._stats.keys_stripped += 1
                    continue
                value = self._normalize(raw)
                cleaned[key] = self._cap_value(key, value)
        return cleaned

    def update_allowlist(self, keys: Iterable[str]) -> None:
        """Replace the current allowlist. Used by config reload."""
        with self._lock:
            self._allowlist = frozenset(keys)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(raw: str | None) -> str:
        if raw is None:
            return "<none>"
        s = str(raw).strip()
        if not s:
            return "<empty>"
        return s

    def _cap_value(self, key: str, value: str) -> str:
        bucket = self._seen.setdefault(key, set())
        if value in bucket:
            return value
        if len(bucket) < self._max_unique:
            bucket.add(value)
            return value
        # Over the cap — bucket into a deterministic hash prefix. The
        # resulting "bucket_" value is a stable, bounded label.
        self._stats.values_hashed += 1
        h = hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:8]
        return f"bucket_{h}"
