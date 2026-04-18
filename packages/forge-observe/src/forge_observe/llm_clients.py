"""Monkey-patch instrumentation for raw LLM SDKs.

This module is what makes the :class:`~forge_adapters.generic.GenericCallableAdapter`
credible: when a user wraps an arbitrary ``async def`` that internally calls
``anthropic.Anthropic(...)`` or ``openai.OpenAI(...)`` directly, Forge has no
adapter hook to latch onto. Instead, we temporarily replace the SDK's
``create`` method with a wrapper that extracts token usage and notifies the
:class:`~forge_core.protocols.LLMCallInterceptor`. When the user's callable
returns, patches are reversed atomically.

Design choices
--------------

1. **sys.modules probe, not import.** We only patch SDKs the user has already
   imported — we never force-import a dependency that isn't installed. This
   keeps ``forge-observe`` a thin, optional addition.

2. **Monkey-patch the *class method*, not the instance method.** Users
   instantiate ``Anthropic()`` or ``OpenAI()`` inside their flow; we can't know
   the instances in advance. Patching the bound method on the class makes every
   instance (past and future, within the scope) emit telemetry.

3. **Sync wrappers bridge via ``asyncio.run_coroutine_threadsafe``.** A user's
   sync callable runs inside ``asyncio.to_thread`` (see
   :class:`GenericCallableAdapter._execute`), so we ARE on a worker thread with
   access to the main loop via the captured reference. Blocking briefly on
   the interceptor future is acceptable because the alternative (unawaited
   coroutine) would silently drop telemetry.

4. **Patches are best-effort.** If an SDK's internal module layout shifts in a
   minor version, the descriptor's ``apply`` raises ``AttributeError`` and we
   log + continue rather than failing the run. Users who care about telemetry
   pin SDK versions; users who don't get a warning once per instrumentation.

5. **No partial state.** All applied patches are recorded in a stack on enter;
   the context manager's ``__exit__`` replays the stack in reverse regardless
   of whether the user's callable raised. A half-patched SDK is a long-lived
   landmine — we avoid it.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from forge_core.protocols import LLMCallInterceptor

logger = structlog.get_logger()


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


@contextmanager
def instrument_llm_clients(
    interceptor: LLMCallInterceptor,
    *,
    agent_id: str | None = None,
) -> Iterator[InstrumentationReport]:
    """Monkey-patch imported LLM SDKs for the duration of the ``with`` block.

    Yields an :class:`InstrumentationReport` describing which SDKs were
    patched so callers can surface a warning if, say, they expected
    anthropic coverage but the SDK wasn't imported (a common footgun
    when a helper module does the import lazily on first call).

    Parameters
    ----------
    interceptor
        The interceptor that will receive ``on_llm_start`` / ``on_llm_end``
        callbacks. Usually the orchestrator's ``ForgeLLMInterceptor``.
    agent_id
        Optional label attached to events from this scope. If the calling
        adapter is ``GenericCallableAdapter`` it typically passes its own
        ``name``; more advanced callers can leave ``None`` to let
        ``current_run()`` supply the agent.
    """
    # Snapshot the loop so sync wrappers running on worker threads can
    # marshal their interceptor calls back onto it. ``get_event_loop`` is
    # deprecated in 3.12+ when there's no running loop, so we only reach
    # for ``get_running_loop`` — if the caller isn't already in async
    # land, sync instrumentation degrades to "no telemetry" silently,
    # which is preferable to raising from a context manager whose job
    # is best-effort.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    applied: list[_AppliedPatch] = []
    report = InstrumentationReport()

    for descriptor in _DESCRIPTORS:
        if descriptor.module_name not in sys.modules:
            # User never imported this SDK; no reason to patch it.
            report.skipped.append(descriptor.sdk_label)
            continue
        try:
            patch = descriptor.apply(
                module=sys.modules[descriptor.module_name],
                interceptor=interceptor,
                loop=loop,
                agent_id=agent_id,
            )
        except Exception as exc:
            logger.warning(
                "llm_clients.patch_failed",
                sdk=descriptor.sdk_label,
                error=str(exc),
            )
            report.failed.append(descriptor.sdk_label)
            continue
        if patch is not None:
            applied.append(patch)
            report.patched.append(descriptor.sdk_label)

    try:
        yield report
    finally:
        # Reverse in LIFO order so nested patches on the same attribute
        # (hypothetical but cheap to preserve) unwind cleanly.
        for patch in reversed(applied):
            try:
                patch.revert()
            except Exception as exc:
                # We REALLY don't want a revert failure to mask a user
                # exception — log and carry on.
                logger.error(
                    "llm_clients.revert_failed",
                    target=patch.target_description,
                    error=str(exc),
                )


class InstrumentationReport:
    """Record of which SDKs were patched during an instrumentation scope.

    Useful for tests and operator dashboards. ``patched`` is the happy
    path; ``skipped`` means the SDK wasn't imported; ``failed`` means
    the descriptor raised during ``apply`` (usually a version mismatch).
    """

    __slots__ = ("failed", "patched", "skipped")

    def __init__(self) -> None:
        self.patched: list[str] = []
        self.skipped: list[str] = []
        self.failed: list[str] = []

    def __repr__(self) -> str:
        return (
            f"InstrumentationReport(patched={self.patched}, "
            f"skipped={self.skipped}, failed={self.failed})"
        )


# ----------------------------------------------------------------------
# Applied-patch record (for revert)
# ----------------------------------------------------------------------


class _AppliedPatch:
    """A single attribute replacement recorded for later revert.

    Not merged with descriptors because a single descriptor may apply
    multiple patches (e.g., both sync ``Anthropic.messages.create`` and
    async ``AsyncAnthropic.messages.create``).
    """

    __slots__ = ("attr", "original", "owner", "target_description")

    def __init__(
        self,
        *,
        owner: Any,
        attr: str,
        original: Any,
        target_description: str,
    ) -> None:
        self.owner = owner
        self.attr = attr
        self.original = original
        self.target_description = target_description

    def revert(self) -> None:
        setattr(self.owner, self.attr, self.original)


# ----------------------------------------------------------------------
# Descriptor machinery
# ----------------------------------------------------------------------


class _SDKDescriptor:
    """Describes how to locate and wrap a specific SDK's LLM entrypoint.

    Subclasses override :meth:`apply` to do the module-specific walk
    (``module.Anthropic.messages.create`` etc.). Keeping this as a
    classy dispatcher rather than a table-of-lambdas makes the version
    drift tolerance (try/except per SDK) explicit.
    """

    module_name: str = ""
    sdk_label: str = ""

    def apply(
        self,
        *,
        module: Any,
        interceptor: LLMCallInterceptor,
        loop: asyncio.AbstractEventLoop | None,
        agent_id: str | None,
    ) -> _AppliedPatch | None:
        raise NotImplementedError


# ----------------------------------------------------------------------
# Anthropic SDK
# ----------------------------------------------------------------------


class _AnthropicDescriptor(_SDKDescriptor):
    module_name = "anthropic"
    sdk_label = "anthropic"

    def apply(
        self,
        *,
        module: Any,
        interceptor: LLMCallInterceptor,
        loop: asyncio.AbstractEventLoop | None,
        agent_id: str | None,
    ) -> _AppliedPatch | None:
        # Anthropic's SDK exposes ``messages`` as a property on the client
        # instance. The underlying class is ``Messages`` (sync) or
        # ``AsyncMessages`` (async), living in ``anthropic.resources.messages``.
        # We patch ``create`` on each of those classes — that way every
        # existing and future ``Anthropic()`` / ``AsyncAnthropic()`` instance
        # picks up the instrumentation.
        patches: list[_AppliedPatch] = []
        try:
            from anthropic.resources.messages import (
                Messages as SyncMessages,
            )
        except ImportError:
            SyncMessages = None  # noqa: N806
        try:
            from anthropic.resources.messages import AsyncMessages
        except ImportError:
            AsyncMessages = None  # noqa: N806

        if SyncMessages is not None and hasattr(SyncMessages, "create"):
            original = SyncMessages.create
            wrapped = _make_sync_wrapper(
                original=original,
                interceptor=interceptor,
                loop=loop,
                agent_id=agent_id,
                extract_model=_extract_anthropic_model,
                extract_usage=_extract_anthropic_usage,
            )
            SyncMessages.create = wrapped
            patches.append(
                _AppliedPatch(
                    owner=SyncMessages,
                    attr="create",
                    original=original,
                    target_description="anthropic.resources.messages.Messages.create",
                )
            )
        if AsyncMessages is not None and hasattr(AsyncMessages, "create"):
            original = AsyncMessages.create
            wrapped = _make_async_wrapper(
                original=original,
                interceptor=interceptor,
                agent_id=agent_id,
                extract_model=_extract_anthropic_model,
                extract_usage=_extract_anthropic_usage,
            )
            AsyncMessages.create = wrapped
            patches.append(
                _AppliedPatch(
                    owner=AsyncMessages,
                    attr="create",
                    original=original,
                    target_description="anthropic.resources.messages.AsyncMessages.create",
                )
            )
        return _compose(patches)


def _extract_anthropic_model(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("model") or "unknown")


def _extract_anthropic_usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return (0, 0)
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


# ----------------------------------------------------------------------
# OpenAI SDK
# ----------------------------------------------------------------------


class _OpenAIDescriptor(_SDKDescriptor):
    module_name = "openai"
    sdk_label = "openai"

    def apply(
        self,
        *,
        module: Any,
        interceptor: LLMCallInterceptor,
        loop: asyncio.AbstractEventLoop | None,
        agent_id: str | None,
    ) -> _AppliedPatch | None:
        # ``openai>=1.0`` exposes ``chat.completions.create`` via
        # ``openai.resources.chat.completions.Completions`` (sync) and
        # ``AsyncCompletions`` (async). Same pattern as anthropic.
        patches: list[_AppliedPatch] = []
        try:
            from openai.resources.chat.completions import (
                Completions as SyncCompletions,
            )
        except ImportError:
            SyncCompletions = None  # noqa: N806
        try:
            from openai.resources.chat.completions import (
                AsyncCompletions,
            )
        except ImportError:
            AsyncCompletions = None  # noqa: N806

        if SyncCompletions is not None and hasattr(SyncCompletions, "create"):
            original = SyncCompletions.create
            wrapped = _make_sync_wrapper(
                original=original,
                interceptor=interceptor,
                loop=loop,
                agent_id=agent_id,
                extract_model=_extract_openai_model,
                extract_usage=_extract_openai_usage,
            )
            SyncCompletions.create = wrapped
            patches.append(
                _AppliedPatch(
                    owner=SyncCompletions,
                    attr="create",
                    original=original,
                    target_description="openai.resources.chat.completions.Completions.create",
                )
            )
        if AsyncCompletions is not None and hasattr(AsyncCompletions, "create"):
            original = AsyncCompletions.create
            wrapped = _make_async_wrapper(
                original=original,
                interceptor=interceptor,
                agent_id=agent_id,
                extract_model=_extract_openai_model,
                extract_usage=_extract_openai_usage,
            )
            AsyncCompletions.create = wrapped
            patches.append(
                _AppliedPatch(
                    owner=AsyncCompletions,
                    attr="create",
                    original=original,
                    target_description="openai.resources.chat.completions.AsyncCompletions.create",
                )
            )
        return _compose(patches)


def _extract_openai_model(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("model") or "unknown")


def _extract_openai_usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return (0, 0)
    # OpenAI uses ``prompt_tokens`` / ``completion_tokens``.
    return (
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
    )


# ----------------------------------------------------------------------
# Google Generative AI SDK
# ----------------------------------------------------------------------


class _GoogleGenAIDescriptor(_SDKDescriptor):
    module_name = "google.generativeai"
    sdk_label = "google.generativeai"

    def apply(
        self,
        *,
        module: Any,
        interceptor: LLMCallInterceptor,
        loop: asyncio.AbstractEventLoop | None,
        agent_id: str | None,
    ) -> _AppliedPatch | None:
        GenerativeModel = getattr(module, "GenerativeModel", None)  # noqa: N806 -- SDK class alias
        if GenerativeModel is None:
            return None

        patches: list[_AppliedPatch] = []
        if hasattr(GenerativeModel, "generate_content"):
            original = GenerativeModel.generate_content
            wrapped = _make_sync_wrapper(
                original=original,
                interceptor=interceptor,
                loop=loop,
                agent_id=agent_id,
                extract_model=_extract_google_model,
                extract_usage=_extract_google_usage,
            )
            GenerativeModel.generate_content = wrapped
            patches.append(
                _AppliedPatch(
                    owner=GenerativeModel,
                    attr="generate_content",
                    original=original,
                    target_description="google.generativeai.GenerativeModel.generate_content",
                )
            )
        if hasattr(GenerativeModel, "generate_content_async"):
            original = GenerativeModel.generate_content_async
            wrapped = _make_async_wrapper(
                original=original,
                interceptor=interceptor,
                agent_id=agent_id,
                extract_model=_extract_google_model,
                extract_usage=_extract_google_usage,
            )
            GenerativeModel.generate_content_async = wrapped
            patches.append(
                _AppliedPatch(
                    owner=GenerativeModel,
                    attr="generate_content_async",
                    original=original,
                    target_description="google.generativeai.GenerativeModel.generate_content_async",
                )
            )
        return _compose(patches)


def _extract_google_model(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    # For bound-method calls, args[0] is the GenerativeModel instance.
    if args:
        model_attr = getattr(args[0], "model_name", None)
        if model_attr:
            return str(model_attr)
    return str(kwargs.get("model") or "unknown")


def _extract_google_usage(response: Any) -> tuple[int, int]:
    metadata = getattr(response, "usage_metadata", None)
    if metadata is None:
        return (0, 0)
    return (
        int(getattr(metadata, "prompt_token_count", 0) or 0),
        int(getattr(metadata, "candidates_token_count", 0) or 0),
    )


# ----------------------------------------------------------------------
# Wrapper factories
# ----------------------------------------------------------------------


def _make_sync_wrapper(
    *,
    original: Callable[..., Any],
    interceptor: LLMCallInterceptor,
    loop: asyncio.AbstractEventLoop | None,
    agent_id: str | None,
    extract_model: Callable[[tuple[Any, ...], dict[str, Any]], str],
    extract_usage: Callable[[Any], tuple[int, int]],
) -> Callable[..., Any]:
    """Build a sync wrapper that marshals interceptor calls onto ``loop``.

    If ``loop`` is ``None`` we degrade to plain pass-through — there's no
    safe way to run a coroutine from a sync context with no loop
    reference (``asyncio.run`` would create a second loop and deadlock
    in-process LLM code).
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if loop is None:
            return original(*args, **kwargs)

        model = extract_model(args, kwargs)
        start = time.perf_counter()
        # Open span on the owning loop; block briefly for the span_id —
        # this is the contract ForgeLLMInterceptor requires.
        try:
            span_id = asyncio.run_coroutine_threadsafe(
                interceptor.on_llm_start(
                    model=model,
                    agent_id=agent_id,
                    metadata={"source": "generic_sdk_patch"},
                ),
                loop,
            ).result(timeout=_INTERCEPTOR_TIMEOUT)
        except Exception as exc:
            # Never fail the user's LLM call because instrumentation hiccupped.
            logger.warning("llm_clients.on_start_failed", error=str(exc))
            return original(*args, **kwargs)

        try:
            response = original(*args, **kwargs)
        except BaseException as exc:
            try:
                asyncio.run_coroutine_threadsafe(
                    interceptor.on_llm_error(span_id, exc),
                    loop,
                ).result(timeout=_INTERCEPTOR_TIMEOUT)
            except Exception as inner:
                logger.warning("llm_clients.on_error_failed", error=str(inner))
            raise

        input_tokens, output_tokens = extract_usage(response)
        try:
            asyncio.run_coroutine_threadsafe(
                interceptor.on_llm_end(
                    span_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    output=response,
                ),
                loop,
            ).result(timeout=_INTERCEPTOR_TIMEOUT)
        except Exception as exc:
            logger.warning("llm_clients.on_end_failed", error=str(exc))

        _ = start  # reserved for future micro-metric; kept named for clarity
        return response

    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    wrapper.__forge_instrumented__ = True  # type: ignore[attr-defined]
    return wrapper


