"""Decision agents: risk scoring, policy, planning, output guard, approval.

From here down the pipeline is deterministic wherever it can be. The model
drafts the plan, but what the plan is *allowed* to contain, what priority the
work gets, and whether a human must sign it off are all decided by code and
config that can be unit-tested and pointed at in an audit.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .. import llm, registry
from ..config import rules_config
from ..models import PRIORITY_ORDER, PlannedAction
from ..rules import apply_rules, highest_priority
from .base import AgentContext, Halt

# Priority matrix: severity x asset criticality. Published, boring, and the
# same answer every time -- which is exactly what a maintenance planner wants
# from an automated system.
_PRIORITY_MATRIX: dict[str, dict[int, str]] = {
    "critical": {5: "P1", 4: "P1", 3: "P1", 2: "P2", 1: "P2"},
    "high":     {5: "P1", 4: "P2", 3: "P2", 2: "P3", 1: "P3"},
    "medium":   {5: "P2", 4: "P3", 3: "P3", 2: "P3", 1: "P4"},
    "low":      {5: "P3", 4: "P4", 3: "P4", 2: "P4", 1: "P4"},
}


async def risk(ctx: AgentContext) -> dict[str, Any]:
    run = ctx.run
    if run.diagnosis is None:
        raise Halt("risk scoring requires a diagnosis")

    criticality = run.asset.criticality if run.asset else 3
    base = _PRIORITY_MATRIX[run.diagnosis.severity][criticality]

    reasons = [f"severity={run.diagnosis.severity} x criticality={criticality} -> {base}"]
    priority = base
    if run.diagnosis.safety_risk and PRIORITY_ORDER[priority] > PRIORITY_ORDER["P1"]:
        priority = "P1"
        reasons.append("safety risk to personnel escalates to P1")
    if run.diagnosis.injury_reported:
        priority = "P1"
        reasons.append("injury reported escalates to P1")
    if run.diagnosis.environmental_risk and PRIORITY_ORDER[priority] > PRIORITY_ORDER["P2"]:
        priority = "P2"
        reasons.append("environmental release risk escalates to at least P2")

    run.priority = priority  # type: ignore[assignment]
    sla_hours = rules_config()["policy"]["sla_hours"].get(priority, 168)
    run.sla_due_at = run.started_at + timedelta(hours=sla_hours)

    return {
        "priority": priority,
        "base_priority": base,
        "criticality": criticality,
        "sla_hours": sla_hours,
        "sla_due_at": run.sla_due_at.isoformat(),
        "reasons": reasons,
    }


def build_rule_context(ctx: AgentContext) -> dict[str, Any]:
    """Flatten the run into the variables the rule expressions reference."""
    run = ctx.run
    d = run.diagnosis
    asset = run.asset
    spare = registry.get_spare(d.recommended_spare) if d and d.recommended_spare else None

    return {
        "asset_id": asset.id if asset else None,
        "asset_class": asset.asset_class if asset else None,
        "asset_criticality": asset.criticality if asset else 0,
        "asset_resolved": asset is not None,
        "site": asset.site if asset else None,
        "severity": d.severity if d else "medium",
        "safety_risk": bool(d and d.safety_risk),
        "environmental_risk": bool(d and d.environmental_risk),
        "injury_reported": bool(d and d.injury_reported),
        "near_miss": bool(d and d.near_miss),
        "confidence": d.confidence if d else 0.0,
        "citation_count": len(run.citations),
        "spare_required": spare is not None,
        "spare_sku": spare.sku if spare else None,
        "spare_in_stock": bool(spare and spare.stock > 0),
        "spare_lead_time_days": spare.lead_time_days if spare else 0,
        # Warranty: a simple derived fact today, a CMMS lookup tomorrow.
        "in_warranty": _in_warranty(asset.commissioned if asset else ""),
        "priority": run.priority,
    }


def _in_warranty(commissioned: str, years: int = 5) -> bool:
    if not commissioned:
        return False
    try:
        year = int(commissioned.split("-")[0])
    except (ValueError, IndexError):
        return False
    from datetime import date

    return date.today().year - year < years


async def policy(ctx: AgentContext) -> dict[str, Any]:
    run = ctx.run
    context = build_rule_context(ctx)
    cfg = rules_config()
    findings, errored = apply_rules(
        cfg["rules"], context, run.asset.asset_class if run.asset else None
    )

    if errored:
        # A rule we could not evaluate might have been the one that would have
        # stopped us. Fail closed.
        raise Halt(
            f"{len(errored)} policy rule(s) could not be evaluated",
            {"rules": errored},
        )

    run.findings = findings
    rule_priority = highest_priority(findings, run.priority)
    if PRIORITY_ORDER[rule_priority] < PRIORITY_ORDER[run.priority]:
        run.priority = rule_priority  # type: ignore[assignment]
        sla_hours = cfg["policy"]["sla_hours"].get(rule_priority, 168)
        run.sla_due_at = run.started_at + timedelta(hours=sla_hours)

    return {
        "rules_evaluated": len(cfg["rules"]),
        "findings": len(findings),
        "blockers": sum(1 for f in findings if f.severity == "blocker"),
        "priority": run.priority,
        "fired": [
            {"rule_id": f.rule_id, "severity": f.severity, "priority": f.priority,
             "message": f.message, "citation": f.citation}
            for f in findings
        ],
        "context": context,
    }


async def plan(ctx: AgentContext) -> dict[str, Any]:
    run = ctx.run
    allowed = ctx.allowed_actions
    # The rules already said what must happen; the model's job is to order it,
    # write the human-readable rationale, and drop anything redundant.
    required: list[str] = []
    for finding in run.findings:
        for action_id in finding.actions:
            if action_id not in required:
                required.append(action_id)

    findings_block = "\n".join(
        f"- {f.rule_id} [{f.severity}/{f.priority}] {f.message} (actions: {', '.join(f.actions)}) [{f.citation}]"
        for f in run.findings
    ) or "(no findings)"
    allowed_block = "\n".join(f"- {a['id']} -- {a['label']}" for a in allowed.values())

    system, user = llm.render_prompt(
        "plan",
        asset_name=run.asset.name if run.asset else "Unidentified asset",
        asset_id=run.asset.id if run.asset else "UNKNOWN",
        criticality=run.asset.criticality if run.asset else 3,
        summary=run.diagnosis.summary if run.diagnosis else "",
        severity=run.diagnosis.severity if run.diagnosis else "unknown",
        priority=run.priority,
        findings=findings_block,
        allowed_actions=allowed_block,
    )

    def simulate() -> dict[str, Any]:
        return {
            "actions": [
                {
                    "id": action_id,
                    "rationale": _rationale_for(action_id, run),
                    "source_rules": [f.rule_id for f in run.findings if action_id in f.actions],
                    "params": {"note": _note_for(run)},
                }
                for action_id in required
            ],
            "handover_note": _handover_note(run),
        }

    result = await llm.json_completion(
        system=system, user=user, required_keys=("actions",), simulate=simulate, temperature=0.2
    )

    actions: list[PlannedAction] = []
    rejected: list[str] = []
    for raw in result.data.get("actions", []) or []:
        action_id = str(raw.get("id", ""))
        spec = allowed.get(action_id)
        if spec is None:
            # The model proposed something outside its allow-list. Drop it and
            # record it -- this is the event you want alerting on in production.
            rejected.append(action_id)
            continue
        if any(a.id == action_id for a in actions):
            continue
        actions.append(
            PlannedAction(
                id=action_id,
                label=spec["label"],
                connector=spec["connector"],
                reversible=bool(spec.get("reversible", True)),
                rationale=str(raw.get("rationale", "")),
                source_rules=[str(r) for r in raw.get("source_rules", []) or []],
                params=dict(raw.get("params", {}) or {}),
            )
        )

    # Anything a blocker rule demanded is non-negotiable: if the model dropped
    # it, put it back. The model may reorder and explain; it may not veto policy.
    for action_id in required:
        if action_id in allowed and not any(a.id == action_id for a in actions):
            spec = allowed[action_id]
            actions.append(
                PlannedAction(
                    id=action_id, label=spec["label"], connector=spec["connector"],
                    reversible=bool(spec.get("reversible", True)),
                    rationale="Required by policy; reinstated after the planner omitted it.",
                    source_rules=[f.rule_id for f in run.findings if action_id in f.actions],
                    params={"note": _note_for(run)},
                )
            )

    if not actions:
        # No rule produced an action. That is a gap in the policy set, not a
        # licence to do nothing: hand it to a person and make the gap visible.
        spec = allowed["require_human_review"]
        actions.append(
            PlannedAction(
                id="require_human_review", label=spec["label"], connector=spec["connector"],
                reversible=True,
                rationale="No policy rule covered this case; routing to a human so the gap is visible.",
                source_rules=[], params={"note": _note_for(run)},
            )
        )

    run.actions = actions
    return {
        "provider": result.provider,
        "latency_ms": result.latency_ms,
        "planned": [a.id for a in actions],
        "required_by_policy": required,
        "rejected_not_allowed": rejected,
        "handover_note": result.data.get("handover_note", ""),
    }


def _rationale_for(action_id: str, run: Any) -> str:
    sources = [f for f in run.findings if action_id in f.actions]
    if sources:
        return f"{sources[0].message} ({sources[0].citation})"
    return "Proposed from the diagnosis."


def _note_for(run: Any) -> str:
    d = run.diagnosis
    parts = [d.summary if d else run.working_text]
    if d and d.recommended_spare:
        parts.append(f"Suggested spare: {d.recommended_spare}.")
    if run.citations:
        parts.append("Refs: " + "; ".join(f"{c.doc_id} {c.section}" for c in run.citations[:2]))
    return " ".join(parts)


def _handover_note(run: Any) -> str:
    asset = run.asset.name if run.asset else "an unidentified asset"
    lead = run.findings[0].message if run.findings else "No policy findings."
    due = f" Respond by {run.sla_due_at:%Y-%m-%d %H:%M UTC}." if run.sla_due_at else ""
    return f"{run.priority} on {asset}. {lead}{due}"


async def guard_output(ctx: AgentContext) -> dict[str, Any]:
    """Screen generated text and re-check every action against the allow-list."""
    run = ctx.run
    from .. import azure_ai

    generated = " ".join(
        [run.diagnosis.summary if run.diagnosis else ""]
        + [a.rationale for a in run.actions]
    )
    allowed, detail, backend = await azure_ai.screen_text(generated, direction="output")
    if not allowed:
        raise Halt("generated content rejected by the output guardrail", {"backend": backend, **detail})

    # Belt and braces: the planner already filtered, but this is the last gate
    # before anything reaches a real system, so it checks again.
    permitted = ctx.allowed_actions
    illegal = [a.id for a in run.actions if a.id not in permitted]
    if illegal:
        raise Halt("plan contains actions outside the allow-list", {"actions": illegal})

    uncited = [a.id for a in run.actions if not a.source_rules]
    return {
        "backend": backend,
        "actions_checked": len(run.actions),
        "actions_without_rule_citation": uncited,
        "screened_chars": len(generated),
    }


async def approval(ctx: AgentContext) -> dict[str, Any]:
    """Decide whether a human must sign this off before anything is dispatched.

    Fails closed in every direction: high priority, low confidence, an
    unresolved asset, an irreversible action, or a blocker finding all force a
    human into the loop. Auto-dispatch is the narrow exception, not the rule.
    """
    run = ctx.run
    cfg = rules_config()["policy"]
    threshold = cfg.get("human_approval_at_or_above", "P2")
    floor = float(cfg.get("confidence_floor", 0.55))

    reasons: list[str] = []
    if PRIORITY_ORDER[run.priority] <= PRIORITY_ORDER[threshold]:
        reasons.append(f"priority {run.priority} is at or above the {threshold} approval threshold")
    if run.diagnosis and run.diagnosis.confidence < floor:
        reasons.append(f"confidence {run.diagnosis.confidence} is below the {floor} floor")
    if run.asset is None:
        reasons.append("the asset could not be identified")
    if any(f.severity == "blocker" for f in run.findings):
        reasons.append("a blocker-severity policy rule fired")
    irreversible = [a.id for a in run.actions if not a.reversible]
    if irreversible:
        reasons.append(f"the plan contains irreversible action(s): {', '.join(irreversible)}")

    run.requires_approval = bool(reasons)
    run.approval_reason = "; ".join(reasons)

    if run.requires_approval:
        run.status = "awaiting_approval"

    return {
        "requires_approval": run.requires_approval,
        "reasons": reasons,
        "approver_group": (
            cfg.get("hse_approver_group")
            if run.diagnosis and (run.diagnosis.safety_risk or run.diagnosis.injury_reported)
            else cfg.get("default_approver_group")
        ),
        "actions_pending": [a.id for a in run.actions],
    }
