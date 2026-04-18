"""Minimal stand-in for the ``anthropic`` SDK.

``forge_observe.llm_clients.instrument_llm_clients`` patches
``anthropic.resources.messages.Messages.create`` (and the async
variant) when the module is present in ``sys.modules``. Installing
this fake there lets the E2E demo test exercise the full generic-
adapter instrumentation path — cost, tokens, topology — without
pulling in the real SDK or requiring credentials.

The classes match only the shape Forge actually reads:

* ``Messages.create(model=..., ...)`` returns an object with
  ``.content`` (list with a single ``TextBlock``) and
  ``.usage.input_tokens`` / ``.usage.output_tokens``.
* ``Anthropic`` exposes ``.messages`` as a ``Messages`` instance.

No streaming, no tool use, no retries — just enough for the
interceptor's extractor to pull numbers out.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import ClassVar


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _MessageResponse:
    content: list[_TextBlock]
    usage: _Usage
    model: str
    id: str = "msg_fake"
    role: str = "assistant"
    stop_reason: str = "end_turn"
    type: str = "message"


class Messages:
    """Sync messages resource — what ``instrument_llm_clients`` patches."""

    # Scripted responses: a class-level FIFO. Tests append; calls consume.
    _script: ClassVar[list[_MessageResponse]] = []

    @classmethod
    def queue(cls, text: str, *, input_tokens: int = 100, output_tokens: int = 150) -> None:
        cls._script.append(
            _MessageResponse(
                content=[_TextBlock(text=text)],
                usage=_Usage(input_tokens=input_tokens, output_tokens=output_tokens),
                model="mock-model",
            )
        )

    @classmethod
    def reset(cls) -> None:
        cls._script.clear()

    def create(self, *, model: str, messages: list, max_tokens: int = 1024, **_: object):
        # Pull the next scripted response. If empty, synthesize one — the
        # interceptor is what matters here; the test will assert on the
        # resulting RunResult, not on the response shape.
        if type(self)._script:
            resp = type(self)._script.pop(0)
        else:
            resp = _MessageResponse(
                content=[_TextBlock(text="fallback")],
                usage=_Usage(input_tokens=50, output_tokens=75),
                model=model or "mock-model",
            )
        # Reflect caller-supplied model so cost lookup uses it.
        resp.model = model or resp.model
        return resp


class AsyncMessages:
    """Async variant — here for symmetry; unused in this E2E test."""

    async def create(self, *, model: str, messages: list, max_tokens: int = 1024, **_: object):
        return Messages().create(model=model, messages=messages, max_tokens=max_tokens)


class Anthropic:
    def __init__(self, *_: object, **__: object) -> None:
        self.messages = Messages()


class AsyncAnthropic:
    def __init__(self, *_: object, **__: object) -> None:
        self.messages = AsyncMessages()


def install() -> None:
    """Wire this module tree into ``sys.modules`` as ``anthropic``.

    Idempotent: re-installing clears the script but keeps the existing
    module objects so any already-captured ``Messages`` reference stays
    valid.
    """
    # Top-level module
    anthropic_mod = sys.modules.get("anthropic")
    if anthropic_mod is None:
        anthropic_mod = types.ModuleType("anthropic")
        sys.modules["anthropic"] = anthropic_mod
    anthropic_mod.Anthropic = Anthropic  # type: ignore[attr-defined]
    anthropic_mod.AsyncAnthropic = AsyncAnthropic  # type: ignore[attr-defined]

    # resources subpackage
    resources_mod = sys.modules.get("anthropic.resources")
    if resources_mod is None:
        resources_mod = types.ModuleType("anthropic.resources")
        sys.modules["anthropic.resources"] = resources_mod
    anthropic_mod.resources = resources_mod  # type: ignore[attr-defined]

    # resources.messages submodule — this is the one the descriptor
    # imports from.
    messages_mod = sys.modules.get("anthropic.resources.messages")
    if messages_mod is None:
        messages_mod = types.ModuleType("anthropic.resources.messages")
        sys.modules["anthropic.resources.messages"] = messages_mod
    messages_mod.Messages = Messages  # type: ignore[attr-defined]
    messages_mod.AsyncMessages = AsyncMessages  # type: ignore[attr-defined]
    resources_mod.messages = messages_mod  # type: ignore[attr-defined]

    Messages.reset()


def uninstall() -> None:
    """Remove the fake from ``sys.modules`` so other tests see a clean slate."""
    for name in ("anthropic.resources.messages", "anthropic.resources", "anthropic"):
        sys.modules.pop(name, None)
