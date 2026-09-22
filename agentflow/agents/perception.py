"""Perception agents: normalise, guard, redact, translate.

Everything here runs *before* any model sees the text. The ordering is
deliberate and worth defending in a review: screen first (so a prompt-injection
payload never reaches a model at all), redact second (so PII never leaves the
trust boundary), translate third (so the model works in one language).
"""

from __future__ import annotations

from typing import Any

from .. import azure_ai
from ..config import get_settings
from .base import AgentContext, Degraded, Halt


async def intake(ctx: AgentContext) -> dict[str, Any]:
    """Normalise any inbound channel into the canonical working text."""
    signal = ctx.run.signal
    text = signal.text.strip()
    if not text:
        raise Halt("empty signal", {"reason": "no_text"})

    # Readings deliberately stay OUT of the text channel. Folding them in looks
    # harmless until "vibration_mm_s=8.2" tokenises to {8, 2} and the "2" matches
    # "Gantry Crane 2", so a conveyor alert resolves to a crane. The diagnosis
    # agent receives readings as structured data instead, where they belong.
    ctx.run.working_text = text
    return {
        "channel": signal.channel,
        "kind": signal.kind,
        "chars": len(text),
        "readings": signal.readings,
        "reported_by": signal.reported_by,
    }


async def guard_input(ctx: AgentContext) -> dict[str, Any]:
    """Screen untrusted inbound text before a model ever sees it."""
    allowed, detail, backend = await azure_ai.screen_text(ctx.run.working_text, direction="input")
    if not allowed:
        raise Halt(
            f"input rejected by guardrail: {detail.get('reason', 'unsafe_content')}",
            {"backend": backend, **detail},
        )
    return {"backend": backend, "allowed": True, **detail}


async def redact(ctx: AgentContext) -> dict[str, Any]:
    """Strip personal data before the text leaves the trust boundary."""
    redacted, count, categories, backend = await azure_ai.redact_pii(ctx.run.working_text)
    ctx.run.working_text = redacted
    ctx.run.redactions = count
    return {"backend": backend, "redactions": count, "categories": categories}


async def translate(ctx: AgentContext) -> dict[str, Any]:
    """Detect the language and normalise to the working language."""
    target = get_settings().working_language
    text, detected, translated, backend = await azure_ai.translate(ctx.run.working_text, target)
    ctx.run.language = detected
    ctx.run.working_text = text

    if detected != target and not translated:
        # We know it is not English but cannot translate it. Continue -- the
        # retrieval and rules layers still work on asset ids and telemetry --
        # but mark the node degraded so nobody trusts the diagnosis blindly.
        raise Degraded(
            f"detected {detected!r} but no translation service is configured",
            {"backend": backend, "detected_language": detected, "translated": False},
        )
    return {
        "backend": backend,
        "detected_language": detected,
        "translated": translated,
        "target": target,
    }
