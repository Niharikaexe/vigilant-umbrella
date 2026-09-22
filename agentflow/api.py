"""HTTP API and SSE stream.

The API is the integration seam. Copilot Studio, Power Automate, n8n and the
built-in dashboard are all just clients of these endpoints -- which is the whole
architectural argument: keep the agent logic here, where it can be versioned and
tested, and let the low-code tools do what they are genuinely good at (triggers,
connectors, approvals, and reaching into Microsoft 365).

The generated OpenAPI document at ``/openapi.json`` is what you import to create
a Power Platform custom connector.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import azure_ai, connectors, registry
from .config import SAMPLES_DIR, WEB_DIR, get_settings, pipeline_config, reload_config, rules_config
from .models import Run, Signal
from .orchestrator import orchestrator
from .store import GLOBAL_TOPIC, store

app = FastAPI(
    title="AgentFlow - Maintenance & Asset Operations Copilot",
    version="0.1.0",
    description=(
        "Turns an unstructured maintenance signal into a grounded diagnosis, a "
        "policy-checked priority and a bounded action plan. Import this document "
        "as a Power Platform custom connector to call it from Copilot Studio or "
        "Power Automate."
    ),
)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Shared-secret auth on write endpoints.

    Disabled when ``AGENTFLOW_API_KEY`` is unset so the demo runs with no setup;
    the moment a key is configured it is enforced. In Azure this sits behind API
    Management or Entra ID and the key goes in Key Vault -- never in the repo.
    """
    expected = get_settings().api_key
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class SignalRequest(BaseModel):
    """A maintenance signal from any channel."""

    text: str = Field(..., description="What was reported, in free text.", min_length=3)
    kind: str = Field("operator_request", description="inspection_finding | sensor_alert | operator_request")
    channel: str = Field("api", description="dashboard | teams | power_automate | email | sensor | api")
    asset_hint: str | None = Field(None, description="Optional asset id or name if the caller already knows it.")
    reported_by: str | None = Field(None, description="Who reported it. Redacted before any model sees it.")
    readings: dict[str, float] = Field(default_factory=dict, description="Telemetry, e.g. {\"vibration_mm_s\": 12.4}")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    approved: bool = Field(..., description="True to dispatch the plan, false to reject it entirely.")
    approver: str = Field(..., description="Who is approving. Recorded in the audit trail.", min_length=1)
    rejected_actions: list[str] = Field(
        default_factory=list, description="Action ids to drop while approving the rest."
    )


class RunAccepted(BaseModel):
    run_id: str
    status: str
    stream_url: str


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def run_detail(run: Run) -> dict[str, Any]:
    d = run.diagnosis
    return {
        "run_id": run.id,
        "status": run.status,
        "priority": run.priority,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "sla_due_at": run.sla_due_at.isoformat() if run.sla_due_at else None,
        "signal": {
            "id": run.signal.id, "text": run.signal.text, "kind": run.signal.kind,
            "channel": run.signal.channel, "reported_by": run.signal.reported_by,
            "readings": run.signal.readings,
        },
        "working_text": run.working_text,
        "language": run.language,
        "redactions": run.redactions,
        "asset": run.asset.model_dump(by_alias=True) if run.asset else None,
        "asset_confidence": run.asset_confidence,
        "asset_candidates": run.asset_candidates,
        "diagnosis": {
            "summary": d.summary,
            "severity": d.severity,
            "confidence": d.confidence,
            "safety_risk": d.safety_risk,
            "environmental_risk": d.environmental_risk,
            "injury_reported": d.injury_reported,
            "near_miss": d.near_miss,
            "recommended_spare": d.recommended_spare,
            "failure_modes": [
                {"name": m.name, "likelihood": m.likelihood, "rationale": m.rationale,
                 "citations": [c.model_dump() for c in m.citations]}
                for m in d.failure_modes
            ],
        } if d else None,
        "citations": [c.model_dump() for c in run.citations],
        "findings": [f.model_dump() for f in run.findings],
        "actions": [a.model_dump() for a in run.actions],
        "requires_approval": run.requires_approval,
        "approval_reason": run.approval_reason,
        "approved_by": run.approved_by,
        "node_status": run.node_status,
        "events": [
            {"seq": e.seq, "node": e.node, "status": e.status, "message": e.message,
             "at": e.at.isoformat(), "duration_ms": e.duration_ms, "data": e.data}
            for e in run.events
        ],
        "error": run.error,
    }


# --------------------------------------------------------------------------
# Metadata endpoints
# --------------------------------------------------------------------------


@app.get("/healthz", tags=["meta"], summary="Liveness probe")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "version": app.version}


@app.get("/api/config", tags=["meta"], summary="Pipeline graph, service modes and demo samples")
async def get_config() -> dict[str, Any]:
    pipeline = pipeline_config()
    settings = get_settings()
    samples: list[dict[str, Any]] = []
    sample_path = SAMPLES_DIR / "signals.json"
    if sample_path.exists():
        samples = json.loads(sample_path.read_text(encoding="utf-8"))

    return {
        "pipeline": {
            "name": pipeline.get("name"),
            "nodes": [
                {"id": n["id"], "label": n["label"], "description": n.get("description", ""),
                 "azure_service": n.get("azure_service"), "on_error": n.get("on_error", "fail_closed")}
                for n in pipeline["nodes"]
            ],
            "allowed_actions": pipeline.get("allowed_actions", []),
        },
        "azure_services": list(settings.service_status().values()),
        "connectors": connectors.connector_status(),
        "llm_provider": settings.llm_provider,
        "policy": rules_config()["policy"],
        "rules": [
            {"id": r["id"], "description": r.get("description", ""), "severity": r.get("severity"),
             "priority": r.get("priority"), "citation": r.get("citation")}
            for r in rules_config()["rules"]
        ],
        "assets": [a.model_dump(by_alias=True) for a in registry.get_assets()],
        "spares": [s.model_dump() for s in registry.get_spares()],
        "samples": samples,
    }


