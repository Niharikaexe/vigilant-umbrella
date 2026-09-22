"""LLM access: Azure OpenAI, OpenAI, or a deterministic simulator.

Three things matter here and they are the same three things that matter in any
production LLM feature:

1. **Structured output.** We ask for JSON and validate it against the caller's
   expected keys. A free-text answer cannot drive a work order.
2. **A repair loop.** Models occasionally emit prose around the JSON or drop a
   field. One cheap repair turn recovers most of that; after that we give up
   rather than retry forever.
3. **A fallback that is honest.** With no credentials configured the caller's
   ``simulate`` callable runs instead, and the result is tagged
   ``provider="simulated"`` all the way to the UI.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import get_settings, prompts_config

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class LLMResult:
    data: dict[str, Any]
    provider: str
    model: str = ""
    latency_ms: int = 0
    repaired: bool = False
    notes: list[str] = field(default_factory=list)


class LLMError(RuntimeError):
    pass


def render_prompt(key: str, **variables: Any) -> tuple[str, str]:
    """Render a named prompt template into ``(system, user)``.

    Prompts live in ``config/prompts.yaml``, not in Python. They are versioned,
    diffable and editable by someone who is tuning behaviour without touching
    application code -- which is how prompt changes stop being code releases.
    """
    prompts = prompts_config().get("prompts", {})
    if key not in prompts:
        raise LLMError(f"unknown prompt {key!r}")
    tmpl = prompts[key]
    system = tmpl.get("system", "").format(**variables)
    user = tmpl.get("user", "").format(**variables)
    return system, user


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if not match:
            raise
        return json.loads(match.group(0))


async def _call_provider(system: str, user: str, temperature: float, max_tokens: int) -> tuple[str, str, str]:
    """Return ``(content, provider, model)``."""
    s = get_settings()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    if s.llm_provider == "azure" and s.azure_openai_ready:
        url = (
            f"{s.azure_openai_endpoint.rstrip('/')}/openai/deployments/"
            f"{s.azure_openai_deployment}/chat/completions"
            f"?api-version={s.azure_openai_api_version}"
        )
        headers = {"api-key": s.azure_openai_key}
        model = s.azure_openai_deployment
        provider = "azure_openai"
    elif s.llm_provider == "openai" and s.openai_ready:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {s.openai_key}"}
        payload["model"] = s.openai_model
        model = s.openai_model
        provider = "openai"
    else:
        raise LLMError("no live LLM provider configured")

    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
    return body["choices"][0]["message"]["content"], provider, model


async def json_completion(
    *,
    system: str,
    user: str,
    required_keys: tuple[str, ...] = (),
    simulate: Callable[[], dict[str, Any]] | None = None,
    temperature: float = 0.1,
    max_tokens: int = 900,
) -> LLMResult:
    """Ask for a JSON object, validate it, repair once, or simulate."""
    started = time.perf_counter()
    s = get_settings()
    live = (s.llm_provider == "azure" and s.azure_openai_ready) or (
        s.llm_provider == "openai" and s.openai_ready
    )

    if live:
        notes: list[str] = []
        try:
            content, provider, model = await _call_provider(system, user, temperature, max_tokens)
            try:
                data = _parse_json(content)
                missing = [k for k in required_keys if k not in data]
                if missing:
                    raise ValueError(f"missing keys: {missing}")
            except (json.JSONDecodeError, ValueError) as exc:
                # One repair turn: hand the model its own output and the error.
                notes.append(f"repair triggered: {exc}")
                repair_user = (
                    f"{user}\n\n---\nYour previous reply was rejected: {exc}\n"
                    f"Previous reply:\n{content[:2000]}\n\n"
                    f"Reply again with ONLY a valid JSON object containing exactly "
                    f"these keys: {list(required_keys)}."
                )
                content, provider, model = await _call_provider(system, repair_user, 0.0, max_tokens)
                data = _parse_json(content)
                return LLMResult(
                    data=data, provider=provider, model=model, repaired=True, notes=notes,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            return LLMResult(
                data=data, provider=provider, model=model, notes=notes,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except (httpx.HTTPError, json.JSONDecodeError, LLMError, KeyError) as exc:
            if simulate is None:
                raise LLMError(f"LLM call failed: {exc}") from exc
            notes.append(f"live call failed, simulated instead: {exc}")
            return LLMResult(
                data=simulate(), provider="simulated", notes=notes,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

    if simulate is None:
        raise LLMError("no LLM provider configured and no simulation available")
    return LLMResult(
        data=simulate(),
        provider="simulated",
        model="rule-based-simulator",
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
