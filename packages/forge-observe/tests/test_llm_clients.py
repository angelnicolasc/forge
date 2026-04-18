"""Tests for ``forge_observe.llm_clients.instrument_llm_clients``.

These tests register stand-in ``anthropic`` / ``openai`` /
``google.generativeai`` modules in :mod:`sys.modules` so we exercise the
monkey-patch machinery without depending on real SDK installs. The
descriptors probe ``sys.modules`` and then attempt a relative import
(``from anthropic.resources.messages import Messages``) — we build a
matching package layout so those imports resolve to our fakes.

The key assertions are:

* After entering the context, the SDK's ``create`` method is replaced
  with a Forge wrapper.
* Calls through the wrapper invoke ``on_llm_start`` / ``on_llm_end``
  exactly once each, in that order, with the model name and token
  counts extracted from the SDK's response shape.
* On exit, the original method is restored byte-for-byte.
* Exceptions raised inside the user's LLM call propagate out, and
  ``on_llm_error`` is called with the original exception.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest

from forge_observe.llm_clients import (
    InstrumentationReport,
    instrument_llm_clients,
)

# ---------------------------------------------------------------------------
# Recording interceptor
# ---------------------------------------------------------------------------


class _RecordingInterceptor:
    """Tiny LLMCallInterceptor stand-in that logs every call into lists.

    Not a ``MagicMock`` because we want deterministic ordering and
    typed attributes; ``_assert_paired`` uses that.
    """

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.ends: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self._counter = 0

    async def on_llm_start(
        self,
        *,
        model: str,
        agent_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self._counter += 1
        span_id = f"span-{self._counter}"
        self.starts.append(
            {
                "span_id": span_id,
                "model": model,
                "agent_id": agent_id,
                "metadata": metadata,
            }
        )
        return span_id

    async def on_llm_end(
        self,
        span_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        output: Any = None,
    ) -> None:
        self.ends.append(
            {
                "span_id": span_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "output": output,
            }
        )

    async def on_llm_error(self, span_id: str, error: BaseException) -> None:
        self.errors.append({"span_id": span_id, "error": error})


# ---------------------------------------------------------------------------
# Fake SDK factories — register real Python packages in sys.modules so the
# descriptors' relative imports work.
# ---------------------------------------------------------------------------


@dataclass
class _FakeAnthropicUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeAnthropicResponse:
    usage: _FakeAnthropicUsage
    content: str = "hello"


def _install_fake_anthropic() -> type:
    """Create and register ``anthropic`` + ``anthropic.resources.messages``.

    Returns the ``Messages`` class for the test to make assertions on.
    """
    anthropic_pkg = types.ModuleType("anthropic")
    anthropic_pkg.__path__ = []  # type: ignore[attr-defined]  # marks it a package
    resources_pkg = types.ModuleType("anthropic.resources")
    resources_pkg.__path__ = []  # type: ignore[attr-defined]
    messages_mod = types.ModuleType("anthropic.resources.messages")

    class Messages:
        def create(self, **kwargs: Any) -> _FakeAnthropicResponse:
            return _FakeAnthropicResponse(
                usage=_FakeAnthropicUsage(input_tokens=11, output_tokens=22)
            )

    class AsyncMessages:
        async def create(self, **kwargs: Any) -> _FakeAnthropicResponse:
            return _FakeAnthropicResponse(
                usage=_FakeAnthropicUsage(input_tokens=33, output_tokens=44)
            )

    messages_mod.Messages = Messages  # type: ignore[attr-defined]
    messages_mod.AsyncMessages = AsyncMessages  # type: ignore[attr-defined]
    sys.modules["anthropic"] = anthropic_pkg
    sys.modules["anthropic.resources"] = resources_pkg
    sys.modules["anthropic.resources.messages"] = messages_mod
    return Messages


def _install_fake_openai() -> type:
    """Register ``openai.resources.chat.completions``."""
    openai_pkg = types.ModuleType("openai")
    openai_pkg.__path__ = []  # type: ignore[attr-defined]
    resources_pkg = types.ModuleType("openai.resources")
    resources_pkg.__path__ = []  # type: ignore[attr-defined]
    chat_pkg = types.ModuleType("openai.resources.chat")
    chat_pkg.__path__ = []  # type: ignore[attr-defined]
    completions_mod = types.ModuleType("openai.resources.chat.completions")

    class _OpenAIUsage:
        prompt_tokens = 7
        completion_tokens = 13

    class _OpenAIResponse:
        usage = _OpenAIUsage()
        choices: list[Any] = []  # noqa: RUF012

    class Completions:
        def create(self, **kwargs: Any) -> _OpenAIResponse:
            return _OpenAIResponse()

    class AsyncCompletions:
        async def create(self, **kwargs: Any) -> _OpenAIResponse:
            return _OpenAIResponse()

    completions_mod.Completions = Completions  # type: ignore[attr-defined]
    completions_mod.AsyncCompletions = AsyncCompletions  # type: ignore[attr-defined]
    sys.modules["openai"] = openai_pkg
    sys.modules["openai.resources"] = resources_pkg
    sys.modules["openai.resources.chat"] = chat_pkg
    sys.modules["openai.resources.chat.completions"] = completions_mod
    return Completions


def _install_fake_google() -> type:
    """Register ``google.generativeai.GenerativeModel``."""
    google_pkg = types.ModuleType("google")
    google_pkg.__path__ = []  # type: ignore[attr-defined]
    genai_mod = types.ModuleType("google.generativeai")

    class _GoogleUsage:
        prompt_token_count = 5
        candidates_token_count = 9

    class _GoogleResponse:
        usage_metadata = _GoogleUsage()

    class GenerativeModel:
        def __init__(self, model_name: str = "gemini-1.5-flash") -> None:
            self.model_name = model_name

        def generate_content(self, prompt: str) -> _GoogleResponse:
            return _GoogleResponse()

        async def generate_content_async(self, prompt: str) -> _GoogleResponse:
            return _GoogleResponse()

    genai_mod.GenerativeModel = GenerativeModel  # type: ignore[attr-defined]
    sys.modules["google"] = google_pkg
    sys.modules["google.generativeai"] = genai_mod
    return GenerativeModel


# ---------------------------------------------------------------------------
# Fixture: clean up sys.modules after each test so fakes don't leak.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_sdk_modules() -> Any:
    """Remove any SDK-named modules after the test.

    Even when a test doesn't install a given SDK, the registry inside
    :mod:`forge_observe.llm_clients` caches no state, so cleanup is just
    about ``sys.modules`` hygiene.
    """
    names = [
        "anthropic",
        "anthropic.resources",
        "anthropic.resources.messages",
        "openai",
        "openai.resources",
        "openai.resources.chat",
        "openai.resources.chat.completions",
        "google",
        "google.generativeai",
    ]
    # Snapshot anything real that happens to exist before we run.
    pre = {n: sys.modules.get(n) for n in names}
    yield
    for n in names:
        if pre.get(n) is not None:
            sys.modules[n] = pre[n]  # type: ignore[assignment]
        else:
            sys.modules.pop(n, None)
    # Invalidate caches so a subsequent importlib.import_module works.
    importlib.invalidate_caches()


# ---------------------------------------------------------------------------
# Anthropic tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instrument_patches_anthropic_async_messages_create() -> None:
    """Inside the scope, AsyncMessages.create notifies the interceptor."""
    _install_fake_anthropic()
    from anthropic.resources.messages import AsyncMessages

    original = AsyncMessages.create
    interceptor = _RecordingInterceptor()

    with instrument_llm_clients(interceptor) as report:
        assert "anthropic" in report.patched
        assert AsyncMessages.create is not original  # patched
        inst = AsyncMessages()
        response = await inst.create(model="claude-sonnet-4-20250514")

    # Restored on exit.
    assert AsyncMessages.create is original

    # Verify the telemetry round-trip.
    assert response.usage.input_tokens == 33
    assert len(interceptor.starts) == 1
    assert interceptor.starts[0]["model"] == "claude-sonnet-4-20250514"
    assert len(interceptor.ends) == 1
    assert interceptor.ends[0]["input_tokens"] == 33
    assert interceptor.ends[0]["output_tokens"] == 44
    assert interceptor.starts[0]["span_id"] == interceptor.ends[0]["span_id"]


@pytest.mark.asyncio
async def test_instrument_restores_patches_on_exception() -> None:
    """If the user's code raises, the SDK is still un-patched on exit."""
    _install_fake_anthropic()
    from anthropic.resources.messages import AsyncMessages

    original = AsyncMessages.create
    interceptor = _RecordingInterceptor()

    class _BoomError(RuntimeError):
        pass

    try:
        with instrument_llm_clients(interceptor):
            assert AsyncMessages.create is not original
            raise _BoomError("user error")
    except _BoomError:
        pass

    assert AsyncMessages.create is original


