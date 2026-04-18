"""Thin LLM client abstraction for the evolution loop.

Mutators that need to call an LLM (notably :class:`PromptRewriteMutator`
for structured prompt rewrites) go through :class:`LLMClient` rather
than importing the Anthropic SDK directly. This gives us three things:

1. **Testability.** Tests inject a :class:`FakeLLMClient` that returns
   canned structured responses without hitting the network.
2. **Vendor neutrality.** The default implementation uses Anthropic,
   but swapping in an OpenAI/Gemini-backed client is a one-line change
   in the caller. The protocol is intentionally tiny.
3. **Structured outputs only.** We expose exactly one method —
   :meth:`LLMClient.structured_call` — that takes a Pydantic model as a
   schema and returns a validated instance. No "just give me a string"
   escape hatch. The evolution loop refuses to trust free-form text.

The default Anthropic implementation uses the Messages API with a
single ``tool`` entry matching the requested schema, then forces the
model to invoke it via ``tool_choice={"type": "tool", "name": ...}``.
That's the canonical pattern for structured output in the Anthropic SDK
and behaves identically in mock form (no network calls) when the fake
client is injected.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar, runtime_checkable

import structlog
from pydantic import BaseModel, ValidationError

logger = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class LLMClient(Protocol):
    """Structured-output LLM call contract.

    Implementations MUST validate the model's response against ``schema``
    and raise :class:`LLMStructuredCallError` on parse/validation failure.
    They MUST NOT return raw text.
    """

    async def structured_call(
        self,
        *,
        model: str,
        schema: type[T],
        system: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> T:
        """Invoke ``model`` and return a validated instance of ``schema``."""
        ...


class LLMStructuredCallError(RuntimeError):
    """Raised when the model returned something that can't be parsed as ``schema``.

    Callers (mutators) should catch this and treat the mutation as
    inapplicable for this step — not as a fatal error. The evolution
    loop's journal records the skip with the exception message.
    """


# ---------------------------------------------------------------------------
# Anthropic-backed default implementation
# ---------------------------------------------------------------------------


class AnthropicLLMClient:
    """Default :class:`LLMClient` backed by ``anthropic.AsyncAnthropic``.

    Lazy-imports the SDK at call time so installing ``forge-core`` without
    Anthropic remains possible (the evolution loop simply degrades to
    suggest-only mode if no client is configured).
    """

    def __init__(self, api_key: str | None = None, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise LLMStructuredCallError(
                "anthropic package not installed — install with `pip install anthropic` "
                "or inject a custom LLMClient into the mutator."
            ) from exc
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def structured_call(
        self,
        *,
        model: str,
        schema: type[T],
        system: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> T:
        """Use Anthropic tool-use to force a structured response.

        The schema is rendered as a tool; the model is required to call
        it via ``tool_choice``. We parse the resulting ``input`` dict
        through Pydantic to validate structure and return the instance.
        """
        client = self._get_client()
        tool_name = _tool_name_for(schema)
        tool_schema = _pydantic_to_tool_schema(schema)

        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[
                    {
                        "name": tool_name,
                        "description": (schema.__doc__ or "Structured response.").strip(),
                        "input_schema": tool_schema,
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except Exception as exc:
            raise LLMStructuredCallError(f"Anthropic call failed: {exc}") from exc

        payload = _extract_tool_input(response, tool_name)
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise LLMStructuredCallError(
                f"Model output did not match schema {schema.__name__}: {exc}"
            ) from exc


def _tool_name_for(schema: type[BaseModel]) -> str:
    """Derive a safe tool name from a Pydantic model class."""
    # Anthropic tool names must match ^[a-zA-Z0-9_-]{1,64}$
    return schema.__name__[:64]


def _pydantic_to_tool_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Render a Pydantic model as a JSON Schema acceptable to Anthropic tools."""
    js = schema.model_json_schema()
    # Anthropic rejects `$defs`-only schemas at the top level; inline by
    # wrapping if needed. The common case (flat models) already works.
    return js


def _extract_tool_input(response: Any, expected_tool_name: str) -> dict[str, Any]:
    """Pull the tool-use block's ``input`` dict out of an Anthropic response."""
    content = getattr(response, "content", None) or []
    for block in content:
        # Anthropic SDK returns typed blocks; ``type`` is "tool_use" for ours.
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "tool_use":
            continue
        name = getattr(block, "name", None) or (
            block.get("name") if isinstance(block, dict) else None
        )
        if name != expected_tool_name:
            continue
        payload = getattr(block, "input", None)
        if payload is None and isinstance(block, dict):
            payload = block.get("input")
        if isinstance(payload, str):
            # Some transports serialize tool input as JSON string.
            payload = json.loads(payload)
        if isinstance(payload, dict):
            return payload
    raise LLMStructuredCallError(
        f"No tool_use block named '{expected_tool_name}' found in response."
    )


# ---------------------------------------------------------------------------
# Fake client for tests
# ---------------------------------------------------------------------------


class FakeLLMClient:
    """Deterministic :class:`LLMClient` for tests.

    Construct with a dict (or callable) mapping ``schema`` instances to
    canned payloads. Every :meth:`structured_call` pulls the next queued
    response and validates it against the requested schema.

    Usage::

        fake = FakeLLMClient()
        fake.queue(PromptRewriteResponse, {"rewritten_prompt": "..."})
        mutator = PromptRewriteMutator(llm_client=fake)
    """

    def __init__(self) -> None:
        self._queue: list[tuple[type[BaseModel], dict[str, Any]]] = []
        self.calls: list[dict[str, Any]] = []

    def queue(self, schema: type[T], payload: dict[str, Any]) -> None:
        """Enqueue a canned response for the next call whose schema matches."""
        self._queue.append((schema, payload))

    async def structured_call(
        self,
        *,
        model: str,
        schema: type[T],
        system: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> T:
        self.calls.append(
            {
                "model": model,
                "schema": schema.__name__,
                "system": system,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if not self._queue:
            raise LLMStructuredCallError(
                f"FakeLLMClient has no queued response for schema {schema.__name__}"
            )
        queued_schema, payload = self._queue.pop(0)
        if queued_schema is not schema:
            raise LLMStructuredCallError(
                f"FakeLLMClient queue mismatch: expected {queued_schema.__name__}, "
                f"got {schema.__name__}"
            )
        return schema.model_validate(payload)


# ---------------------------------------------------------------------------
# Module-level default
# ---------------------------------------------------------------------------

_default_client: LLMClient | None = None


def get_default_llm_client() -> LLMClient:
    """Return the process-wide default :class:`LLMClient`, lazily initialized."""
    global _default_client
    if _default_client is None:
        _default_client = AnthropicLLMClient()
    return _default_client


def set_default_llm_client(client: LLMClient | None) -> None:
    """Override the process-wide default. Pass ``None`` to reset."""
    global _default_client
    _default_client = client
