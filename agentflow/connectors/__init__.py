"""Connector registry."""

from __future__ import annotations

from typing import Any

from ..models import PlannedAction, Run
from .base import Connector
from .cmms import CmmsConnector
from .power_automate import PowerAutomateConnector
from .teams import TeamsConnector

_CONNECTORS: dict[str, Connector] = {
    "teams": TeamsConnector(),
    "power_automate": PowerAutomateConnector(),
    "cmms": CmmsConnector(),
}


def get_connector(name: str) -> Connector | None:
    return _CONNECTORS.get(name)


def connector_status() -> list[dict[str, Any]]:
    return [
        {"name": name, "mode": "live" if c.configured else "simulated"}
        for name, c in _CONNECTORS.items()
    ]


async def dispatch(action: PlannedAction, run: Run) -> dict[str, Any]:
    if action.connector == "none":
        return {"mode": "noop", "channel": "none"}
    connector = get_connector(action.connector)
    if connector is None:
        return {"mode": "error", "error": f"unknown connector {action.connector!r}"}
    return await connector.dispatch(action, run)


__all__ = ["Connector", "dispatch", "get_connector", "connector_status"]
