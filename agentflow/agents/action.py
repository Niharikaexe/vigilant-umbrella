"""Action agent: dispatch approved actions through the connectors."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from .. import connectors
from .base import AgentContext


async def dispatch(ctx: AgentContext) -> dict[str, Any]:
    """Execute the plan.

    Reaching this node means either no approval was needed or a named human
    gave it -- the orchestrator will not schedule ``dispatch`` otherwise.

    Actions run sequentially on purpose. Ordering carries meaning: take the
    asset out of service *before* telling people it is safe to approach, raise
    the work order before notifying the person who has to pick it up. A failure
    part-way stops the rest rather than leaving a half-applied plan.
    """
    run = ctx.run
    results: list[dict[str, Any]] = []

    for action in run.actions:
        if action.status == "rejected":
            results.append({"action": action.id, "skipped": "rejected by approver"})
            continue
        try:
            outcome = await asyncio.wait_for(connectors.dispatch(action, run), timeout=30)
            action.status = "dispatched"
            action.result = outcome
            await ctx.log(f"dispatched {action.id}", {"action": action.id, **outcome})
        except (TimeoutError, Exception) as exc:  # noqa: BLE001
            action.status = "failed"
            action.result = {"error": str(exc), "type": type(exc).__name__}
            await ctx.log(f"dispatch failed for {action.id}", action.result)
        results.append({"action": action.id, "status": action.status, "result": action.result})

    run.finished_at = datetime.now(UTC)
    dispatched = sum(1 for a in run.actions if a.status == "dispatched")
    failed = sum(1 for a in run.actions if a.status == "failed")

    return {
        "dispatched": dispatched,
        "failed": failed,
        "rejected": sum(1 for a in run.actions if a.status == "rejected"),
        "connectors": connectors.connector_status(),
        "results": results,
    }
