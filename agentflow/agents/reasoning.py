"""Reasoning agents: asset resolution, retrieval, diagnosis.

The diagnosis agent is the only place a model is asked to *judge* anything, and
even there it is fenced in: it may only reason from retrieved manual sections,
it must cite them, and its output is a closed schema. Everything downstream
(priority, policy, approval) is deterministic.

The ``_simulate_*`` functions are not stubs. They are a real rule-based
implementation over the same retrieved passages, so the pipeline produces
sensible, defensible output with no cloud account attached -- which is what
makes the demo runnable and the tests hermetic.
"""

from __future__ import annotations

import functools
import re
from typing import Any

from .. import llm, registry, retrieval
from ..models import Citation, Diagnosis, FailureMode
from .base import AgentContext, Degraded, Halt

# --------------------------------------------------------------------------
# Asset resolution
# --------------------------------------------------------------------------


async def resolve_asset(ctx: AgentContext) -> dict[str, Any]:
    run = ctx.run
    asset, confidence, candidates = registry.resolve(run.working_text, run.signal.asset_hint)
    run.asset_candidates = candidates

    if asset is None and candidates:
        # Cheap paths were inconclusive; now it is worth a model call.
        await ctx.log("asset ambiguous, asking the model to disambiguate", {"candidates": candidates})
        system, user = llm.render_prompt(
            "resolve_asset", registry=registry.registry_digest(), text=run.working_text
        )
        result = await llm.json_completion(
            system=system, user=user,
            required_keys=("asset_id", "confidence"),
            simulate=lambda: {
                # Report the lexical score as-is. If it is weak, the floor above
                # rejects it -- the simulator does not get to launder a guess
                # into a confident answer.
                "asset_id": candidates[0]["asset_id"] if candidates else None,
                "confidence": candidates[0]["score"] if candidates else 0.0,
                "candidates": [c["asset_id"] for c in candidates],
                "reasoning": "highest lexical overlap with the registry (simulated)",
            },
        )
        picked = result.data.get("asset_id")
        picked_confidence = float(result.data.get("confidence", 0.0) or 0.0)
        if picked and picked_confidence >= _ASSET_CONFIDENCE_FLOOR:
            asset = registry.get_asset(picked)
            confidence = picked_confidence
        else:
            # Weak pick: stay unresolved rather than act on a guess.
            await ctx.log("disambiguation below the confidence floor; leaving unresolved", {
                "picked": picked, "confidence": picked_confidence, "floor": _ASSET_CONFIDENCE_FLOOR,
            })
            confidence = picked_confidence

    run.asset = asset
    run.asset_confidence = round(float(confidence), 3)

    if asset is None:
        # Not fatal: the policy engine has a rule (AIQ-002) that refuses to
        # auto-action an unidentified asset. Let policy decide, not this agent.
        raise Degraded(
            "could not match the signal to a registry asset",
            {"confidence": run.asset_confidence, "candidates": candidates},
        )

    return {
        "asset_id": asset.id,
        "asset_name": asset.name,
        "asset_class": asset.asset_class,
        "criticality": asset.criticality,
        "site": asset.site,
        "confidence": run.asset_confidence,
        "candidates": candidates,
    }


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------


async def retrieve(ctx: AgentContext) -> dict[str, Any]:
    run = ctx.run
    asset_class = run.asset.asset_class if run.asset else None
    citations, backend = await retrieval.search(run.working_text, asset_class, top_k=4)
    run.citations = citations

    if not citations:
        raise Degraded(
            "no manual sections matched this signal",
            {"backend": backend, "asset_class": asset_class},
        )
    return {
        "backend": backend,
        "count": len(citations),
        "sections": [f"{c.doc_id} {c.section}" for c in citations],
        "top_score": citations[0].score,
    }


# --------------------------------------------------------------------------
# Diagnosis
# --------------------------------------------------------------------------

# Phrases that, in a *manual*, mean the equipment is dangerous right now.
_MANUAL_SAFETY_MARKERS = (
    "safety critical", "safety-critical", "unsafe to operate", "must not be operated",
    "taken out of service immediately", "must be taken out of service", "lock out",
    "snap-back", "lethal", "statutory", "exposure limits", "drop under gravity",
    "no lifting operation may continue", "must stop", "immediate shutdown",
    "primary safety protection", "severe safety hazard",
)
_MANUAL_ENV_MARKERS = (
    "environmental release", "release to water", "spill", "contaminates",
    "oily water", "fire risk", "oil release",
)

