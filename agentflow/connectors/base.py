"""Outbound connector contract.

Every connector returns a dict describing what it did, including a ``mode`` of
``live`` or ``simulated``. Dispatch results are stored on the run, so the audit
trail records not just "we notified HSE" but whether that notification actually
left the building.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..models import PlannedAction, Run


class Connector(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def dispatch(self, action: PlannedAction, run: Run) -> dict[str, Any]:
        """Execute one approved action."""

    @property
    def configured(self) -> bool:
        return False
