"""forge_skills.registry — SkillRegistry: in-memory skill catalog."""

from __future__ import annotations

import structlog

from forge_skills.types import SkillDef, SkillHandlerFn

logger = structlog.get_logger(__name__)


class SkillNotFoundError(KeyError):
    """Raised when a requested skill is not registered."""


class SkillRegistry:
    """Maps skill names to (SkillDef, optional Python handler).

    Registration is last-write-wins: re-registering a skill by the same name
    replaces the previous definition and its handler.
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillDef] = {}
        self._handlers: dict[str, SkillHandlerFn] = {}

    def register(self, skill: SkillDef, handler: SkillHandlerFn | None = None) -> None:
        """Register *skill*, optionally with a Python callable *handler*."""
        if skill.name in self._skills:
            logger.warning("skills.registry.overwrite", name=skill.name)
        self._skills[skill.name] = skill
        if handler is not None:
            self._handlers[skill.name] = handler
        elif skill.name in self._handlers:
            del self._handlers[skill.name]
        logger.info(
            "skills.registry.registered",
            name=skill.name,
            has_handler=handler is not None,
        )

    def unregister(self, name: str) -> bool:
        """Remove a skill by name. Returns True if found, False if already absent."""
        if name not in self._skills:
            return False
        del self._skills[name]
        self._handlers.pop(name, None)
        logger.info("skills.registry.removed", name=name)
        return True

    def get(self, name: str) -> SkillDef:
        """Return the SkillDef for *name*. Raises SkillNotFoundError if absent."""
        skill = self._skills.get(name)
        if skill is None:
            raise SkillNotFoundError(f"Skill '{name}' is not registered.")
        return skill

    def find(self, name: str) -> SkillDef | None:
        """Return the SkillDef for *name*, or None if not registered."""
        return self._skills.get(name)

    def get_handler(self, name: str) -> SkillHandlerFn | None:
        """Return the callable handler for *name*, or None if not registered."""
        return self._handlers.get(name)

    def all_skills(self) -> list[SkillDef]:
        """Return all registered SkillDefs sorted by name."""
        return sorted(self._skills.values(), key=lambda s: s.name)

    def all_names(self) -> list[str]:
        """Return all registered skill names sorted alphabetically."""
        return sorted(self._skills.keys())

    def skills_with_tag(self, tag: str) -> list[SkillDef]:
        """Return all skills that include *tag* in their tags list."""
        return [s for s in self._skills.values() if tag in s.tags]

    def skill_count(self) -> int:
        return len(self._skills)
