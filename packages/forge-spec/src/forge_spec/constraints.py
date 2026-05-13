"""forge_spec.constraints — spec-gated mutation enforcement for EvolutionLoop."""

from __future__ import annotations

from forge_core.types import MutationKind

from forge_spec.types import SpecConstraint, SpecDef

try:
    from forge_core.types import Mutation
except ImportError:  # pragma: no cover
    Mutation = None  # type: ignore[assignment,misc]


class ConstraintViolationError(Exception):
    """Raised when a mutation violates one or more spec constraints."""

    def __init__(self, violations: list[SpecConstraint]) -> None:
        names = ", ".join(f"{c.id!r}" for c in violations)
        super().__init__(f"Mutation blocked by spec constraints: {names}")
        self.violations = violations


class SpecConstraintGuard:
    """Guard that checks EvolutionLoop mutations against a SpecDef's constraints.

    Usage::

        guard = SpecConstraintGuard(spec)
        guard.check(mutation)   # raises ConstraintViolationError if blocked
    """

    def __init__(self, spec: SpecDef) -> None:
        self._spec = spec

    def check(self, mutation_kind: MutationKind) -> None:
        """Raise ConstraintViolationError if *mutation_kind* is blocked by any constraint."""
        blocked = self._violations_for_kind(mutation_kind)
        if blocked:
            raise ConstraintViolationError(blocked)

    def is_allowed(self, mutation_kind: MutationKind) -> bool:
        """Return True if *mutation_kind* is not blocked by any constraint."""
        return not self._violations_for_kind(mutation_kind)

    def blocked_kinds(self) -> set[MutationKind]:
        """Return every MutationKind that at least one constraint blocks."""
        blocked: set[MutationKind] = set()
        for constraint in self._spec.constraints:
            if constraint.mutation_kinds_blocked:
                blocked.update(constraint.mutation_kinds_blocked)
            else:
                # Empty list → constraint blocks ALL mutations
                blocked.update(MutationKind)
        return blocked

    def allowed_kinds(self) -> set[MutationKind]:
        """Return every MutationKind not blocked by any constraint."""
        return set(MutationKind) - self.blocked_kinds()

    def violations_for(self, mutation_kind: MutationKind) -> list[SpecConstraint]:
        """Return constraints that block *mutation_kind*."""
        return self._violations_for_kind(mutation_kind)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _violations_for_kind(self, mutation_kind: MutationKind) -> list[SpecConstraint]:
        violated: list[SpecConstraint] = []
        for constraint in self._spec.constraints:
            if not constraint.mutation_kinds_blocked:
                # Empty → blocks all
                violated.append(constraint)
            elif mutation_kind in constraint.mutation_kinds_blocked:
                violated.append(constraint)
        return violated