def _make_async_wrapper(
    *,
    original: Callable[..., Any],
    interceptor: LLMCallInterceptor,
    agent_id: str | None,
    extract_model: Callable[[tuple[Any, ...], dict[str, Any]], str],
    extract_usage: Callable[[Any], tuple[int, int]],
) -> Callable[..., Any]:
    """Build an async wrapper; ``await`` the interceptor directly.

    Unlike the sync variant, we're already on the event loop that owns
    the interceptor, so no run_coroutine_threadsafe dance is needed.
    """

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        model = extract_model(args, kwargs)
        try:
            span_id = await interceptor.on_llm_start(
                model=model,
                agent_id=agent_id,
                metadata={"source": "generic_sdk_patch"},
            )
        except Exception as exc:
            logger.warning("llm_clients.on_start_failed", error=str(exc))
            return await original(*args, **kwargs)

        try:
            response = await original(*args, **kwargs)
        except BaseException as exc:
            try:
                await interceptor.on_llm_error(span_id, exc)
            except Exception as inner:
                logger.warning("llm_clients.on_error_failed", error=str(inner))
            raise

        # Some async SDK methods return streams rather than a single
        # response with ``.usage``. If usage extraction returns (0,0) we
        # still publish the event — bad data is better than silent drop.
        input_tokens, output_tokens = extract_usage(response)
        try:
            await interceptor.on_llm_end(
                span_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                output=response,
            )
        except Exception as exc:
            logger.warning("llm_clients.on_end_failed", error=str(exc))
        return response

    # Preserve coroutine-function metadata so ``inspect.iscoroutinefunction``
    # still returns True for downstream introspection.
    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    wrapper.__forge_instrumented__ = True  # type: ignore[attr-defined]
    return wrapper


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


