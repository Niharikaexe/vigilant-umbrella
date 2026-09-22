"""Power Automate connector.

Posts the run to a "When an HTTP request is received" trigger. From there the
flow owner can fan out to anything Power Platform reaches -- Outlook, SharePoint,
Dataverse, Planner, an approval -- without this service needing a connector for
each. That is the whole reason to integrate at the Power Platform boundary
rather than point-to-point.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import get_settings
from ..models import PlannedAction, Run
from .base import Connector


def build_payload(run: Run, action: PlannedAction) -> dict[str, Any]:
    """Flat, stable payload -- Power Automate's schema designer is happier with
    flat JSON, and a flat contract is easier to keep backwards compatible."""
    d = run.diagnosis
    return {
        "runId": run.id,
        "actionId": action.id,
        "actionLabel": action.label,
        "priority": run.priority,
        "slaDueAt": run.sla_due_at.isoformat() if run.sla_due_at else None,
        "assetId": run.asset.id if run.asset else None,
        "assetName": run.asset.name if run.asset else None,
        "assetClass": run.asset.asset_class if run.asset else None,
        "site": run.asset.site if run.asset else None,
        "criticality": run.asset.criticality if run.asset else None,
        "severity": d.severity if d else None,
        "confidence": round(d.confidence, 3) if d else 0.0,
        "summary": d.summary if d else "",
        "safetyRisk": d.safety_risk if d else False,
        "environmentalRisk": d.environmental_risk if d else False,
        "rationale": action.rationale,
        "sourceRules": ",".join(action.source_rules),
        "findings": [{"ruleId": f.rule_id, "message": f.message, "citation": f.citation} for f in run.findings],
        "citations": [{"docId": c.doc_id, "section": c.section} for c in run.citations],
        "note": action.params.get("note", ""),
        "reportedBy": run.signal.reported_by,
        "reportedAt": run.signal.reported_at.isoformat(),
    }


class PowerAutomateConnector(Connector):
    name = "power_automate"

    @property
    def configured(self) -> bool:
        return bool(get_settings().power_automate_url)

    async def dispatch(self, action: PlannedAction, run: Run) -> dict[str, Any]:
        payload = build_payload(run, action)
        if not self.configured:
            return {"mode": "simulated", "channel": "power_automate", "payload_keys": sorted(payload)[:8]}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(get_settings().power_automate_url, json=payload)
            resp.raise_for_status()
        return {"mode": "live", "channel": "power_automate", "status_code": resp.status_code}
