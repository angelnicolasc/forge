"""Shared fixtures for forge-skills tests."""

from __future__ import annotations

import textwrap

import pytest

from forge_skills.types import SkillDef, SkillParam

VALID_SKILL_MD = textwrap.dedent("""
    ---
    name: summarize
    description: Summarize a document
    examples:
      - "summarize this document"
      - "give me the key points"
    parameters:
      - name: text
        type: string
        description: Text to summarize
        required: true
      - name: max_words
        type: int
        required: false
        default: 200
    timeout_seconds: 30.0
    max_calls: 5
    tags:
      - nlp
      - text
    ---

    Summarize the following text in at most {max_words} words:

    {text}
""").lstrip()


@pytest.fixture()
def basic_skill() -> SkillDef:
    return SkillDef(
        name="greet",
        description="Greet a user",
        examples=["say hello", "greet user"],
        parameters=[SkillParam(name="user", description="User name")],
        tags=["social"],
    )


@pytest.fixture()
def no_param_skill() -> SkillDef:
    return SkillDef(name="ping", description="Simple ping skill")


async def _echo_handler(args: dict) -> str:
    return f"echo:{args.get('text', '')}"


async def _greet_handler(args: dict) -> str:
    return f"Hello, {args.get('user', 'World')}!"
