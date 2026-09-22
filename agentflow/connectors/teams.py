"""Microsoft Teams connector.

Posts an Adaptive Card to an incoming webhook (or a Power Automate "When a
Teams webhook request is received" trigger). The card carries the diagnosis,
the citations and the priority, so the duty engineer gets the *reasoning*, not
just an alert -- an alert without a reason gets ignored by the third week.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import get_settings
from ..models import PlannedAction, Run
from .base import Connector

_PRIORITY_COLOUR = {"P1": "attention", "P2": "warning", "P3": "accent", "P4": "good"}


def build_adaptive_card(run: Run, action: PlannedAction) -> dict[str, Any]:
    d = run.diagnosis
    facts = [
        {"title": "Asset", "value": f"{run.asset.name} ({run.asset.id})" if run.asset else "Unresolved"},
        {"title": "Priority", "value": run.priority},
        {"title": "Severity", "value": d.severity if d else "unknown"},
        {"title": "Confidence", "value": f"{d.confidence:.0%}" if d else "n/a"},
    ]
    if run.sla_due_at:
        facts.append({"title": "Respond by", "value": run.sla_due_at.strftime("%Y-%m-%d %H:%M UTC")})

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock", "size": "Large", "weight": "Bolder",
            "text": action.label, "color": _PRIORITY_COLOUR.get(run.priority, "default"),
        },
        {"type": "TextBlock", "text": d.summary if d else run.signal.text, "wrap": True},
        {"type": "FactSet", "facts": facts},
    ]
    if run.findings:
        body.append({
            "type": "TextBlock", "weight": "Bolder", "text": "Policy findings", "spacing": "Medium",
        })
        for f in run.findings[:4]:
            body.append({
                "type": "TextBlock", "wrap": True, "spacing": "None",
                "text": f"- **{f.rule_id}** {f.message}\n\n  _{f.citation}_",
            })
    if run.citations:
        body.append({
            "type": "TextBlock", "isSubtle": True, "wrap": True, "spacing": "Medium",
            "text": "Sources: " + "; ".join(f"{c.doc_id} {c.section}" for c in run.citations[:3]),
        })

    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
            },
        }],
    }


class TeamsConnector(Connector):
    name = "teams"

    @property
    def configured(self) -> bool:
        return bool(get_settings().teams_webhook_url)

    async def dispatch(self, action: PlannedAction, run: Run) -> dict[str, Any]:
        card = build_adaptive_card(run, action)
        if not self.configured:
            return {"mode": "simulated", "channel": "teams", "card_preview": card["attachments"][0]["content"]["body"][0]["text"]}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(get_settings().teams_webhook_url, json=card)
            resp.raise_for_status()
        return {"mode": "live", "channel": "teams", "status_code": resp.status_code}
