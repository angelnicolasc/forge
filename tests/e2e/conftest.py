"""E2E test fixtures — install/remove the fake anthropic module."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ``tests/`` and ``tests/e2e/`` are intentionally NOT Python packages
# (no ``__init__.py``) so they don't collide with each package's own
# ``tests/`` package during pytest collection. That means the fixtures
# directory isn't importable by dotted path either — inject it onto
# ``sys.path`` so ``import fake_anthropic`` works.
_FIXTURES_DIR = Path(__file__).parent / "fixtures"
if str(_FIXTURES_DIR) not in sys.path:
    sys.path.insert(0, str(_FIXTURES_DIR))

import fake_anthropic  # noqa: E402


@pytest.fixture
def fake_anthropic_sdk():
    """Install the fake ``anthropic`` SDK for the duration of a test.

    Yields the ``fake_anthropic`` module so tests can queue scripted
    responses via ``Messages.queue(...)``. Removes the module from
    ``sys.modules`` on teardown so neighboring tests see a clean slate.
    """
    fake_anthropic.install()
    # Prime with a couple of responses; token counts chosen so the cost
    # via mock-model pricing ($1/1M in, $1/1M out) is > $0.00 at
    # 5-decimal resolution in the Rich output.
    fake_anthropic.Messages.queue("plan draft", input_tokens=200, output_tokens=400)
    fake_anthropic.Messages.queue("research notes", input_tokens=350, output_tokens=500)
    try:
        yield fake_anthropic
    finally:
        fake_anthropic.uninstall()
