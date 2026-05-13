"""forge_skills.runtime — SkillRuntime: unified facade for the Skills system.

Combines SkillRegistry + SkillExecutor + IntentDispatcher into one object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from forge_skills.dispatcher import DispatchError, IntentDispatcher
from forge_skills.executor import SkillExecutor
from forge_skills.loader import load_dir, load_skill_md, load_skill_yaml
from forge_skills.registry import SkillNotFoundError, SkillRegistry
from forge_skills.types import (
    SkillBudgetSnapshot,
    SkillDef,
    SkillHandlerFn,
    SkillInvocation,
    SkillResult,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)


class SkillRuntime:
    """Top-level orchestrator for the Forge Skills system.

    Typical usage::

        rt = SkillRuntime()
        rt.register(my_skill, handler=my_handler)
        result = await rt.invoke("my_skill", arguments={"x": 1})

        # Intent-based (requires forge-core[intent]):
        rt.sync_intents()
        result = await rt.dispatch_text("please summarize this article")
    """

    def __init__(
        self,
        *,
        confidence_threshold: float = 0.35,
        intent_model: str = "all-MiniLM-L6-v2",
    ) -> None:
        self._registry = SkillRegistry()
        self._executor = SkillExecutor()
        self._dispatcher = IntentDispatcher(
            self._registry,
            confidence_threshold=confidence_threshold,
            model_name=intent_model,
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, skill: SkillDef, *, handler: SkillHandlerFn | None = None) -> None:
        """Register a skill definition, optionally with a Python handler callable."""
        self._registry.register(skill, handler)

    def unregister(self, name: str) -> bool:
        """Remove a skill by name. Returns True if found."""
        return self._registry.unregister(name)

    def load_md(
        self,
        source: str | Path,
        *,
        handler: SkillHandlerFn | None = None,
    ) -> SkillDef:
        """Parse a SKILL.md document and register the resulting skill."""
        skill = load_skill_md(source)
        self._registry.register(skill, handler)
        return skill

    def load_yaml(
        self,
        source: str | Path,
        *,
        handler: SkillHandlerFn | None = None,
    ) -> SkillDef:
        """Parse a pure YAML skill definition and register it."""
        skill = load_skill_yaml(source)
        self._registry.register(skill, handler)
        return skill

    def load_dir(self, directory: str | Path, *, glob: str = "**/*.md") -> list[SkillDef]:
        """Scan *directory* for SKILL.md files and register all valid skills.

        Returns the list of successfully loaded SkillDefs.
        Files that fail to parse are silently skipped.
        """
        skills = load_dir(directory, glob=glob)
        for skill in skills:
            self._registry.register(skill)
        return skills

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_event_bus(self, bus: Any) -> None:
        """Attach an EventBus for SKILL_CALL event emission."""
        self._executor.set_event_bus(bus)

    def sync_intents(self) -> None:
        """Re-register all skill example phrases with the IntentClassifier.

        Must be called after any registration change before dispatch_text() works.
        Requires forge-core[intent] (sentence-transformers).
        """
        self._dispatcher.sync_intents()

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    async def invoke(
        self,
        skill_name: str,
        *,
        arguments: dict[str, Any] | None = None,
        caller_id: str = "",
        run_id: str = "",
    ) -> SkillResult:
        """Invoke a skill by exact name.

        Returns SkillResult (never raises for handled errors). If the skill is
        not registered, SkillResult.error carries the message.
        """
        try:
            skill = self._registry.get(skill_name)
        except SkillNotFoundError as exc:
            return SkillResult(skill_name=skill_name, error=str(exc))

        handler = self._registry.get_handler(skill_name)
        inv = SkillInvocation(
            skill_name=skill_name,
            arguments=arguments or {},
            caller_id=caller_id,
            run_id=run_id,
        )
        return await self._executor.execute(skill, handler, inv)

    async def dispatch_text(
        self,
        text: str,
        *,
        arguments: dict[str, Any] | None = None,
        caller_id: str = "",
        run_id: str = "",
    ) -> SkillResult:
        """Classify *text* to a skill and invoke it.

        Returns SkillResult(error=...) if no skill matches above the confidence
        threshold — does not raise. Requires forge-core[intent] to be installed.
        """
        try:
            skill = await self._dispatcher.resolve(text)
        except DispatchError as exc:
            return SkillResult(skill_name="unknown", error=str(exc))

        handler = self._registry.get_handler(skill.name)
        inv = SkillInvocation(
            skill_name=skill.name,
            arguments=arguments or {},
            caller_id=caller_id,
            run_id=run_id,
        )
        return await self._executor.execute(skill, handler, inv)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_skills(self) -> list[str]:
        return self._registry.all_names()

    def get_skill(self, name: str) -> SkillDef | None:
        return self._registry.find(name)

    def skills_with_tag(self, tag: str) -> list[SkillDef]:
        return self._registry.skills_with_tag(tag)

    def budget_snapshot(self) -> SkillBudgetSnapshot:
        return self._executor.snapshot()

    def reset_budget(self) -> None:
        """Reset per-run call counters. Call at the start of each new run."""
        self._executor.reset()
