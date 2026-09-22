"""CMMS / EAM connector (SAP PM, Maximo, Ultimo, Fiix, ...).

Creates work orders, purchase requests and asset status changes. Without
``ERP_API_BASE_URL`` configured it mints a deterministic local reference so the
rest of the pipeline -- and the demo -- behaves identically.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from ..config import get_settings
from ..models import PlannedAction, Run
from .base import Connector

_ENDPOINTS = {
    "create_work_order": ("POST", "/workorders"),
    "raise_purchase_request": ("POST", "/purchase-requests"),
    "freeze_asset": ("POST", "/assets/{asset_id}/out-of-service"),
    "require_permit_to_work": ("POST", "/permits"),
    "hold_inhouse_repair": ("POST", "/workorders/hold"),
    "log_only": ("POST", "/condition-log"),
}


def _reference(prefix: str, run: Run, action_id: str) -> str:
    digest = hashlib.sha256(f"{run.id}:{action_id}".encode()).hexdigest()[:6].upper()
    return f"{prefix}-{digest}"


class CmmsConnector(Connector):
    name = "cmms"

    @property
    def configured(self) -> bool:
        return bool(get_settings().erp_base_url)

    async def dispatch(self, action: PlannedAction, run: Run) -> dict[str, Any]:
        method, path = _ENDPOINTS.get(action.id, ("POST", "/workorders"))
        asset_id = run.asset.id if run.asset else "UNKNOWN"
        path = path.format(asset_id=asset_id)
        body = {
            "assetId": asset_id,
            "priority": run.priority,
            "description": action.params.get("note") or action.rationale,
            "raisedBy": "agentflow",
            "sourceRunId": run.id,
            "sourceRules": action.source_rules,
            "slaDueAt": run.sla_due_at.isoformat() if run.sla_due_at else None,
            "spareSku": run.diagnosis.recommended_spare if run.diagnosis else None,
        }

        prefix = {"raise_purchase_request": "PR", "require_permit_to_work": "PTW"}.get(action.id, "WO")
        if not self.configured:
            return {"mode": "simulated", "channel": "cmms", "reference": _reference(prefix, run, action.id), "body": body}

        s = get_settings()
        headers = {"Authorization": f"Bearer {s.erp_api_key}"} if s.erp_api_key else {}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, f"{s.erp_base_url.rstrip('/')}{path}", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
        return {"mode": "live", "channel": "cmms", "reference": data.get("id", "unknown"), "status_code": resp.status_code}
