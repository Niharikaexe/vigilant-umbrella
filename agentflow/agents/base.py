"""Shared agent plumbing.

An "agent" here is a single async function over a shared :class:`AgentContext`.
It reads from and writes to the run, and returns a dict of detail that becomes
the payload of the node's trace event.

Three control-flow signals, and nothing else:

* return normally           -> node ``ok``
* raise :class:`Degraded`   -> node ``degraded``, pipeline continues
* raise :class:`Halt`       -> node ``failed``, pipeline stops, human required

Keeping the contract this small is what lets ``config/pipeline.yaml`` be the
real orchestration rather than a picture of it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..models import Run


class Degraded(Exception):
    """Non-fatal: this node could not do its job but the run can continue."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.detail = detail or {}


class Halt(Exception):
    """Fatal: stop the run and hand it to a human. Always fail closed."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.detail = detail or {}


@dataclass
class AgentContext:
    run: Run
    # Emit an interstitial trace line from inside a long-running node.
    log: Callable[[str, dict[str, Any]], Awaitable[None]]
    pipeline: dict[str, Any]

    @property
    def allowed_actions(self) -> dict[str, dict[str, Any]]:
        return {a["id"]: a for a in self.pipeline.get("allowed_actions", [])}


Agent = Callable[[AgentContext], Awaitable[dict[str, Any]]]
