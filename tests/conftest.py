"""Shared fixtures.

Every test runs against the simulated providers: no network, no keys, no
flakiness. That is a property of the design, not a testing trick -- the same
fallbacks make the service demoable on a laptop.
"""

from __future__ import annotations

import pytest

from agentflow.config import get_settings
from agentflow.models import Signal
from agentflow.orchestrator import Orchestrator
from agentflow.store import RunStore


@pytest.fixture(autouse=True)
def _no_demo_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo pacing exists for humans watching the graph, not for CI."""
    monkeypatch.setattr(get_settings(), "step_delay_ms", 0, raising=False)


@pytest.fixture
def store() -> RunStore:
    return RunStore(persist=False)


@pytest.fixture
def orch(store: RunStore) -> Orchestrator:
    return Orchestrator(store)


@pytest.fixture
def run_signal(orch: Orchestrator):
    async def _run(text: str, **kwargs):
        return await orch.start(Signal(text=text, **kwargs))

    return _run
