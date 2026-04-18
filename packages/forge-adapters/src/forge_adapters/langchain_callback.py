"""LangChain/LangGraph callback handler that feeds Forge's interceptor.

Most of the LangGraph ecosystem — ``ChatAnthropic``, ``ChatOpenAI``,
``ChatGoogleGenerativeAI``, and every OSS tool that wraps them — surfaces
LLM telemetry through LangChain's :class:`BaseCallbackHandler` protocol.
Installing a single :class:`ForgeLangChainCallback` onto a graph's
``config={"callbacks": [...]}`` therefore captures token usage for
*every* model call inside that graph, regardless of which provider SDK
actually executes the request.

This callback is the canonical closure for the L7/L8 Gap #1
("``RunEvent.input_tokens/cost`` siempre 0 en LangGraph ``ainvoke``"):
the LangGraph adapter installs it on every invocation and the interceptor
it wraps publishes fully-populated ``LLM_CALL`` events to the bus.

Implementation notes
--------------------
* LangChain's "usage_metadata" location has moved around across 0.1.x →
  0.3.x. We look in three places (``LLMResult.llm_output``, per-generation
  ``generation_info``, per-generation ``message.usage_metadata``) and pick
  whichever is non-empty. This is the ugly reality of integrating with a
  fast-moving OSS framework — keeping the logic here (not in
  ``forge-core``) contains the blast radius of API churn.
* We deliberately do not inherit from ``BaseCallbackHandler`` with a hard
  import at module load: LangChain is an optional dependency from Forge's
  perspective (CrewAI and generic adapters don't need it). Import is
  deferred until class instantiation so ``forge-adapters`` imports cleanly
  in a skinny install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from uuid import UUID

    from forge_core.protocols import LLMCallInterceptor

logger = structlog.get_logger()


def _callback_base() -> type:
    """Return LangChain's ``AsyncCallbackHandler`` class, imported lazily.

    Raises ``ImportError`` with a user-actionable message if LangChain
    isn't installed. Called exactly once at class-definition time via
    the factory pattern below.
    """
    try:
        from langchain_core.callbacks import AsyncCallbackHandler
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "ForgeLangChainCallback requires langchain-core. "
            "Install with: pip install 'forge-os[langgraph]'"
        ) from exc
    return AsyncCallbackHandler  # type: ignore[no-any-return]


def _build_callback_class() -> type:
    """Factory: construct the concrete callback class on first import.

    Using a factory avoids evaluating the LangChain import at module-load
    time, which matters because ``forge-adapters.__init__`` imports this
    module eagerly to expose ``ForgeLangChainCallback`` for user code.
    """

    AsyncCallbackHandler = _callback_base()  # noqa: N806 -- mirrors the LangChain class name verbatim

    class _ForgeLangChainCallback(AsyncCallbackHandler):  # type: ignore[misc, valid-type]
        """LangChain AsyncCallbackHandler that routes to Forge's interceptor."""

        # LangChain sets this attribute to allow callbacks in async contexts.
        # Without it, chat model start events may be skipped.
        raise_error: bool = False
        run_inline: bool = False

        def __init__(self, interceptor: LLMCallInterceptor) -> None:
            super().__init__()
            self._interceptor = interceptor
            # LangChain's run_id (UUID) -> Forge's span_id (str). We map
            # one-to-one so `on_llm_end` can retrieve the right span when
            # multiple concurrent LLM calls are in flight.
            self._run_to_span: dict[UUID, str] = {}

        # --------------------------------------------------------------
        # LLM call lifecycle
        # --------------------------------------------------------------

        async def on_llm_start(
            self,
            serialized: dict[str, Any],
            prompts: list[str],
            *,
            run_id: UUID,
            parent_run_id: UUID | None = None,
            tags: list[str] | None = None,
            metadata: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> None:
            """Handle the start of a non-chat LLM call (legacy completion API)."""
            model = self._extract_model(serialized, metadata)
            span_id = await self._interceptor.on_llm_start(
                model=model,
                agent_id=self._extract_agent_id(tags, metadata),
                metadata={"source": "langchain", "prompts_count": len(prompts)},
            )
            self._run_to_span[run_id] = span_id

        async def on_chat_model_start(
            self,
            serialized: dict[str, Any],
            messages: list[list[Any]],
            *,
            run_id: UUID,
            parent_run_id: UUID | None = None,
            tags: list[str] | None = None,
            metadata: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> None:
            """Handle the start of a chat-model call (modern message API)."""
            model = self._extract_model(serialized, metadata)
            span_id = await self._interceptor.on_llm_start(
                model=model,
                agent_id=self._extract_agent_id(tags, metadata),
                metadata={"source": "langchain.chat", "batches": len(messages)},
            )
            self._run_to_span[run_id] = span_id

        async def on_llm_end(
            self,
            response: Any,  # LLMResult
            *,
            run_id: UUID,
            parent_run_id: UUID | None = None,
            **kwargs: Any,
        ) -> None:
            """Close the span with token counts extracted from the LLMResult."""
            span_id = self._run_to_span.pop(run_id, None)
            if span_id is None:
                # Either on_llm_start was never called for this run_id (bug in
                # callback dispatch) or we're seeing duplicate end events. Log
                # and move on — re-raising would break the user's LLM call.
                logger.warning("langchain_callback.orphan_end", run_id=str(run_id))
                return

            input_tokens, output_tokens = _extract_usage(response)
            await self._interceptor.on_llm_end(
                span_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                output=response,
            )

        async def on_llm_error(
            self,
            error: BaseException,
            *,
            run_id: UUID,
            parent_run_id: UUID | None = None,
            **kwargs: Any,
        ) -> None:
            """Close the span with an error."""
            span_id = self._run_to_span.pop(run_id, None)
            if span_id is None:
                logger.warning("langchain_callback.orphan_error", run_id=str(run_id))
                return
            await self._interceptor.on_llm_error(span_id, error)

        # --------------------------------------------------------------
        # Helpers
        # --------------------------------------------------------------

        @staticmethod
        def _extract_model(
            serialized: dict[str, Any],
            metadata: dict[str, Any] | None,
        ) -> str:
            """Find the model name in LangChain's serialized callback payload.

            LangChain puts it in different fields depending on the provider
            and the version. We check the common spots and fall back to a
            structured "unknown" string that the cost model will flag via
            its one-shot warning.
            """
            # 0.3.x: serialized["kwargs"]["model"] for chat models.
            kwargs = serialized.get("kwargs", {}) if isinstance(serialized, dict) else {}
            for key in ("model", "model_name", "model_id"):
                if kwargs.get(key):
                    return str(kwargs[key])
            if metadata:
                for key in ("ls_model_name", "model", "model_name"):
                    if metadata.get(key):
                        return str(metadata[key])
            # Invocation params passed via on_llm_start extras in some
            # versions include the model under "invocation_params".
            inv = serialized.get("invocation_params", {}) if isinstance(serialized, dict) else {}
            for key in ("model", "model_name"):
                if inv.get(key):
                    return str(inv[key])
            return "unknown"

        @staticmethod
        def _extract_agent_id(
            tags: list[str] | None,
            metadata: dict[str, Any] | None,
        ) -> str | None:
            """Derive a Forge ``agent_id`` from LangGraph node metadata.

            LangGraph decorates callback metadata with ``langgraph_node``
            when invoking a node — this is the closest equivalent to
            "which agent made this call" that we get out of the box.
            """
            if metadata:
                for key in ("langgraph_node", "forge_agent_id", "agent_id", "agent"):
                    if metadata.get(key):
                        return str(metadata[key])
            if tags:
                for tag in tags:
                    if tag.startswith("agent:"):
                        return tag.split(":", 1)[1]
            return None

    return _ForgeLangChainCallback


# Lazy-constructed class. The first access builds it (and imports LangChain).
_CALLBACK_CLASS: type | None = None


def ForgeLangChainCallback(interceptor: LLMCallInterceptor) -> Any:  # noqa: N802 -- factory masquerades as a class; callers write `ForgeLangChainCallback(...)`
    """Factory function masquerading as a class.

    Using a factory here lets us defer the LangChain import to the moment
    a user actually constructs one. ``forge-adapters`` modules that never
    touch LangChain (e.g. ``crewai_adapter.py`` when CrewAI is used without
    a LangChain LLM) import cleanly in environments that don't ship it.
    """
    global _CALLBACK_CLASS
    if _CALLBACK_CLASS is None:
        _CALLBACK_CLASS = _build_callback_class()
    return _CALLBACK_CLASS(interceptor)


# ----------------------------------------------------------------------
# Usage extraction — the painful compatibility layer
# ----------------------------------------------------------------------


def _extract_usage(response: Any) -> tuple[int, int]:
    """Pull ``(input_tokens, output_tokens)`` from a LangChain ``LLMResult``.

    Returns ``(0, 0)`` when usage isn't reported — this surfaces as a zero
    cost with a ``cost_model.unknown_model`` warning only if the model is
    *also* missing, so users can distinguish "no usage reported" from "no
    pricing data".

    The search order reflects LangChain history:

    1. ``response.llm_output["token_usage"]`` (OpenAI-style, legacy).
    2. ``generation.generation_info["usage_metadata"]`` (0.3.x chat models).
    3. ``generation.message.usage_metadata`` (Anthropic via langchain-anthropic).
    """
    input_tokens = 0
    output_tokens = 0

    # 1. Legacy llm_output.token_usage.
    llm_output = getattr(response, "llm_output", None) or {}
    token_usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
    if token_usage:
        input_tokens = int(token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0)
        output_tokens = int(
            token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0
        )
        if input_tokens or output_tokens:
            return input_tokens, output_tokens

    # 2. + 3. Walk the generations list looking for usage_metadata.
    generations = getattr(response, "generations", None) or []
    for batch in generations:
        for gen in batch:
            # 2: generation_info["usage_metadata"]
            info = getattr(gen, "generation_info", None) or {}
            usage = info.get("usage_metadata") if isinstance(info, dict) else None
            # 3: gen.message.usage_metadata (chat generations)
            if usage is None:
                msg = getattr(gen, "message", None)
                usage = getattr(msg, "usage_metadata", None) if msg is not None else None
            if usage:
                input_tokens += int(usage.get("input_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or 0)

    return input_tokens, output_tokens
