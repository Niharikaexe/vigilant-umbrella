"""Run storage and the live event bus.

In-memory by design for a prototype, behind a narrow interface so swapping in
Postgres, Cosmos DB or Dataverse is a single-file change. Runs are also
appended to a JSONL file so a restart does not lose the audit trail -- for a
system that takes real actions on real equipment, "we lost the reasoning" is
not an acceptable answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .config import ROOT
from .models import Run, RunEvent

AUDIT_DIR = ROOT / "data" / "runs"
GLOBAL_TOPIC = "*"


class RunStore:
    def __init__(self, max_runs: int = 500, persist: bool = True):
        self._runs: dict[str, Run] = {}
        self._order: deque[str] = deque(maxlen=max_runs)
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent]]] = defaultdict(set)
        self._persist = persist
        self._seq: dict[str, int] = defaultdict(int)

    # -- runs ---------------------------------------------------------------

    def add(self, run: Run) -> None:
        if len(self._order) == self._order.maxlen and self._order:
            evicted = self._order[0]
            self._runs.pop(evicted, None)
        self._runs[run.id] = run
        self._order.append(run.id)

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list(self, limit: int = 50) -> list[Run]:
        return [self._runs[rid] for rid in reversed(self._order) if rid in self._runs][:limit]

    def stats(self) -> dict[str, Any]:
        runs = list(self._runs.values())
        by_priority: dict[str, int] = defaultdict(int)
        by_status: dict[str, int] = defaultdict(int)
        for r in runs:
            by_priority[r.priority] += 1
            by_status[r.status] += 1
        dispatched = sum(1 for r in runs for a in r.actions if a.status == "dispatched")
        return {
            "runs": len(runs),
            "by_priority": dict(by_priority),
            "by_status": dict(by_status),
            "actions_dispatched": dispatched,
            "awaiting_approval": by_status.get("awaiting_approval", 0),
        }

    # -- events -------------------------------------------------------------

    def next_seq(self, run_id: str) -> int:
        self._seq[run_id] += 1
        return self._seq[run_id]

    async def publish(self, event: RunEvent) -> None:
        run = self._runs.get(event.run_id)
        if run is not None:
            run.events.append(event)
            if event.node not in ("run", "log"):
                run.node_status[event.node] = event.status

        for topic in (event.run_id, GLOBAL_TOPIC):
            for queue in list(self._subscribers.get(topic, ())):
                # Never let one slow SSE client stall the pipeline.
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    def subscribe(self, topic: str = GLOBAL_TOPIC) -> asyncio.Queue[RunEvent]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=256)
        self._subscribers[topic].add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[RunEvent], topic: str = GLOBAL_TOPIC) -> None:
        self._subscribers[topic].discard(queue)

    # -- audit --------------------------------------------------------------

    def write_audit(self, run: Run) -> None:
        if not self._persist:
            return
        try:
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            path = Path(AUDIT_DIR) / f"{run.started_at:%Y-%m-%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_audit_record(run), default=str) + "\n")
        except OSError:
            # Losing the audit file must not take the service down; the run is
            # still in memory and the failure is visible in the logs.
            pass


def _audit_record(run: Run) -> dict[str, Any]:
    """What an auditor actually needs: the decision and its justification."""
    return {
        "run_id": run.id,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "status": run.status,
        "channel": run.signal.channel,
        "signal_text": run.signal.text,
        "language": run.language,
        "redactions": run.redactions,
        "asset_id": run.asset.id if run.asset else None,
        "asset_confidence": run.asset_confidence,
        "severity": run.diagnosis.severity if run.diagnosis else None,
        "confidence": run.diagnosis.confidence if run.diagnosis else None,
        "priority": run.priority,
        "citations": [f"{c.doc_id} {c.section}" for c in run.citations],
        "findings": [{"rule": f.rule_id, "citation": f.citation, "message": f.message} for f in run.findings],
        "requires_approval": run.requires_approval,
        "approval_reason": run.approval_reason,
        "approved_by": run.approved_by,
        "actions": [
            {"id": a.id, "status": a.status, "rules": a.source_rules, "result": a.result}
            for a in run.actions
        ],
        "error": run.error,
    }


store = RunStore()
