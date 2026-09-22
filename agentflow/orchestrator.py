"""The orchestrator.

Walks the node list in ``config/pipeline.yaml``, running each agent, emitting a
trace event per node, and handling retries and failure policy. It is a small,
explicit state machine rather than a framework, because the interesting part of
agent orchestration in production is not the graph -- it is what happens when a
node fails, and who is allowed to decide that it is fine to continue.

Two properties are non-negotiable and both are enforced here rather than in the
agents:

* **Fail closed.** A node marked ``fail_closed`` that raises stops the run and
  routes it to a human. Nothing is dispatched.
* **The human gate is real.** The ``dispatch`` node is unreachable while a run
  is ``awaiting_approval``. Approval is a separate call from a named person,
  and rejected actions never execute.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from .agents import AGENTS, AgentContext, Degraded, Halt
from .config import get_settings, pipeline_config
from .models import Run, RunEvent, Signal
from .store import RunStore
from .store import store as default_store

APPROVAL_NODE = "approval"
DISPATCH_NODE = "dispatch"


class Orchestrator:
    def __init__(self, store: RunStore | None = None):
        self.store = store or default_store

    # -- event helpers ------------------------------------------------------

    async def _emit(
        self, run: Run, node: str, status: str, message: str = "",
        data: dict[str, Any] | None = None, duration_ms: int | None = None,
    ) -> None:
        await self.store.publish(
            RunEvent(
                run_id=run.id, seq=self.store.next_seq(run.id), node=node,
                status=status,  # type: ignore[arg-type]
                message=message, data=data or {}, duration_ms=duration_ms,
            )
        )

    # -- public API ---------------------------------------------------------

    async def start(self, signal: Signal) -> Run:
        run = Run(signal=signal)
        nodes = pipeline_config()["nodes"]
        run.node_status = {n["id"]: "pending" for n in nodes}
        self.store.add(run)

        await self._emit(run, "run", "running", f"run started from {signal.channel}", {
            "signal_id": signal.id, "kind": signal.kind,
        })
        await self._execute(run, nodes)
        return run

    async def resume(
        self, run_id: str, *, approved: bool, approver: str, rejected_actions: list[str] | None = None,
    ) -> Run | None:
        """Apply a human decision and, if approved, run the dispatch node."""
        run = self.store.get(run_id)
        if run is None or run.status != "awaiting_approval":
            return run

        run.approved_by = approver
        rejected = set(rejected_actions or [])

        if not approved:
            for action in run.actions:
                action.status = "rejected"
            run.status = "rejected"
            run.finished_at = datetime.now(UTC)
            await self._emit(run, APPROVAL_NODE, "ok", f"rejected by {approver}", {
                "approved": False, "approver": approver,
            })
            await self._emit(run, "run", "failed", "plan rejected; no actions dispatched")
            self.store.write_audit(run)
            return run

        for action in run.actions:
            action.status = "rejected" if action.id in rejected else "approved"

        approved_count = sum(1 for a in run.actions if a.status == "approved")
        await self._emit(run, APPROVAL_NODE, "ok", f"approved by {approver}", {
            "approved": True, "approver": approver,
            "approved_actions": approved_count, "rejected_actions": sorted(rejected),
        })

        run.status = "running"
        nodes = pipeline_config()["nodes"]
        remaining = [n for n in nodes if n["id"] == DISPATCH_NODE]
        await self._execute(run, remaining)
        return run

    # -- engine -------------------------------------------------------------

    async def _execute(self, run: Run, nodes: list[dict[str, Any]]) -> None:
        pipeline = pipeline_config()

        async def log(message: str, data: dict[str, Any]) -> None:
            await self._emit(run, "log", "ok", message, data)

        ctx = AgentContext(run=run, log=log, pipeline=pipeline)
        delay = get_settings().step_delay_ms / 1000.0

        for node in nodes:
            node_id = node["id"]
            agent = AGENTS.get(node["agent"])
            if agent is None:
                await self._fail(run, node_id, f"no agent registered for {node['agent']!r}")
                return

            # The gate. Unreachable while a human decision is outstanding.
            if node_id == DISPATCH_NODE and run.status == "awaiting_approval":
                await self._emit(run, node_id, "waiting", "held for human approval")
                return

            await self._emit(run, node_id, "running", node.get("description", ""), {
                "azure_service": node.get("azure_service"),
            })
            if delay:
                await asyncio.sleep(delay)

            started = time.perf_counter()
            attempts = int(node.get("retries", 0)) + 1
            last_error: Exception | None = None

            for attempt in range(1, attempts + 1):
                try:
                    detail = await agent(ctx)
                    await self._emit(
                        run, node_id, "ok", "", {**detail, "attempt": attempt},
                        duration_ms=int((time.perf_counter() - started) * 1000),
                    )
                    last_error = None
                    break
                except Degraded as exc:
                    await self._emit(
                        run, node_id, "degraded", str(exc), exc.detail,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                    )
                    last_error = None
                    break
                except Halt as exc:
                    await self._emit(
                        run, node_id, "failed", str(exc), exc.detail,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                    )
                    await self._halt(run, f"{node_id}: {exc}")
                    return
                except Exception as exc:  # noqa: BLE001 - the engine is the backstop
                    last_error = exc
                    if attempt < attempts:
                        await self._emit(run, node_id, "running", f"attempt {attempt} failed, retrying", {
                            "error": str(exc), "type": type(exc).__name__,
                        })
                        await asyncio.sleep(0.2 * attempt)

            if last_error is not None:
                on_error = node.get("on_error", "fail_closed")
                detail = {"error": str(last_error), "type": type(last_error).__name__, "attempts": attempts}
                if on_error == "degrade":
                    await self._emit(run, node_id, "degraded", str(last_error), detail)
                    continue
                await self._emit(run, node_id, "failed", str(last_error), detail)
                await self._halt(run, f"{node_id}: {last_error}")
                return

            # The approval node may have parked the run.
            if node_id == APPROVAL_NODE and run.status == "awaiting_approval":
                await self._emit(run, "run", "waiting", "awaiting human approval", {
                    "reason": run.approval_reason, "priority": run.priority,
                })
                self.store.write_audit(run)
                return

        if run.status == "running":
            run.status = "completed"
            run.finished_at = datetime.now(UTC)
            await self._emit(run, "run", "ok", "run completed", {
                "priority": run.priority,
                "actions": len(run.actions),
                "auto_dispatched": not run.requires_approval,
            })
            self.store.write_audit(run)

    async def _fail(self, run: Run, node_id: str, message: str) -> None:
        await self._emit(run, node_id, "failed", message)
        await self._halt(run, message)

    async def _halt(self, run: Run, reason: str) -> None:
        """Stop the run. Nothing is dispatched; a person picks it up."""
        run.status = "failed"
        run.error = reason
        run.finished_at = datetime.now(UTC)
        run.requires_approval = True
        run.approval_reason = f"run halted: {reason}"
        await self._emit(run, "run", "failed", reason, {"fail_closed": True})
        self.store.write_audit(run)


orchestrator = Orchestrator()
