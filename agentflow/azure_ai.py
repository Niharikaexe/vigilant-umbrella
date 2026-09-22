"""Adapters for the Azure AI Services used by the pipeline.

Each function has the same shape: try the live Azure service when it is
configured, otherwise fall back to a deterministic local implementation and say
so in the returned ``backend`` string. The dashboard surfaces that string, so
nobody can mistake a simulated run for a cloud one.

Services wired in:

=========================  =======================================
Azure AI Language          PII detection and redaction
Azure AI Translator        Language detection and translation
Azure AI Content Safety    Input/output guardrails
Azure AI Document Intel.   Text and layout from scanned reports
Azure OpenAI               see ``llm.py``
Azure AI Search            see ``retrieval.py``
=========================  =======================================
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from .config import get_settings

# --------------------------------------------------------------------------
# PII redaction (Azure AI Language)
# --------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("PhoneNumber", re.compile(r"(?<!\d)(?:\+\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?)\d{3}[\s-]?\d{3,4}(?!\d)")),
    ("EmployeeId", re.compile(r"\b(?:emp|employee|badge|pers)[\s#:-]*\d{3,8}\b", re.I)),
    ("IPAddress", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("CreditCard", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
]


async def redact_pii(text: str) -> tuple[str, int, list[str], str]:
    """Return ``(redacted_text, count, categories, backend)``.

    Redaction happens *before* the text reaches any model. If the Language
    service is configured but errors, we fail closed to the local regex pass
    rather than forwarding un-redacted text -- the one thing we must never do.
    """
    s = get_settings()
    if s.language_endpoint and s.language_key:
        try:
            return await _azure_redact(text)
        except (httpx.HTTPError, KeyError, ValueError, IndexError):
            pass

    redacted = text
    categories: list[str] = []
    count = 0
    for label, pattern in _PII_PATTERNS:
        redacted, n = pattern.subn(f"[{label}]", redacted)
        if n:
            count += n
            categories.append(label)
    return redacted, count, categories, "local_regex"


async def _azure_redact(text: str) -> tuple[str, int, list[str], str]:
    s = get_settings()
    url = f"{s.language_endpoint.rstrip('/')}/language/:analyze-text?api-version=2023-04-01"
    payload = {
        "kind": "PiiEntityRecognition",
        "parameters": {"modelVersion": "latest"},
        "analysisInput": {"documents": [{"id": "1", "language": "en", "text": text}]},
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers={"Ocp-Apim-Subscription-Key": s.language_key})
        resp.raise_for_status()
        doc = resp.json()["results"]["documents"][0]
    entities = doc.get("entities", [])
    return (
        doc.get("redactedText", text),
        len(entities),
        sorted({e.get("category", "Unknown") for e in entities}),
        "azure_ai_language",
    )


# --------------------------------------------------------------------------
# Language detection and translation (Azure AI Translator)
# --------------------------------------------------------------------------

# Stopword fingerprints for the offline detector. Crude, but a shipyard floor
# report is short and these words are near-unavoidable in each language.
_LANG_MARKERS: dict[str, set[str]] = {
    "nl": {"de", "het", "een", "niet", "lekt", "en", "van", "is", "naar", "olie", "bij", "geen"},
    "de": {"der", "die", "das", "nicht", "und", "ist", "auf", "bei", "kein", "leckt", "von"},
    "es": {"el", "la", "los", "una", "no", "con", "aceite", "fuga", "esta", "por"},
    "fr": {"le", "la", "les", "une", "pas", "est", "avec", "huile", "fuite", "sur"},
    "pl": {"nie", "jest", "sie", "na", "olej", "wyciek", "oraz", "pompa", "przy"},
}


def detect_language_offline(text: str) -> str:
    words = set(re.findall(r"[a-zA-ZÀ-ÿąćęłńóśźż]+", text.lower()))
    if not words:
        return "en"
    best, best_hits = "en", 0
    for lang, markers in _LANG_MARKERS.items():
        hits = len(words & markers)
        if hits > best_hits:
            best, best_hits = lang, hits
    # Two independent markers before we claim it is not English -- one shared
    # word ("de", "la") is not evidence.
    return best if best_hits >= 2 else "en"


async def translate(text: str, target: str = "en") -> tuple[str, str, bool, str]:
    """Return ``(text_in_target_language, detected_language, translated, backend)``."""
    s = get_settings()
    if s.translator_key:
        try:
            return await _azure_translate(text, target)
        except (httpx.HTTPError, KeyError, ValueError, IndexError):
            pass

    detected = detect_language_offline(text)
    # Without the service we cannot translate, only detect. Say so honestly and
    # let the pipeline mark the node degraded rather than pretend.
    return text, detected, False, "local_detect_only"


async def _azure_translate(text: str, target: str) -> tuple[str, str, bool, str]:
    s = get_settings()
    url = f"{s.translator_endpoint.rstrip('/')}/translate?api-version=3.0&to={target}"
    headers = {"Ocp-Apim-Subscription-Key": s.translator_key, "Content-Type": "application/json"}
    if s.translator_region:
        headers["Ocp-Apim-Subscription-Region"] = s.translator_region
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=[{"Text": text}], headers=headers)
        resp.raise_for_status()
        item = resp.json()[0]
    detected = item.get("detectedLanguage", {}).get("language", "en")
    translated_text = item["translations"][0]["text"]
    return translated_text, detected, detected != target, "azure_ai_translator"


# --------------------------------------------------------------------------
# Guardrails (Azure AI Content Safety)
# --------------------------------------------------------------------------

# Prompt-injection probes. A maintenance signal can arrive from an email or a
# Teams message, so the text is genuinely untrusted input to the model.
_INJECTION_PATTERNS = [
    re.compile(r"ignore (?:all |any )?(?:previous|prior|above) instructions", re.I),
    re.compile(r"disregard (?:the )?(?:system|previous) (?:prompt|instructions)", re.I),
    re.compile(r"you are now (?:a|an|in) ", re.I),
    re.compile(r"reveal (?:your )?(?:system )?prompt", re.I),
    re.compile(r"\bauto[- ]?approve\b.*\beverything\b", re.I),
    re.compile(r"</?(?:system|instructions?)>", re.I),
]

_ABUSE_TERMS = {"kill yourself", "idiot", "moron"}


async def screen_text(text: str, *, direction: str = "input") -> tuple[bool, dict[str, Any], str]:
    """Return ``(allowed, detail, backend)``.

    Fails **closed**: if Content Safety is configured and unreachable, the run
    stops. A guardrail that opens under load is not a guardrail.
    """
    s = get_settings()
    if s.content_safety_endpoint and s.content_safety_key:
        try:
            return await _azure_screen(text)
        except httpx.HTTPError as exc:
            return False, {"reason": "content_safety_unavailable", "error": str(exc)}, "azure_content_safety"

    lowered = text.lower()
    injection = [p.pattern for p in _INJECTION_PATTERNS if p.search(text)]
    abuse = [t for t in _ABUSE_TERMS if t in lowered]
    allowed = not injection and not abuse
    detail: dict[str, Any] = {
        "prompt_injection": bool(injection),
        "matched_patterns": injection[:3],
        "abuse": abuse,
        "direction": direction,
    }
    if not allowed:
        detail["reason"] = "prompt_injection" if injection else "abusive_language"
    return allowed, detail, "local_heuristics"


async def _azure_screen(text: str) -> tuple[bool, dict[str, Any], str]:
    s = get_settings()
    url = f"{s.content_safety_endpoint.rstrip('/')}/contentsafety/text:analyze?api-version=2024-09-01"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            json={"text": text[:10000], "outputType": "FourSeverityLevels"},
            headers={"Ocp-Apim-Subscription-Key": s.content_safety_key},
        )
        resp.raise_for_status()
        data = resp.json()
    categories = {c["category"]: c["severity"] for c in data.get("categoriesAnalysis", [])}
    worst = max(categories.values(), default=0)
    return worst < 4, {"categories": categories, "max_severity": worst}, "azure_content_safety"


# --------------------------------------------------------------------------
# Document Intelligence
# --------------------------------------------------------------------------


async def extract_document_text(content: bytes, content_type: str) -> tuple[str, str]:
    """Pull text out of an uploaded inspection report. ``(text, backend)``."""
    s = get_settings()
    if s.docint_endpoint and s.docint_key and not content_type.startswith("text/"):
        try:
            return await _azure_docint(content), "azure_document_intelligence"
        except (httpx.HTTPError, KeyError, ValueError):
            pass
    try:
        return content.decode("utf-8", errors="replace"), "plain_text"
    except Exception:  # pragma: no cover - defensive
        return "", "plain_text"


async def _azure_docint(content: bytes) -> str:
    s = get_settings()
    url = (
        f"{s.docint_endpoint.rstrip('/')}/documentintelligence/documentModels/"
        f"prebuilt-read:analyze?api-version=2024-11-30"
    )
    headers = {"Ocp-Apim-Subscription-Key": s.docint_key, "Content-Type": "application/octet-stream"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, content=content, headers=headers)
        resp.raise_for_status()
        op_url = resp.headers["operation-location"]
        for _ in range(30):
            poll = await client.get(op_url, headers={"Ocp-Apim-Subscription-Key": s.docint_key})
            poll.raise_for_status()
            body = poll.json()
            if body.get("status") == "succeeded":
                return body["analyzeResult"]["content"]
            if body.get("status") == "failed":
                raise ValueError("document intelligence analysis failed")
    raise TimeoutError("document intelligence timed out")
