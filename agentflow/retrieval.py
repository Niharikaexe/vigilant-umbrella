"""Manual retrieval -- the grounding layer.

A diagnosis the copilot cannot point at a manual section for is worth very
little, so retrieval is a first-class step rather than an afterthought.

Two backends behind one interface:

* **Local BM25** (default) -- pure Python, no dependencies, no network. Good
  enough for a corpus of manuals and it makes the demo and the tests hermetic.
* **Azure AI Search** -- set ``AZURE_SEARCH_ENDPOINT``/``AZURE_SEARCH_API_KEY``
  and the same call goes to a hybrid semantic index instead.

Chunking is by manual *section* (``## s4.2 Hoist Brake Performance``) rather
than by token window, because the section id is exactly the citation an
engineer or auditor needs. Chunk boundaries should match the unit of reference.
"""

from __future__ import annotations

import functools
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import yaml

from .config import KNOWLEDGE_DIR, get_settings
from .models import Citation

_WORD_RE = re.compile(r"[a-z0-9]+")
_SECTION_RE = re.compile(r"^##\s+(s[\d.]+)\s+(.*)$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Domain synonyms. Operators do not write like manuals do: a fitter says
# "leaking oil", the manual says "shaft seal". Bridging that vocabulary gap is
# most of what makes retrieval work on real maintenance text.
SYNONYMS: dict[str, list[str]] = {
    "leak": ["leakage", "weeping", "drip", "seal", "spill"],
    "leaking": ["leakage", "seal", "weeping", "drip"],
    "noise": ["noisy", "knocking", "rattling", "grinding", "abnormal"],
    "grinding": ["noise", "brake", "wear"],
    "hot": ["temperature", "overheating", "thermal"],
    "overheating": ["temperature", "hot", "cooler"],
    "vibration": ["vibrating", "juddering", "bearing", "rms"],
    "smoke": ["exhaust", "combustion", "black"],
    "wont": ["failure", "fails", "not"],
    "start": ["starting", "start"],
    "drop": ["drift", "drifting", "lowering"],
    "drift": ["drop", "brake", "holding"],
    "rope": ["wire", "discard", "broken"],
    "pressure": ["bar", "loss", "hydraulic"],
    "belt": ["tracking", "conveyor", "torn"],
    "filter": ["differential", "clogged", "restriction"],
}


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _expand(tokens: list[str]) -> list[str]:
    out = list(tokens)
    for tok in tokens:
        out.extend(SYNONYMS.get(tok, ()))
    return out


class Chunk:
    __slots__ = ("doc_id", "title", "section", "heading", "text", "asset_classes", "tokens")

    def __init__(self, doc_id: str, title: str, section: str, heading: str, text: str, asset_classes: list[str]):
        self.doc_id = doc_id
        self.title = title
        self.section = section
        self.heading = heading
        self.text = text
        self.asset_classes = asset_classes
        self.tokens = _tokenize(f"{heading} {text}")


def _parse_document(path: Path) -> list[Chunk]:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    fm = _FRONTMATTER_RE.match(raw)
    if fm:
        meta = yaml.safe_load(fm.group(1)) or {}
        raw = raw[fm.end():]

    doc_id = meta.get("doc_id", path.stem.upper())
    title = meta.get("title", path.stem)
    asset_classes = meta.get("asset_classes", ["*"])

    chunks: list[Chunk] = []
    matches = list(_SECTION_RE.finditer(raw))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end].strip()
        chunks.append(Chunk(doc_id, title, m.group(1), m.group(2).strip(), body, asset_classes))
    return chunks


class LocalIndex:
    """A small BM25 index. Deliberately dependency-free."""

    K1 = 1.4
    B = 0.75

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.n = max(len(chunks), 1)
        self.avg_len = sum(len(c.tokens) for c in chunks) / self.n if chunks else 1.0
        df: Counter[str] = Counter()
        for c in chunks:
            df.update(set(c.tokens))
        self.idf = {
            term: math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def search(self, query: str, asset_class: str | None = None, top_k: int = 4) -> list[Citation]:
        terms = _expand(_tokenize(query))
        if not terms:
            return []

        scored: list[tuple[float, Chunk]] = []
        for chunk in self.chunks:
            # Filter to the asset class first: retrieving the crane manual for a
            # compressor fault is worse than retrieving nothing.
            if (
                asset_class
                and chunk.asset_classes
                and "*" not in chunk.asset_classes
                and asset_class not in chunk.asset_classes
            ):
                continue
            tf = Counter(chunk.tokens)
            dl = len(chunk.tokens) or 1
            score = 0.0
            for term in terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = self.idf.get(term, 0.0)
                score += idf * (f * (self.K1 + 1)) / (f + self.K1 * (1 - self.B + self.B * dl / self.avg_len))
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = scored[:top_k]
        if not top:
            return []
        best = top[0][0] or 1.0
        return [
            Citation(
                doc_id=c.doc_id,
                title=c.title,
                section=f"{c.section} {c.heading}",
                snippet=_snippet(c.text),
                score=round(min(s / best, 1.0), 3),
            )
            for s, c in top
        ]


def _snippet(text: str, limit: int = 460) -> str:
    """Snippets are *evidence*, not UI preview text.

    Truncating too aggressively silently drops the part naming the spare part
    or the environmental hazard, so the diagnosis quietly gets worse with no
    error anywhere. Keep whole sentences and err on the long side.
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    # Prefer a sentence boundary so the snippet reads as a complete statement.
    stop = cut.rfind(". ")
    return (cut[: stop + 1] if stop > limit * 0.6 else cut.rsplit(" ", 1)[0] + "...")


@functools.lru_cache(maxsize=1)
def get_index() -> LocalIndex:
    chunks: list[Chunk] = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        chunks.extend(_parse_document(path))
    return LocalIndex(chunks)


async def _azure_search(query: str, asset_class: str | None, top_k: int) -> list[Citation]:
    s = get_settings()
    url = f"{s.search_endpoint.rstrip('/')}/indexes/{s.search_index}/docs/search?api-version=2023-11-01"
    body: dict[str, Any] = {"search": query, "top": top_k, "queryType": "semantic"}
    if asset_class:
        body["filter"] = f"asset_classes/any(c: c eq '{asset_class}' or c eq '*')"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=body, headers={"api-key": s.search_key})
        resp.raise_for_status()
        docs = resp.json().get("value", [])
    return [
        Citation(
            doc_id=d.get("doc_id", "?"),
            title=d.get("title", "?"),
            section=d.get("section", "?"),
            snippet=_snippet(d.get("content", "")),
            score=round(float(d.get("@search.score", 0.0)), 3),
        )
        for d in docs
    ]


async def search(query: str, asset_class: str | None = None, top_k: int = 4) -> tuple[list[Citation], str]:
    """Retrieve grounding passages. Returns ``(citations, backend_used)``.

    Azure AI Search failures degrade to the local index rather than taking the
    run down -- losing semantic ranking is acceptable, losing the diagnosis is
    not. The rule set separately refuses to auto-action anything uncited.
    """
    s = get_settings()
    if s.search_endpoint and s.search_key:
        try:
            hits = await _azure_search(query, asset_class, top_k)
            return hits, "azure_ai_search"
        except (httpx.HTTPError, ValueError, KeyError):
            pass
    return get_index().search(query, asset_class, top_k), "local_bm25"
