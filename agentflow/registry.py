"""The asset registry and spare parts catalogue.

Resolution is layered cheapest-first: an explicit id, then a known alias, then
fuzzy token overlap. Only if all three are inconclusive do we spend an LLM call
on disambiguation. Most real signals name the asset plainly, and paying a model
to read "PMP-003" back to you is how demos become expensive in production.
"""

from __future__ import annotations

import difflib
import functools
import re
from typing import Any

from .config import assets_config
from .models import Asset, Spare

_ID_RE = re.compile(r"\b([A-Z]{3})[- ]?(\d{2,4})\b")
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "on", "in", "at", "of", "and", "to",
    "it", "its", "we", "i", "there", "has", "have", "with", "from", "for",
}


@functools.lru_cache(maxsize=1)
def get_assets() -> list[Asset]:
    return [Asset(**a) for a in assets_config().get("assets", [])]


@functools.lru_cache(maxsize=1)
def get_spares() -> list[Spare]:
    return [Spare(**s) for s in assets_config().get("spares", [])]


@functools.lru_cache(maxsize=1)
def get_sites() -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in assets_config().get("sites", [])}


def get_asset(asset_id: str) -> Asset | None:
    return next((a for a in get_assets() if a.id == asset_id), None)


def spares_for_class(asset_class: str) -> list[Spare]:
    return [s for s in get_spares() if asset_class in s.asset_classes]


def get_spare(sku: str) -> Spare | None:
    return next((s for s in get_spares() if s.sku == sku), None)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS}


def _number_words(text: str) -> set[str]:
    """Operators write 'pump three' as often as 'pump 3'."""
    words = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
        "fourteen": "14", "twenty one": "21", "twentyone": "21",
    }
    found = set()
    lowered = text.lower()
    for word, digit in words.items():
        if re.search(rf"\b{word}\b", lowered):
            found.add(digit)
    return found


def score_asset(asset: Asset, text: str) -> float:
    """0.0-1.0 confidence that ``text`` refers to ``asset``."""
    lowered = text.lower()

    # 1. Explicit registry id -- unambiguous.
    for prefix, number in _ID_RE.findall(text.upper()):
        if f"{prefix}-{int(number):03d}" == asset.id or f"{prefix}{number}" == asset.id.replace("-", ""):
            return 1.0

    # 2. A configured alias appearing verbatim.
    for alias in asset.aliases:
        if alias.lower() in lowered:
            return 0.93

    # 3. Fuzzy overlap between the signal and the asset's descriptive tokens.
    signal_tokens = _tokens(text) | _number_words(text)
    asset_tokens = _tokens(f"{asset.name} {asset.asset_class} {asset.location} {asset.manufacturer} {asset.model}")
    asset_tokens |= {re.sub(r"^0+", "", asset.id.split("-")[1])} if "-" in asset.id else set()

    if not asset_tokens:
        return 0.0
    overlap = signal_tokens & asset_tokens
    if not overlap:
        return 0.0

    score = len(overlap) / len(asset_tokens)
    # A matching unit number is strong evidence; two assets of the same class
    # differ only by that number.
    asset_number = asset.id.split("-")[-1].lstrip("0")
    if asset_number and asset_number in signal_tokens:
        score += 0.35
    # Class words alone ("pump", "crane") are weak without a number.
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(signal_tokens)), " ".join(sorted(asset_tokens))).ratio()
    score = 0.7 * score + 0.3 * ratio
    return round(min(score, 0.9), 3)


def _unique_class_match(text: str) -> Asset | None:
    """If the text names an equipment class and the site has exactly one of
    them, that is a confident match even with no unit number.

    "the generator will not start" is unambiguous on a site with one generator
    and genuinely ambiguous on a site with six. Let the registry decide which
    situation we are in rather than hard-coding either answer.
    """
    signal_tokens = _tokens(text)
    matched: list[Asset] = []
    for asset in get_assets():
        class_words = set(asset.asset_class.split("_")) | _tokens(asset.name)
        if signal_tokens & (class_words - _tokens(asset.location)):
            matched.append(asset)
    if len(matched) == 1:
        return matched[0]
    # Narrow by the class word itself, in case names overlap loosely.
    by_class = [
        a for a in get_assets()
        if signal_tokens & set(a.asset_class.split("_"))
    ]
    return by_class[0] if len(by_class) == 1 else None


def resolve(text: str, hint: str | None = None) -> tuple[Asset | None, float, list[dict[str, Any]]]:
    """Return ``(best_asset, confidence, ranked_candidates)``."""
    haystack = f"{hint or ''} {text}".strip()
    scored = sorted(
        ((score_asset(a, haystack), a) for a in get_assets()),
        key=lambda pair: pair[0],
        reverse=True,
    )
    candidates = [
        {"asset_id": a.id, "name": a.name, "score": s}
        for s, a in scored[:4] if s > 0.05
    ]
    if not scored or scored[0][0] < 0.25:
        sole = _unique_class_match(haystack)
        if sole is not None:
            return sole, 0.78, [{"asset_id": sole.id, "name": sole.name, "score": 0.78}]
        return None, scored[0][0] if scored else 0.0, candidates

    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    # Two plausible matches that are close together is ambiguity, not a match.
    if best_score - runner_up < 0.08 and best_score < 0.9:
        return None, best_score, candidates
    return best, best_score, candidates


def registry_digest(limit: int | None = None) -> str:
    """Compact registry rendering for the disambiguation prompt."""
    lines = []
    for a in get_assets()[:limit]:
        lines.append(
            f"- {a.id} | {a.name} | class={a.asset_class} | site={a.site} | "
            f"location={a.location} | criticality={a.criticality} | aliases={', '.join(a.aliases)}"
        )
    return "\n".join(lines)
