"""Shared fixtures for forge-observe tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_sse_subscribers():
    """Guarantee no SSE subscriber leaks across tests (ζ.4 defense-in-depth).

    ``stream_events`` registers a queue on ``_live_subscribers`` before its
    first yield and removes it in ``finally``. If that cleanup lands late
    — e.g. because a TestClient sync worker tore down the generator mid-run
    — the queue can outlive the test. This fixture clears the set on both
    sides of every test so a leak surfaces as a failing assertion in
    ``test_stream_endpoint_cleans_up_subscriber`` rather than as a flake in
    an unrelated neighbor.
    """
    from forge_observe.exporters.api import reset_live_subscribers

    reset_live_subscribers()
    yield
    reset_live_subscribers()