@app.get("/api/stats", tags=["meta"], summary="Aggregate run statistics")
async def get_stats() -> dict[str, Any]:
    return store.stats()


@app.post("/api/config/reload", tags=["meta"], summary="Reload YAML config without a restart",
          dependencies=[Depends(require_api_key)])
async def post_reload() -> dict[str, str]:
    reload_config()
    registry.get_assets.cache_clear()
    registry.get_spares.cache_clear()
    return {"status": "reloaded"}


# --------------------------------------------------------------------------
# Core endpoints
# --------------------------------------------------------------------------


@app.post("/api/signals", response_model=RunAccepted, status_code=202, tags=["runs"],
          summary="Submit a maintenance signal",
          dependencies=[Depends(require_api_key)])
async def post_signal(request: SignalRequest, background: BackgroundTasks) -> RunAccepted:
    """Accept a signal and start a run.

    Returns immediately with a run id; the pipeline runs in the background and
    streams its trace over SSE. Callers that want the finished result poll
    ``GET /api/runs/{run_id}`` -- which is what the Power Automate flow does,
    since Power Automate is happier polling than holding a long connection.
    """
    signal = Signal(
        text=request.text,
        kind=request.kind,  # type: ignore[arg-type]
        channel=request.channel,  # type: ignore[arg-type]
        asset_hint=request.asset_hint,
        reported_by=request.reported_by,
        readings=request.readings,
        metadata=request.metadata,
    )
    run = Run(signal=signal)
    run.node_status = {n["id"]: "pending" for n in pipeline_config()["nodes"]}
    store.add(run)

    async def execute() -> None:
        with suppress(Exception):
            await orchestrator._execute(run, pipeline_config()["nodes"])  # noqa: SLF001

    background.add_task(execute)
    return RunAccepted(run_id=run.id, status="running", stream_url=f"/api/stream?run_id={run.id}")


@app.post("/api/signals/sync", tags=["runs"], summary="Submit a signal and wait for the result",
          dependencies=[Depends(require_api_key)])
async def post_signal_sync(request: SignalRequest) -> dict[str, Any]:
    """Blocking variant for callers that cannot poll -- Copilot Studio actions,
    n8n HTTP nodes, curl."""
    signal = Signal(
        text=request.text, kind=request.kind, channel=request.channel,  # type: ignore[arg-type]
        asset_hint=request.asset_hint, reported_by=request.reported_by,
        readings=request.readings, metadata=request.metadata,
    )
    run = await orchestrator.start(signal)
    return run_detail(run)


@app.get("/api/runs", tags=["runs"], summary="List recent runs")
async def get_runs(limit: int = 50) -> dict[str, Any]:
    return {"runs": [r.summary() for r in store.list(limit)]}


@app.get("/api/runs/{run_id}", tags=["runs"], summary="Get one run in full")
async def get_run(run_id: str) -> dict[str, Any]:
    run = store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run_detail(run)


@app.post("/api/runs/{run_id}/approve", tags=["runs"], summary="Approve or reject a held plan",
          dependencies=[Depends(require_api_key)])
async def post_approval(run_id: str, request: ApprovalRequest) -> dict[str, Any]:
    run = store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail=f"run is {run.status}, not awaiting approval")

    updated = await orchestrator.resume(
        run_id, approved=request.approved, approver=request.approver,
        rejected_actions=request.rejected_actions,
    )
    return run_detail(updated) if updated else {}


@app.post("/api/documents", tags=["runs"], summary="Upload an inspection report",
          dependencies=[Depends(require_api_key)])
async def post_document(
    file: UploadFile = File(..., description="A scanned or text inspection report."),
    asset_hint: str | None = Form(None),
) -> dict[str, Any]:
    """Read a document with Azure AI Document Intelligence and run it as a signal."""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")
    text, backend = await azure_ai.extract_document_text(content, file.content_type or "application/octet-stream")
    if not text.strip():
        raise HTTPException(status_code=422, detail="no text could be extracted from the document")

    signal = Signal(text=text.strip()[:6000], kind="inspection_finding", channel="dashboard",
                    asset_hint=asset_hint, metadata={"filename": file.filename, "ocr_backend": backend})
    run = await orchestrator.start(signal)
    return {"ocr_backend": backend, **run_detail(run)}


# --------------------------------------------------------------------------
# Live stream
# --------------------------------------------------------------------------


@app.get("/api/stream", tags=["runs"], summary="Server-sent event stream of the live trace")
async def stream(run_id: str | None = None) -> StreamingResponse:
    topic = run_id or GLOBAL_TOPIC

    async def generator():
        queue = store.subscribe(topic)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    # Comment frame: keeps proxies and browsers from closing an
                    # idle connection.
                    yield ": keepalive\n\n"
                    continue
                payload = {
                    "run_id": event.run_id, "seq": event.seq, "node": event.node,
                    "status": event.status, "message": event.message,
                    "duration_ms": event.duration_ms, "at": event.at.isoformat(),
                    "data": event.data,
                }
                yield f"data: {json.dumps(payload, default=str)}\n\n"
        finally:
            store.unsubscribe(queue, topic)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(str(WEB_DIR / "index.html"))
