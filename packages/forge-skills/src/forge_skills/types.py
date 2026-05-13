"""forge_skills.types — core data models for the Skills Runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ParamType(StrEnum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    LIST = "list"
    DICT = "dict"


class SkillParam(BaseModel):
    """One parameter in a skill's input schema."""

    name: str
    type: ParamType = ParamType.STRING
    description: str = ""
    required: bool = True
    default: Any = None


class SkillDef(BaseModel):
    """A skill definition — parsed from SKILL.md or YAML, or constructed inline."""

    name: str
    description: str = ""
    examples: list[str] = Field(default_factory=list)
    parameters: list[SkillParam] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    timeout_seconds: float = 30.0
    max_calls: int = 20
    tags: list[str] = Field(default_factory=list)
    source: str = ""
    body: str = ""

    @model_validator(mode="after")
    def _name_valid(self) -> "SkillDef":
        if " " in self.name:
            raise ValueError(
                f"Skill name must not contain spaces: {self.name!r}. "
                "Use hyphens or underscores."
            )
        return self


class SkillInvocation(BaseModel):
    """A request to invoke a skill."""

    skill_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    caller_id: str = ""
    run_id: str = ""
    parent_run_id: str = ""


class SkillResult(BaseModel):
    """The outcome of a single skill invocation."""

    skill_name: str
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    tokens_used: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


class SkillBudgetSnapshot(BaseModel):
    """Budget accounting snapshot for the current SkillRuntime session."""

    calls_by_skill: dict[str, int] = Field(default_factory=dict)
    total_calls: int = 0


# Callable type for skill handlers.
# Signature: async def handler(arguments: dict[str, Any]) -> Any
SkillHandlerFn = Callable[[dict[str, Any]], Awaitable[Any]]