# What an operator writes, graded. Ordered most severe first; first hit wins.
_SIGNAL_SEVERITY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("critical", (
        "will not start", "won't start", "wont start", "failed to start", "no start",
        "seized", "snapped", "parted", "fire", "smoke", "cracked", "crack",
        "stopped completely", "total failure", "drops the load",
        # "drifts" alone is ambiguous: a crane load drifting down is critical,
        # a weld seam drifting out of tolerance is not. Qualify it.
        "load drifts", "drifts down", "drifting down", "drift under load",
        "bypassed", "does not stop", "doesn't stop", "not stopping", "unresponsive",
        "e-stop", "emergency stop fault", "broke", "collapsed",
    )),
    ("high", (
        "leaking", "leak", "leaks", "overheating", "overheat", "alarm", "tripped",
        "trip", "grinding", "loss of pressure", "pressure loss", "won't hold",
        "smells burning", "burning smell", "sparking", "torn", "worn through",
        "out of service", "cannot", "can't", "out of tolerance", "quality escape",
        "contaminated", "bypassed",
    )),
    ("medium", (
        "noise", "noisy", "knocking", "rattling", "vibration", "vibrating", "juddering",
        "weeping", "seeping", "intermittent", "slow", "sluggish", "warm", "hot",
        "deviation", "drifting slightly", "occasionally",
    )),
    ("low", (
        "cosmetic", "scratch", "paint", "label", "minor", "slight", "monitoring",
        "trending", "routine", "for information",
    )),
)

_INJURY_MARKERS = (
    "injured", "injury", "hurt", "burned", "burnt", "crushed", "struck by",
    "hit by", "taken to hospital", "first aid", "laceration", "fracture",
)
_NEAR_MISS_MARKERS = ("near miss", "near-miss", "nearly hit", "almost hit", "close call", "narrowly")
_ENV_SIGNAL_MARKERS = (
    "spill", "spilled", "into the water", "overboard", "on the ground",
    "on the floor", "reached the deck", "contaminated",
)

# Telemetry thresholds. In production these come from the asset's condition
# monitoring policy; here they are the manual's published limits.
_READING_THRESHOLDS: tuple[tuple[str, float, float], ...] = (
    ("vibration", 7.1, 11.0),
    ("temperature", 90.0, 98.0),
    ("temp", 90.0, 98.0),
    ("pressure_drop", 1.5, 3.0),
    ("hours_since_service", 8000.0, 12000.0),
)

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@functools.lru_cache(maxsize=512)
def _marker_re(marker: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(marker)}\b", re.I)


def _has(text: str, markers: tuple[str, ...]) -> list[str]:
    """Word-boundary marker matching.

    Substring matching looks fine until "hot" fires on "photo" and "broke" on
    "broken" -- both silently shift the severity of a real report.
    """
    return [m for m in markers if _marker_re(m).search(text)]
# Retrieval scores are normalised so the best hit is 1.0. Only sections
# effectively tied with it count as evidence of a hazard.
_EVIDENCE_TIE_FLOOR = 0.97
# Below this normalised retrieval score a section is a source, not a candidate
# explanation for the fault.
_FAILURE_MODE_FLOOR = 0.30
# Below this, an asset match is a guess. Guessing the asset is worse than
# admitting we do not know: policy rule AIQ-002 routes the unknown case to a
# human, and that is the behaviour we want.
_ASSET_CONFIDENCE_FLOOR = 0.45
_SKU_RE = re.compile(r"\b[A-Z]{3,4}[-][A-Z0-9-]{2,}\b")


def _worst(a: str, b: str) -> str:
    return a if _SEVERITY_RANK[a] >= _SEVERITY_RANK[b] else b


def _severity_from_text(text: str) -> tuple[str, list[str]]:
    lowered = text.lower()
    for level, markers in _SIGNAL_SEVERITY:
        hits = _has(lowered, markers)
        if hits:
            return level, hits
    return "medium", []