@pytest.mark.asyncio
async def test_async_wrapper_calls_on_llm_error_on_failure() -> None:
    """If the SDK itself raises, on_llm_error fires and the error propagates."""
    _install_fake_anthropic()
    from anthropic.resources.messages import AsyncMessages

    interceptor = _RecordingInterceptor()

    # Replace .create with one that raises.
    class _SDKError(RuntimeError):
        pass

    async def _broken_create(self: Any, **kwargs: Any) -> Any:
        raise _SDKError("rate limited")

    AsyncMessages.create = _broken_create  # type: ignore[method-assign]

    with instrument_llm_clients(interceptor):
        inst = AsyncMessages()
        with pytest.raises(_SDKError):
            await inst.create(model="claude-sonnet-4-20250514")

    assert len(interceptor.errors) == 1
    assert isinstance(interceptor.errors[0]["error"], _SDKError)
    # on_llm_end should NOT have been called — error path took over.
    assert interceptor.ends == []


# ---------------------------------------------------------------------------
# OpenAI tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instrument_patches_openai_async_completions() -> None:
    _install_fake_openai()
    from openai.resources.chat.completions import AsyncCompletions

    interceptor = _RecordingInterceptor()
    with instrument_llm_clients(interceptor) as report:
        assert "openai" in report.patched
        inst = AsyncCompletions()
        response = await inst.create(model="gpt-4o-mini")

    assert response.usage.prompt_tokens == 7
    assert len(interceptor.ends) == 1
    assert interceptor.ends[0]["input_tokens"] == 7
    assert interceptor.ends[0]["output_tokens"] == 13
    assert interceptor.starts[0]["model"] == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Google GenAI tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instrument_patches_google_generate_content_async() -> None:
    _install_fake_google()
    from google.generativeai import GenerativeModel

    interceptor = _RecordingInterceptor()
    with instrument_llm_clients(interceptor) as report:
        assert "google.generativeai" in report.patched
        model = GenerativeModel("gemini-1.5-flash")
        await model.generate_content_async("hi")

    assert len(interceptor.ends) == 1
    assert interceptor.ends[0]["input_tokens"] == 5
    assert interceptor.ends[0]["output_tokens"] == 9
    # google descriptor extracts model from instance.model_name, not kwargs.
    assert interceptor.starts[0]["model"] == "gemini-1.5-flash"


