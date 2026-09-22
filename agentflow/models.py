"""Canonical domain model.

Everything that crosses a boundary -- channel, agent, connector, HTTP -- is one
of these. Agents never pass raw dicts around, so a change to the shape of the
data is a type error rather than a 3am surprise.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low"]
Priority = Literal["P1", "P2", "P3", "P4"]
NodeStatus = Literal["pending", "running", "ok", "degraded", "failed", "skipped", "waiting"]
RunStatus = Literal["running", "awaiting_approval", "completed", "failed", "rejected"]

PRIORITY_ORDER: dict[str, int] = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


def _now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Signal(BaseModel):
    """An inbound maintenance signal, whatever channel it arrived on."""

    id: str = Field(default_factory=lambda: new_id("SIG"))
    channel: Literal["dashboard", "teams", "power_automate", "email", "sensor", "api"] = "api"
    kind: Literal["inspection_finding", "sensor_alert", "operator_request"] = "operator_request"
    text: str
    asset_hint: str | None = None
    reported_by: str | None = None
    reported_at: datetime = Field(default_factory=_now)
    # Telemetry accompanying a sensor alert, e.g. {"vibration_mm_s": 12.4}.
    readings: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Asset(BaseModel):
    id: str
    name: str
    asset_class: str = Field(alias="class")
    manufacturer: str = ""
    model: str = ""
    site: str = ""
    location: str = ""
    criticality: int = 3
    commissioned: str = ""
    aliases: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class Spare(BaseModel):
    sku: str
    description: str
    asset_classes: list[str] = Field(default_factory=list)
    stock: int = 0
    lead_time_days: int = 0


class Citation(BaseModel):
    """A pointer back to the source that justified a statement.

    Nothing the copilot asserts about equipment is allowed to travel without
    one of these -- see ``AI Assurance Policy s3.2`` in ``config/rules.yaml``.
    """

    doc_id: str
    title: str
    section: str
    snippet: str = ""
    score: float = 0.0


class FailureMode(BaseModel):
    name: str
    likelihood: float = 0.0
    rationale: str = ""
    citations: list[Citation] = Field(default_factory=list)


class Diagnosis(BaseModel):
    summary: str = ""
    failure_modes: list[FailureMode] = Field(default_factory=list)
    severity: Severity = "medium"
    safety_risk: bool = False
    environmental_risk: bool = False
    injury_reported: bool = False
    near_miss: bool = False
    confidence: float = 0.0
    recommended_spare: str | None = None
    citations: list[Citation] = Field(default_factory=list)


class Finding(BaseModel):
    """One fired policy rule."""

    rule_id: str
    severity: Literal["blocker", "warning", "info"]
    priority: Priority
    message: str
    actions: list[str] = Field(default_factory=list)
    citation: str


class PlannedAction(BaseModel):
    id: str
    label: str
    connector: str
    reversible: bool = True
    rationale: str = ""
    source_rules: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    status: Literal["planned", "approved", "rejected", "dispatched", "failed"] = "planned"
    result: dict[str, Any] | None = None


class RunEvent(BaseModel):
    """One line of the live trace. Streamed to the dashboard over SSE."""

    run_id: str
    seq: int
    node: str
    status: NodeStatus
    message: str = ""
    at: datetime = Field(default_factory=_now)
    duration_ms: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("RUN"))
    signal: Signal
    status: RunStatus = "running"
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None

    asset: Asset | None = None
    asset_confidence: float = 0.0
    asset_candidates: list[dict[str, Any]] = Field(default_factory=list)

    language: str = "en"
    redactions: int = 0
    working_text: str = ""

    diagnosis: Diagnosis | None = None
    citations: list[Citation] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    priority: Priority = "P4"
    sla_due_at: datetime | None = None

    actions: list[PlannedAction] = Field(default_factory=list)
    requires_approval: bool = True
    approval_reason: str = ""
    approved_by: str | None = None

    node_status: dict[str, NodeStatus] = Field(default_factory=dict)
    events: list[RunEvent] = Field(default_factory=list)
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        """Compact form for list views and connector payloads."""
        return {
            "run_id": self.id,
            "status": self.status,
            "priority": self.priority,
            "asset_id": self.asset.id if self.asset else None,
            "asset_name": self.asset.name if self.asset else None,
            "severity": self.diagnosis.severity if self.diagnosis else None,
            "confidence": self.diagnosis.confidence if self.diagnosis else 0.0,
            "summary": self.diagnosis.summary if self.diagnosis else "",
            "findings": len(self.findings),
            "actions": len(self.actions),
            "started_at": self.started_at.isoformat(),
            "text": self.signal.text[:160],
        }