def _severity_from_readings(readings: dict[str, float]) -> tuple[str | None, list[str]]:
    worst: str | None = None
    hits: list[str] = []
    for key, value in readings.items():
        for marker, high, critical in _READING_THRESHOLDS:
            if marker not in key.lower():
                continue
            if value >= critical:
                worst = "critical" if worst is None else _worst(worst, "critical")
                hits.append(f"{key}={value} at or above the critical limit {critical}")
            elif value >= high:
                worst = "high" if worst is None else _worst(worst, "high")
                hits.append(f"{key}={value} at or above the alert limit {high}")
    return worst, hits


def _recommended_spare(citations: list[Citation], asset_class: str | None) -> str | None:
    """Prefer a SKU the manual itself names; fall back to the catalogue."""
    for citation in citations:
        for candidate in _SKU_RE.findall(citation.snippet):
            if registry.get_spare(candidate):
                return candidate
    if asset_class:
        spares = registry.spares_for_class(asset_class)
        if len(spares) == 1:
            return spares[0].sku
    return None


def _simulate_diagnosis(
    text: str, citations: list[Citation], asset_class: str | None, readings: dict[str, float],
) -> dict[str, Any]:
    """Rule-based diagnosis over the retrieved passages."""
    lowered = text.lower()
    # Only well-matched sections count as hazard evidence. The generic HSE
    # policy document is retrievable for every asset class, and its section
    # titles ("Lock-Out/Tag-Out for Safety-Critical Defects") contain the same
    # words as a genuine hazard -- so a weak match to it would otherwise flag a
    # cosmetic paint scratch as safety-critical. A heading describes a
    # procedure; only a section the signal actually matches is evidence about
    # *this* fault.
    # Hazard flags come from the section that describes THIS fault -- the
    # top-ranked hit -- plus anything effectively tied with it.
    #
    # Lower-ranked sections are still shown as sources and still feed the
    # failure-mode list, but they must not drive safety_risk. Within an
    # asset-class-filtered corpus, the class word ("conveyor") is shared by
    # every section, so a section about an unrelated fault on the same machine
    # scores high on lexical overlap alone: "Conveyor Emergency Stop Integrity"
    # would otherwise flag a routine bearing-vibration trend as
    # safety-critical, and a system that cries P1 at everything gets ignored.
    evidence = [c for c in citations if c.score >= _EVIDENCE_TIE_FLOOR] or citations[:1]
    corpus = " ".join(f"{c.section} {c.snippet}".lower() for c in evidence)

    severity, signal_hits = _severity_from_text(text)
    reading_severity, reading_hits = _severity_from_readings(readings)
    if reading_severity:
        severity = _worst(severity, reading_severity)

    safety_risk = bool(_has(corpus, _MANUAL_SAFETY_MARKERS))
    if safety_risk:
        # The manual says this condition endangers people: never below "high".
        severity = _worst(severity, "high")

    injury = bool(_has(lowered, _INJURY_MARKERS))
    near_miss = bool(_has(lowered, _NEAR_MISS_MARKERS))
    if injury:
        severity = "critical"

    env_risk = bool(_has(corpus, _MANUAL_ENV_MARKERS)) and bool(
        _has(lowered, ("leak", "leaking", "leakage", "spill", "oil", "fuel", "coolant", "hydraulic", "release"))
    )
    env_risk = env_risk or bool(_has(lowered, _ENV_SIGNAL_MARKERS))

    # Only sections that plausibly describe this fault become failure modes. A
    # weakly-matched general policy section is a useful *source* but listing it
    # as a "22% likely failure mode" is noise that trains people to skim.
    failure_modes = [
        FailureMode(
            name=c.section.split(" ", 1)[-1] if " " in c.section else c.section,
            likelihood=round(c.score, 2),
            rationale=c.snippet.split(". ")[0][:200] + ".",
            citations=[c],
        )
        for c in citations[:3] if c.score >= _FAILURE_MODE_FLOOR
    ] or ([
        FailureMode(
            name=citations[0].section.split(" ", 1)[-1],
            likelihood=round(citations[0].score, 2),
            rationale=citations[0].snippet.split(". ")[0][:200] + ".",
            citations=[citations[0]],
        )
    ] if citations else [])

    top_score = citations[0].score if citations else 0.0
    confidence = 0.35 + 0.35 * top_score + (0.12 if signal_hits or reading_hits else 0.0)
    confidence += 0.10 if len(citations) >= 2 else 0.0
    if not citations:
        confidence = 0.25
    confidence = round(min(confidence, 0.93), 2)

    lead = failure_modes[0].name.lower() if failure_modes else "an unclassified fault"
    summary = (
        f"Most likely {lead}, assessed as {severity} severity"
        f"{' with a safety risk to personnel' if safety_risk else ''}"
        f"{' and a potential environmental release' if env_risk else ''}. "
        f"Grounded in {len(citations)} manual section(s); "
        f"{'telemetry exceeds published limits' if reading_hits else 'based on the reported symptoms'}."
    )

    return {
        "summary": summary,
        "failure_modes": [
            {
                "name": fm.name, "likelihood": fm.likelihood, "rationale": fm.rationale,
                "sections": [c.section.split(" ")[0] for c in fm.citations],
            }
            for fm in failure_modes
        ],
        "severity": severity,
        "safety_risk": safety_risk,
        "environmental_risk": env_risk,
        "injury_reported": injury,
        "near_miss": near_miss,
        "confidence": confidence,
        "recommended_spare": _recommended_spare(citations, asset_class),
        "_evidence": {"signal_markers": signal_hits, "reading_markers": reading_hits},
    }