_INTERCEPTOR_TIMEOUT: float = 5.0


def _compose(patches: list[_AppliedPatch]) -> _AppliedPatch | None:
    """Wrap a list of patches as a single revertable unit.

    If a descriptor produced multiple patches (e.g., sync + async
    anthropic variants) we return one :class:`_AppliedPatch` whose
    ``revert`` replays the entire list. This keeps the applied-stack
    count meaningful — one descriptor == one slot.
    """
    if not patches:
        return None
    if len(patches) == 1:
        return patches[0]

    # Build a compound revert closure without mutating _AppliedPatch's slots.
    class _Compound:
        __slots__ = ("target_description",)

        def __init__(self) -> None:
            self.target_description = ",".join(p.target_description for p in patches)

        def revert(self) -> None:
            for patch in reversed(patches):
                patch.revert()

    return _Compound()  # type: ignore[return-value]


# Registry of SDKs we know how to patch. Kept as a module-level constant
# so tests can override it (e.g., to inject a fake descriptor for a
# fictitious SDK without depending on real packages).
_DESCRIPTORS: list[_SDKDescriptor] = [
    _AnthropicDescriptor(),
    _OpenAIDescriptor(),
    _GoogleGenAIDescriptor(),
]


# Re-export for advanced users who want to register custom SDKs.
def register_descriptor(descriptor: _SDKDescriptor) -> None:
    """Append a custom descriptor to the registry.

    Intended for users embedding Forge in a closed ecosystem with a
    proprietary LLM SDK — implement :class:`_SDKDescriptor` and register
    it at import time.
    """
    _DESCRIPTORS.append(descriptor)


__all__ = [
    "InstrumentationReport",
    "instrument_llm_clients",
    "register_descriptor",
]


# Keep ``inspect`` referenced — used for coroutine detection by
# downstream code that introspects our wrappers.
_ = inspect