# ---------------------------------------------------------------------------
# Skip / degrade paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_sdks_not_in_sys_modules() -> None:
    """Descriptors skip over SDKs the user never imported.

    We only install openai; anthropic and google should land in
    ``report.skipped`` rather than patched or failed.
    """
    _install_fake_openai()

    interceptor = _RecordingInterceptor()
    with instrument_llm_clients(interceptor) as report:
        assert "openai" in report.patched
        assert "anthropic" in report.skipped
        assert "google.generativeai" in report.skipped
        assert report.failed == []


def test_report_repr_lists_categories() -> None:
    """Quick sanity check that the report is operator-readable."""
    r = InstrumentationReport()
    r.patched.append("anthropic")
    r.skipped.append("openai")
    r.failed.append("mystery")
    s = repr(r)
    assert "anthropic" in s
    assert "openai" in s
    assert "mystery" in s


# ---------------------------------------------------------------------------
# Sync wrapper: tests the cross-thread bridge.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_wrapper_marshals_interceptor_calls_to_running_loop() -> None:
    """A sync ``.create`` called from ``to_thread`` still produces events.

    This is the hardest path to exercise: we're in an async test, we
    dispatch to a worker thread via ``asyncio.to_thread``, and on that
    thread we call the patched sync ``Messages.create``. The wrapper
    uses ``asyncio.run_coroutine_threadsafe`` to hop back onto the main
    loop for the interceptor call. Regressions here manifest as missing
    events or deadlocks.
    """
    Messages = _install_fake_anthropic()
    interceptor = _RecordingInterceptor()

    with instrument_llm_clients(interceptor):
        inst = Messages()
        # Important: call through to_thread so the wrapper's sync path fires.
        response = await asyncio.to_thread(inst.create, model="claude-haiku-4-20250514")

    assert response.usage.input_tokens == 11
    assert len(interceptor.ends) == 1
    assert interceptor.ends[0]["input_tokens"] == 11
    assert interceptor.ends[0]["output_tokens"] == 22
    assert interceptor.starts[0]["model"] == "claude-haiku-4-20250514"