def _citations_for_sections(sections: list[str], pool: list[Citation]) -> list[Citation]:
    """Map section ids the model returned back to real retrieved citations.

    A model can only cite what we gave it; anything it names that we did not
    retrieve is dropped rather than shown. This is the difference between a
    citation and a plausible-looking string.
    """
    out: list[Citation] = []
    for section in sections:
        key = section.strip().split(" ")[0].lower()
        for c in pool:
            if c.section.lower().startswith(key):
                out.append(c)
                break
    return out


async def diagnose(ctx: AgentContext) -> dict[str, Any]:
    run = ctx.run
    asset = run.asset
    readings = run.signal.readings

    extracts = "\n\n".join(
        f"[{c.doc_id} {c.section}]\n{c.snippet}" for c in run.citations
    ) or "(no manual extracts retrieved)"

    system, user = llm.render_prompt(
        "diagnose",
        asset_name=asset.name if asset else "Unidentified asset",
        asset_class=asset.asset_class if asset else "unknown",
        manufacturer=asset.manufacturer if asset else "",
        model=asset.model if asset else "",
        criticality=asset.criticality if asset else 3,
        location=asset.location if asset else "unknown",
        extracts=extracts,
        kind=run.signal.kind,
        text=run.working_text,
        readings=f"TELEMETRY: {readings}" if readings else "",
    )

    result = await llm.json_completion(
        system=system, user=user,
        required_keys=("summary", "severity", "confidence"),
        simulate=lambda: _simulate_diagnosis(
            run.working_text, run.citations,
            asset.asset_class if asset else None, readings,
        ),
    )
    data = result.data

    severity = str(data.get("severity", "medium")).lower()
    if severity not in _SEVERITY_RANK:
        severity = "medium"

    modes: list[FailureMode] = []
    for raw in data.get("failure_modes", []) or []:
        modes.append(
            FailureMode(
                name=str(raw.get("name", "unnamed")),
                likelihood=float(raw.get("likelihood", 0.0) or 0.0),
                rationale=str(raw.get("rationale", "")),
                citations=_citations_for_sections(list(raw.get("sections", []) or []), run.citations),
            )
        )

    diagnosis = Diagnosis(
        summary=str(data.get("summary", "")).strip(),
        failure_modes=modes,
        severity=severity,  # type: ignore[arg-type]
        safety_risk=bool(data.get("safety_risk", False)),
        environmental_risk=bool(data.get("environmental_risk", False)),
        injury_reported=bool(data.get("injury_reported", False)),
        near_miss=bool(data.get("near_miss", False)),
        confidence=round(float(data.get("confidence", 0.0) or 0.0), 3),
        recommended_spare=data.get("recommended_spare") or None,
        citations=run.citations,
    )

    if not diagnosis.summary:
        raise Halt("diagnosis returned no summary", {"provider": result.provider})

    run.diagnosis = diagnosis
    return {
        "provider": result.provider,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "repaired": result.repaired,
        "severity": diagnosis.severity,
        "confidence": diagnosis.confidence,
        "safety_risk": diagnosis.safety_risk,
        "environmental_risk": diagnosis.environmental_risk,
        "failure_modes": [m.name for m in diagnosis.failure_modes],
        "recommended_spare": diagnosis.recommended_spare,
        "summary": diagnosis.summary,
        "evidence": data.get("_evidence", {}),
    }
